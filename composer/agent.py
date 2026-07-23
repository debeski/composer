import hashlib
import json
import os
import random
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .agent_protocol import ProtocolError, redact_text, utc_now, validate_command
from .agent_store import AgentStore
from .control_client import ControlPlaneClient, ControlPlaneError
from .service_selection import join_service_list, parse_service_list
from .version import read_composer_version
from .watcher import WatchRuntime


PROTECTED_RESTART_SERVICES = frozenset(
    {
        "db",
        "database",
        "postgres",
        "postgresql",
        "redis",
        "backup",
        "db-backup",
        "pgadmin",
        "dlux-updater",
        "composer-agent",
        "composer-updater",
        "docker-socket-proxy",
    }
)


class ComposerAgent:
    def __init__(self, args):
        self.args = args
        self.composer_version = read_composer_version()
        self.control_url = str(
            args.control_url or os.environ.get("COMPOSER_CONTROL_URL") or ""
        ).strip()
        self.enrollment_token = str(
            args.enrollment_token or os.environ.get("COMPOSER_ENROLLMENT_TOKEN") or ""
        ).strip()
        state_dir = (
            args.state_dir
            or os.environ.get("COMPOSER_AGENT_STATE_DIR")
            or "/var/lib/composer-agent"
        )
        self.store = AgentStore(state_dir)
        trigger = Path(args.trigger_file)
        if not args.status_file:
            args.status_file = str(trigger.with_name("deploy-status.json"))
        if not args.availability_file and args.check_image:
            args.availability_file = str(trigger.with_name("image-available.json"))
        self.bridge_dir = Path(
            args.bridge_dir or trigger.parent / "agent"
        )
        self.bridge_requests = self.bridge_dir / "requests"
        self.bridge_results = self.bridge_dir / "results"
        self.bridge_requests.mkdir(parents=True, exist_ok=True)
        self.bridge_results.mkdir(parents=True, exist_ok=True)
        self.watch = WatchRuntime(args)
        self.args.log_file = self.watch.log_file
        self.client = (
            ControlPlaneClient(
                self.control_url,
                allow_http_localhost=bool(args.allow_http_localhost),
            )
            if self.control_url
            else None
        )
        self.stop_event = threading.Event()
        self.poll_thread: Optional[threading.Thread] = None

    def capabilities(self) -> Dict[str, Any]:
        return {
            "schema_version": 1,
            "agent_version": self.composer_version,
            "protocol_versions": [1],
            "capabilities": [
                "dlux.image_update",
                "dlux.backup.create",
                "composer.restart",
                "composer.recovery_deploy",
                "agent.rotate_credentials",
                "dlux.snapshot",
            ],
        }

    def ensure_enrolled(self) -> Optional[Dict[str, str]]:
        credentials = self.store.load_credentials()
        revoked = bool(self.store.get_meta("revoked"))
        if (credentials and not revoked) or not self.client or not self.enrollment_token:
            return credentials
        credentials = self.client.enroll(self.enrollment_token, self.capabilities())
        self.store.save_credentials(credentials["agent_id"], credentials["secret"])
        self.store.set_meta("enrolled_at", utc_now())
        self.store.set_meta("revoked", "")
        return credentials

    def _poll_control_plane(self):
        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                credentials = self.ensure_enrolled()
                if not self.client or not credentials:
                    self.stop_event.wait(5)
                    continue
                command = self.client.next_command(credentials, wait_seconds=25)
                self.store.set_meta("last_contact_at", utc_now())
                self.store.set_meta("revoked", "")
                backoff = 1.0
                if command:
                    self.store.enqueue_command(validate_command(command))
            except ProtocolError as exc:
                self.store.set_meta("last_protocol_error", redact_text(exc))
            except ControlPlaneError as exc:
                if exc.status in {401, 403}:
                    self.store.set_meta("revoked", utc_now())
                    backoff = max(backoff, 30.0)
                self.store.set_meta("last_connection_error", redact_text(exc))
                self.stop_event.wait(backoff + random.random())
                backoff = min(60.0, backoff * 2)
            except Exception as exc:
                self.store.set_meta("last_connection_error", redact_text(exc))
                self.stop_event.wait(backoff + random.random())
                backoff = min(60.0, backoff * 2)

    def start_poller(self):
        if not self.client or self.args.once:
            return
        self.poll_thread = threading.Thread(
            target=self._poll_control_plane,
            name="composer-control-poller",
            daemon=True,
        )
        self.poll_thread.start()

    def _atomic_json(self, path: Path, payload: Dict[str, Any]):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def _bridge_request(self, command: Dict[str, Any]):
        operation_id = command["operation_id"]
        request = {
            "schema_version": 1,
            "operation_id": operation_id,
            "action": command["action"],
            "created_at": command["created_at"] or utc_now(),
            "actor": command["actor"],
            "payload": command["payload"],
        }
        self._atomic_json(self.bridge_requests / f"{operation_id}.json", request)

    @staticmethod
    def _safe_bridge_detail(value: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {
            "status",
            "message",
            "error",
            "image_token",
            "target_version",
            "backup_token",
            "updated_at",
        }
        detail = {}
        for key in allowed:
            if key in value:
                item = value[key]
                detail[key] = redact_text(item) if isinstance(item, str) else item
        return detail

    def process_bridge_results(self):
        for command in self.store.running_commands():
            if command["action"] not in {"dlux.image_update", "dlux.backup.create"}:
                continue
            operation_id = command["operation_id"]
            path = self.bridge_results / f"{operation_id}.json"
            try:
                raw = path.read_bytes()
                value = json.loads(raw.decode("utf-8"))
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            if not isinstance(value, dict) or str(value.get("operation_id")) != operation_id:
                continue
            digest = hashlib.sha256(raw).hexdigest()
            meta_key = f"bridge_result:{operation_id}"
            if self.store.get_meta(meta_key) == digest:
                continue
            self.store.set_meta(meta_key, digest)
            status = str(value.get("status") or "").strip().lower()
            detail = self._safe_bridge_detail(value)
            if status in {"completed", "succeeded"}:
                self.store.transition(operation_id, "succeeded", detail)
            elif status in {"failed", "rejected"}:
                self.store.transition(operation_id, "failed", detail)
            else:
                self.store.transition(operation_id, "running", detail)

    def publish_snapshot(self):
        path = self.bridge_dir / "snapshot.json"
        try:
            raw = path.read_bytes()
            snapshot = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return
        if not isinstance(snapshot, dict):
            return
        digest = hashlib.sha256(raw).hexdigest()
        if self.store.get_meta("snapshot_digest") == digest:
            return
        self.store.set_meta("snapshot_digest", digest)
        clean = {
            "schema_version": 1,
            "observed_at": str(snapshot.get("observed_at") or utc_now())[:64],
            "project": snapshot.get("project") if isinstance(snapshot.get("project"), dict) else {},
            "versions": snapshot.get("versions") if isinstance(snapshot.get("versions"), dict) else {},
            "health": snapshot.get("health") if isinstance(snapshot.get("health"), dict) else {},
            "resources": snapshot.get("resources") if isinstance(snapshot.get("resources"), dict) else {},
            "backup": snapshot.get("backup") if isinstance(snapshot.get("backup"), dict) else {},
            "updates": snapshot.get("updates") if isinstance(snapshot.get("updates"), dict) else {},
            "agent": self.capabilities(),
        }
        self.store.queue_outbox("snapshot", clean)

    def flush_outbox(self):
        if not self.client:
            return
        credentials = self.store.load_credentials()
        if not credentials:
            return
        for item in self.store.pending_outbox():
            try:
                if item["kind"] == "event":
                    self.client.post_event(credentials, item["operation_id"], item["body"])
                elif item["kind"] == "snapshot":
                    self.client.post_snapshot(credentials, item["body"])
                elif item["kind"] == "local_operation":
                    self.client.post_local_operation(credentials, item["body"])
                elif item["kind"] == "capabilities":
                    self.client.put_capabilities(credentials, item["body"])
                else:
                    self.store.acknowledge_outbox(item["id"])
                    continue
            except ControlPlaneError as exc:
                self.store.set_meta("last_connection_error", redact_text(exc))
                if (
                    exc.status == 409
                    and item["kind"] == "event"
                    and item["body"].get("state") == "accepted"
                ):
                    self.store.set_command_state(item["operation_id"], "cancelled")
                    self.store.acknowledge_outbox(item["id"])
                break
            self.store.acknowledge_outbox(item["id"])
            self.store.set_meta("last_contact_at", utc_now())

    def _deadline_expired(self, value: str) -> bool:
        if not value:
            return False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed <= datetime.now(timezone.utc)
        except ValueError:
            return True

    def _child_env(self, operation_id: str) -> dict:
        env = os.environ.copy()
        env["COMPOSER_OPERATION_ID"] = operation_id
        excluded = parse_service_list(env.get("COMPOSER_EXCLUDE_SERVICES"))
        for service in ("composer-agent", "composer-updater", "docker-socket-proxy"):
            if service not in excluded:
                excluded.append(service)
        env["COMPOSER_EXCLUDE_SERVICES"] = join_service_list(excluded)
        if self.args.status_file:
            env["COMPOSER_STATUS_FILE"] = self.args.status_file
        if self.args.log_file:
            env["COMPOSER_LOG_FILE"] = self.args.log_file
        return env

    def _run_child(self, command: Dict[str, Any]) -> tuple[int, str]:
        operation_id = command["operation_id"]
        action = command["action"]
        env = self._child_env(operation_id)
        argv = [sys.executable, "-m", "composer"]
        if action == "composer.restart":
            service = command["payload"]["service"]
            protected = PROTECTED_RESTART_SERVICES | set(
                parse_service_list(env.get("COMPOSER_EXCLUDE_SERVICES"))
            )
            allowed = [
                item
                for item in parse_service_list(os.environ.get("COMPOSER_AGENT_RESTART_SERVICES"))
                if item not in protected
            ]
            if service in protected:
                return 2, f"Service '{service}' is protected from remote restart."
            if service and service not in allowed:
                return 2, f"Service '{service}' is not in COMPOSER_AGENT_RESTART_SERVICES."
            if not service and not allowed:
                return 2, "Project restart is disabled because no restart allowlist is configured."
            argv.append("restart")
            if service:
                argv.append(service)
            else:
                env["COMPOSER_RESTART_SERVICES"] = join_service_list(allowed)
        else:
            argv.append("-u")
            if command["payload"].get("force"):
                argv.append("--force")
        if self.args.dev:
            argv.append("-d")
        if self.args.file:
            argv.extend(["-f", self.args.file])
        try:
            return subprocess.run(argv, env=env).returncode, ""
        except OSError as exc:
            return 127, f"Composer process could not start: {exc}"

    def execute_received_command(self):
        if self.store.has_running_command():
            return
        command = self.store.accepted_command()
        if command and self.client and self.store.has_pending_event(command["operation_id"], "accepted"):
            return
        if command is None:
            command = self.store.next_received()
            if not command:
                return
            operation_id = command["operation_id"]
            if self._deadline_expired(command.get("deadline_at", "")):
                self.store.transition(operation_id, "failed", {"error": "Command deadline expired."})
                return
            self.store.transition(operation_id, "accepted")
            if self.client:
                return
        if not command:
            return
        operation_id = command["operation_id"]
        if command["action"] in {"dlux.image_update", "dlux.backup.create"}:
            self._bridge_request(command)
            self.store.transition(operation_id, "running", {"phase": "awaiting_dlux"})
            return
        if command["action"] == "agent.rotate_credentials":
            self._rotate_credentials(operation_id)
            return
        self.store.transition(operation_id, "running")
        exit_code, error = self._run_child(command)
        detail = {"exit_code": exit_code}
        if error:
            detail["error"] = redact_text(error)
        if self.args.log_file:
            try:
                detail["log"] = redact_text(Path(self.args.log_file).read_text(encoding="utf-8"))
            except OSError:
                pass
        self.store.transition(operation_id, "succeeded" if exit_code == 0 else "failed", detail)

    def _rotate_credentials(self, operation_id: str):
        credentials = self.store.load_credentials()
        if not self.client or not credentials:
            self.store.transition(operation_id, "failed", {"error": "Agent is not enrolled."})
            return
        self.store.transition(operation_id, "running")
        try:
            rotated = self.client.begin_rotation(credentials)
            self.store.stage_credentials(
                operation_id,
                rotated["agent_id"],
                rotated["secret"],
                rotated["rotation_id"],
            )
        except (ControlPlaneError, OSError) as exc:
            self.store.transition(operation_id, "failed", {"error": redact_text(exc)})
            return
        self.process_pending_rotation()

    def process_pending_rotation(self):
        pending = self.store.pending_credentials()
        if not pending or not self.client:
            return
        credentials = {"agent_id": pending["agent_id"], "secret": pending["secret"]}
        try:
            self.client.confirm_rotation(credentials, pending["rotation_id"])
        except ControlPlaneError as exc:
            self.store.set_meta("last_connection_error", redact_text(exc))
            if exc.status in {400, 409}:
                self.store.clear_pending_credentials()
                self.store.transition(
                    pending["operation_id"],
                    "failed",
                    {"error": "Credential rotation expired or was rejected."},
                )
            return
        self.store.promote_pending_credentials()
        self.store.transition(pending["operation_id"], "succeeded")

    def publish_capabilities(self):
        capabilities = self.capabilities()
        digest = hashlib.sha256(
            json.dumps(capabilities, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if self.store.get_meta("capabilities_digest") == digest:
            return
        self.store.set_meta("capabilities_digest", digest)
        self.store.queue_outbox("capabilities", capabilities)

    def process_local_update(self):
        request = self.watch.pending_request()
        if not request:
            return
        operation_id = str(request.get("operation_id") or "").strip()
        if operation_id and self.store.command_state(operation_id):
            self.store.transition(operation_id, "running", {"phase": "composer_deploy"})
        exit_code = self.watch.process(request)
        if operation_id and self.store.command_state(operation_id):
            self.store.transition(
                operation_id,
                "running",
                {"phase": "awaiting_dlux_finalization", "composer_exit_code": exit_code},
            )
            return
        token = str(request.get("token") or "")
        local_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"composer-local:{token}"))
        self.store.queue_outbox(
            "local_operation",
            {
                "schema_version": 1,
                "operation_id": local_id,
                "action": "dlux.image_update",
                "source": "local",
                "request_token": token,
                "state": "succeeded" if exit_code == 0 else "failed",
                "exit_code": exit_code,
                "observed_at": utc_now(),
            },
            local_id,
        )

    def run_once(self):
        self.watch.maybe_check_availability()
        self.process_pending_rotation()
        self.process_local_update()
        self.process_bridge_results()
        self.publish_snapshot()
        self.publish_capabilities()
        self.execute_received_command()
        self.flush_outbox()

    def run(self) -> int:
        if self.args.once and self.client:
            try:
                credentials = self.ensure_enrolled()
                if credentials:
                    command = self.client.next_command(credentials, wait_seconds=0)
                    if command:
                        self.store.enqueue_command(validate_command(command))
            except (ControlPlaneError, ProtocolError, ValueError) as exc:
                self.store.set_meta("last_connection_error", redact_text(exc))
        self.start_poller()
        print(
            f"👁 composer agent — trigger={self.watch.trigger}"
            + (f" · control={self.control_url}" if self.control_url else " · local-only"),
            flush=True,
        )
        try:
            while True:
                self.run_once()
                if self.args.once:
                    return 0
                self.stop_event.wait(max(1.0, float(self.args.interval)))
        except KeyboardInterrupt:
            return 130
        finally:
            self.stop_event.set()
            if self.poll_thread:
                self.poll_thread.join(timeout=2)


def run_agent(args) -> int:
    try:
        return ComposerAgent(args).run()
    except (OSError, ValueError) as exc:
        print(f"✖ composer agent: {redact_text(exc)}", file=sys.stderr)
        return 2
