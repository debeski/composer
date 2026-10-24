"""Named operations DjangoLux asks the resident Composer to perform.

DjangoLux's Operations card writes `state/ops-request.json` naming ONE operation
from ``HANDLERS`` below; this module performs it and publishes
`state/ops-result.json` plus `ops-request.json.ack`, both carrying the request's
token so a stale document can never be read as the current answer. The shape is
the package-update handoff's, for the same reason: it already survives
restarts, crashes and a request nobody is left to answer.

Phase 1 is read-only. An operation that is not in ``HANDLERS`` is refused with a
message rather than attempted, and nothing here takes an argument from the
request beyond the operation's own name — the request cannot carry a command,
a path or a flag, so there is nothing for a compromised database to smuggle
through. See the Operations Centre plan in the DjangoLux repository.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .agent_protocol import redact_text

REQUEST_FILENAME = "ops-request.json"
ACK_FILENAME = f"{REQUEST_FILENAME}.ack"
RESULT_FILENAME = "ops-result.json"

#: A result document is rendered in a browser; a runaway check must not become a
#: multi-megabyte payload on the runtime volume.
MAX_FINDINGS = 200
MAX_MESSAGE_CHARS = 2000
#: A diff is shown in a browser and stored on the volume; cap it hard.
MAX_DIFF_CHARS = 20000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _trim(findings) -> list:
    """Bound and redact what a result carries back to the browser."""
    clean = []
    for finding in (findings or [])[:MAX_FINDINGS]:
        if not isinstance(finding, dict):
            continue
        entry = {
            "level": str(finding.get("level") or "ok")[:16],
            "name": str(finding.get("name") or "")[:128],
            "message": redact_text(str(finding.get("message") or ""))[:MAX_MESSAGE_CHARS],
        }
        if finding.get("fix"):
            entry["fix"] = redact_text(str(finding["fix"]))[:MAX_MESSAGE_CHARS]
        clean.append(entry)
    return clean


def _run_check(runtime) -> dict:
    """`composer check` plus the repairs it would apply. Read-only: never `--fix`.

    One operation on purpose. Splitting "what is wrong" from "what would fix it"
    made the card ask the operator to run two things to learn one answer, and the
    apply needs the preview's digest anyway.
    """
    from types import SimpleNamespace

    from .launcher import DockerComposeLauncher

    launcher = DockerComposeLauncher()
    args = SimpleNamespace(
        file=getattr(runtime.args, "file", None),
        dev=getattr(runtime.args, "dev", False),
        fix=False, yes=False, deep=False,
        deep_service="web", deep_command="python manage.py dlux_doctor",
        json=True, beta=False, stable=False,
    )
    results, _fixed = launcher.collect_checkup(args)
    repairs, digest = _dry_run_repairs(launcher, args)
    return {
        "exit_code": 1 if any(r.get("level") == "fail" for r in results) else 0,
        "composer_version": launcher.composer_version,
        "findings": _trim(results),
        "repairs": repairs,
        "compose_digest": digest,
    }



def compose_digest(launcher) -> str:
    """Fingerprint of the deployment files a preview was computed from.

    An apply must not write a diff nobody saw: DjangoLux hands this value back,
    and a file edited in between makes the digests disagree and the apply refuse.
    """
    import hashlib

    from .maintenance_migration import CADDYFILE, LEGACY_PAGE, NGINX_TEMPLATE

    digest = hashlib.sha256()
    # The proxy files too: the maintenance-page repair rewrites them.
    proxy_files = [str(path) for path in (CADDYFILE, NGINX_TEMPLATE, LEGACY_PAGE) if path.is_file()]
    for name in sorted(launcher.active_compose_files or []) + proxy_files:
        digest.update(name.encode("utf-8"))
        try:
            digest.update(Path(name).read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
    return digest.hexdigest()


#: The guarded transforms `check --fix` runs that can also be previewed. Each is
#: dry-run first by contract (`apply=False` computes the candidate and diffs it).
_PREVIEWABLE = (
    ("agent-enable", "enable_agent"),
    ("executor-enable", "enable_executor"),
    ("resident-block", "enable_executor"),
    ("post-start-label", "enable_post_start_label"),
    ("dlux-runtime", "migrate_dlux_updater"),
    ("restart-labels", "normalize_restart_labels"),
)


def _checkup_args(runtime, *, fix=False):
    from types import SimpleNamespace

    return SimpleNamespace(
        file=getattr(runtime.args, "file", None),
        dev=getattr(runtime.args, "dev", False),
        fix=fix, yes=True, deep=False,
        deep_service="web", deep_command="python manage.py dlux_doctor",
        json=True, beta=False, stable=False,
    )


def _preview_fixes(runtime) -> dict:
    """Kept for DjangoLux 1.9.2, whose card asks for the preview separately."""
    return _run_check(runtime)


def _dry_run_repairs(launcher, args):
    """(repairs, compose digest) — what `check --fix` would change. Writes nothing."""
    from . import agent_installer

    compose_file = args.file or ""
    repairs = []
    seen_diffs = set()
    for name, helper in _PREVIEWABLE:
        function = getattr(agent_installer, helper, None)
        if function is None:
            continue
        try:
            outcome = function(".", compose_file=compose_file, apply=False, include_diff=True)
        except Exception as exc:  # a transform that refuses is not a crash
            repairs.append({"name": name, "files": [], "diff": "", "note": redact_text(str(exc))[:MAX_MESSAGE_CHARS]})
            continue
        if not outcome.get("files"):
            continue
        diff = redact_text(str(outcome.get("diff") or ""))[:MAX_DIFF_CHARS]
        if diff and diff in seen_diffs:
            # Two helpers can produce the same repair (the resident block is
            # reached through executor enable); show it once.
            continue
        seen_diffs.add(diff)
        repairs.append({
            "name": name,
            "files": [str(f) for f in outcome.get("files") or []],
            "diff": diff,
            "note": "; ".join(str(w) for w in outcome.get("warnings") or [])[:MAX_MESSAGE_CHARS],
        })
    repairs.extend(_maintenance_repair(launcher))
    return repairs, compose_digest(launcher)


def _maintenance_repair(launcher):
    """The stack schema 3 page move, which `check --fix` runs outside agent_installer."""
    from . import maintenance_migration

    try:
        outcome = maintenance_migration.preview(".", launcher._maintenance_compose_files())
    except Exception as exc:
        return [{"name": "maintenance-page", "files": [], "diff": "",
                 "note": redact_text(str(exc))[:MAX_MESSAGE_CHARS]}]
    if not outcome["files"]:
        return []
    return [{
        "name": "maintenance-page",
        "files": outcome["files"],
        "diff": redact_text(outcome["diff"])[:MAX_DIFF_CHARS],
        "note": redact_text(outcome["note"])[:MAX_MESSAGE_CHARS],
    }]


def _apply_fixes(runtime, request) -> dict:
    """Run `check --fix` for real, but only against the files the preview saw."""
    from .launcher import DockerComposeLauncher

    expected = str(request.get("compose_digest") or "").strip().lower()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise ValueError("This repair must be previewed before it is applied.")
    launcher = DockerComposeLauncher()
    args = _checkup_args(runtime, fix=True)
    # resolve_active_compose_files() is what fills the file list the digest is
    # taken over; collect_checkup does it too, but the guard has to come first.
    launcher.compose_file = args.file
    launcher.dev_mode = args.dev
    launcher.resolve_active_compose_files()
    actual = compose_digest(launcher)
    if actual != expected:
        raise ValueError(
            "The deployment files changed since the preview, so the repair was not "
            "applied. Run the preview again and review the new changes."
        )
    # Both resident services mount the project read-only; the executor is the
    # only one that can start a container able to write it. Delegate when it is
    # there, and fall back to applying in-process on a legacy agent-only stack
    # (where this process IS the one with Docker authority).
    from . import executor_client

    if executor_client.executor_configured():
        import uuid

        exit_code, detail = executor_client.run_operation(
            "check_fix", {"compose_digest": expected}, operation_id=str(uuid.uuid4()),
        )
        if exit_code != 0:
            raise ValueError(detail or f"The repair could not be applied (exit {exit_code}).")
        launcher = DockerComposeLauncher()
        results, fixed = launcher.collect_checkup(_checkup_args(runtime))
    else:
        results, fixed = launcher.collect_checkup(args)
    return {
        "exit_code": 1 if any(r.get("level") == "fail" for r in results) else 0,
        "composer_version": launcher.composer_version,
        "findings": _trim(results),
        "repairs": [
            {"name": str(f.get("name") or ""), "files": [], "diff": "",
             "note": redact_text(str(f.get("message") or ""))[:MAX_MESSAGE_CHARS]}
            for f in fixed
        ],
        "compose_digest": compose_digest(launcher),
    }



def _resident_update_status(runtime=None) -> dict:
    """Is a newer resident Composer published on this project's Composer channel?

    Read-only, and registry-only: the version label on the channel tag against
    the version this process already is. DjangoLux asks this *before* offering
    the update, because being made to type a password only to be told the pair
    is already current is a bad way to learn it.

    Never raises. A registry that cannot be reached is reported as "unknown",
    which the card shows as "could not check" — not as an update, and not as
    being on the latest.
    """
    from . import channel_config, registry
    from .version import read_composer_version
    from .versions import try_parse

    current = read_composer_version()
    image = channel_config.resolve_self_image()
    channel = channel_config.read_channel()
    published = registry.remote_image_version(image) or ""
    known = bool(published)
    here, there = try_parse(current), try_parse(published)
    available = bool(known and here and there and there > here)
    return {
        "version": current,
        "image": image,
        "channel": channel,
        "published_version": published,
        "update_available": available,
        "checked": known,
    }


def _check_resident(runtime) -> dict:
    """The `agent-check` operation: resident Composer version vs its channel."""
    resident = _resident_update_status(runtime)
    if not resident["checked"]:
        message = (
            f"Composer {resident['version']} is resident. The published version of "
            f"{resident['image']} could not be read, so whether an update exists is unknown."
        )
        level = "warn"
    elif resident["update_available"]:
        message = (
            f"Composer {resident['published_version']} is published on the "
            f"{resident['channel']} channel; {resident['version']} is resident."
        )
        level = "warn"
    elif resident["published_version"] == resident["version"]:
        message = f"Composer {resident['version']} is the {resident['channel']} channel's current version."
        level = "ok"
    else:
        # A pin, or a channel switched after an update, leaves the pair AHEAD of
        # the tag. That is not an update, and saying "current version" for a
        # version the channel does not publish would be false.
        message = (
            f"Composer {resident['version']} is resident; the {resident['channel']} "
            f"channel publishes {resident['published_version']}."
        )
        level = "ok"
    return {
        "exit_code": 0,
        "composer_version": resident["version"],
        "findings": _trim([{"name": "resident-composer", "level": level, "message": message}]),
        "resident": resident,
    }


def _update_resident_pair(runtime, request, self_request_path=None) -> dict:
    """Update composer-agent and composer-executor to the channel's image.

    This operation ends by replacing the very processes that would report it, so
    the executor starts a DETACHED helper from the current image that runs
    `composer agent update` and then writes this run's ack and result itself.
    The pair can be recreated underneath it; the answer still lands.
    """
    from . import executor_client

    token = str(request.get("token") or "")
    resident = _resident_update_status(runtime)
    if resident["checked"] and not resident["update_available"]:
        raise ValueError(
            f"The resident Composer is already {resident['version']}, the current "
            f"version on the {resident['channel']} channel. Nothing to update."
        )
    if not executor_client.executor_configured():
        raise ValueError(
            "Updating the resident Composer needs composer-executor, which this "
            "deployment does not define. Run './start.sh agent update' on the host."
        )
    import uuid

    exit_code, detail = executor_client.run_operation(
        "agent_update", {"token": token}, operation_id=str(uuid.uuid4()),
    )
    if exit_code != 0:
        raise ValueError(detail or f"The resident Composer update could not start (exit {exit_code}).")
    # The helper owns this request now, so take it off the volume: the update
    # recreates THIS container, and the agent that comes up in its place would
    # otherwise find the request still pending and answer it — which is exactly
    # what happened on the first live run, where the new (older) agent replied
    # "does not perform the operation" over a run that was proceeding fine.
    # DjangoLux matches the ack by token and needs no request file to wait.
    try:
        os.unlink(self_request_path)
    except OSError:
        pass
    return {"deferred": True}


#: Lines a log request may ask for, and the characters a result may carry back.
LOG_LINES_DEFAULT = 200
LOG_LINES_MAX = 1000
MAX_LOG_CHARS = 60000


def _stack_launcher(runtime):
    from .launcher import DockerComposeLauncher

    launcher = DockerComposeLauncher()
    launcher.compose_file = getattr(runtime.args, "file", None)
    launcher.dev_mode = getattr(runtime.args, "dev", False)
    launcher.resolve_active_compose_files()
    return launcher


def _parse_ps(text: str) -> dict:
    """`docker compose ps --format json`: one object per line, or one array."""
    rows = []
    text = (text or "").strip()
    if text.startswith("["):
        try:
            rows = json.loads(text)
        except ValueError:
            rows = []
    else:
        for line in text.splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    states = {}
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("Service"):
            states[row["Service"]] = {
                "state": str(row.get("State") or ""),
                "health": str(row.get("Health") or ""),
                "status": str(row.get("Status") or "")[:120],
            }
    return states


def _service_table(runtime=None, *, launcher=None) -> list:
    """Every Compose service, its state, and whether a request may restart it."""
    from .executor_ops import _restart_policy
    from .service_selection import PROTECTED_LIFECYCLE_SERVICES

    launcher = launcher or _stack_launcher(runtime)
    ok, out, err = launcher.run_docker_compose(["config", "--format", "json"], timeout=60)
    if not ok:
        raise ValueError(redact_text(err or out or "docker compose config failed.")[:MAX_MESSAGE_CHARS])
    try:
        services = json.loads(out).get("services") or {}
    except (ValueError, AttributeError):
        raise ValueError("docker compose config returned invalid JSON.")
    _ok, ps_out, _err = launcher.run_docker_compose(["ps", "--all", "--format", "json"], timeout=60)
    states = _parse_ps(ps_out)
    protected, allowed = _restart_policy(os.environ)
    table = []
    for name in sorted(services):
        labels = (services[name] or {}).get("labels") or {}
        label = str(labels.get("org.dlux.restart") or "") if isinstance(labels, dict) else ""
        state = states.get(name, {"state": "absent", "health": "", "status": ""})
        can_restart = name in allowed and name not in protected and label != "protected"
        can_lifecycle = can_restart and name not in PROTECTED_LIFECYCLE_SERVICES
        table.append({
            "name": name,
            **state,
            "restart_label": label,
            "can_restart": can_restart,
            "can_lifecycle": can_lifecycle,
            "can_start": can_lifecycle and state["state"] in ("created", "exited", "stopped"),
            "can_stop": can_lifecycle and state["state"] in ("running", "restarting", "paused"),
        })
    return table


def _list_services(runtime) -> dict:
    return {"exit_code": 0, "services": _service_table(runtime), "findings": []}


def _requested_service(runtime, request, *, restartable: bool, action="") -> str:
    service = str(request.get("service") or "").strip()
    table = {row["name"]: row for row in _service_table(runtime)}
    if service not in table:
        raise ValueError(f"'{service}' is not a service of this deployment.")
    if restartable and not table[service]["can_restart"]:
        raise ValueError(
            f"'{service}' may not be restarted remotely (protected, or not in COMPOSER_AGENT_RESTART_SERVICES)."
        )
    if action in ("start", "stop") and not table[service][f"can_{action}"]:
        raise ValueError(f"Remote {action} is unavailable for '{service}' (protected or incompatible current state).")
    return service


def _restart_service(runtime, request, *, op: str) -> dict:
    """Control one allow-listed service through the executor."""
    import uuid

    from . import executor_client

    service = _requested_service(runtime, request, restartable=True, action=op)
    if not executor_client.executor_configured():
        raise ValueError("Controlling a service needs composer-executor, which this deployment does not define.")
    exit_code, detail = executor_client.run_operation(op, {"service": service}, operation_id=str(uuid.uuid4()))
    verb = {"start": "Started", "stop": "Stopped", "recreate": "Recreated", "restart": "Restarted"}[op]
    if exit_code != 0:
        raise ValueError(redact_text(detail or f"Could not {op} {service} (exit {exit_code}).")[:MAX_MESSAGE_CHARS])
    return {
        "exit_code": 0,
        "service": service,
        "findings": [{"name": f"service-{op}", "level": "ok", "message": f"{verb} {service}."}],
    }


def _merge_logs(text: str, lines: int) -> str:
    """`docker compose logs --timestamps` for several services, as one timeline.

    Each service's tail arrives in its own block; sorting on the timestamp makes
    them one stream, and only the newest ``lines`` of it are kept.
    """
    entries = []
    for raw in (text or "").splitlines():
        service, sep, rest = raw.partition("|")
        if not sep:
            continue
        stamp, _space, message = rest.strip().partition(" ")
        entries.append((stamp, service.strip(), message))
    entries.sort(key=lambda entry: entry[0])
    width = max((len(service) for _stamp, service, _message in entries), default=0)
    return "\n".join(f"{service.ljust(width)} | {message}" for _stamp, service, message in entries[-lines:])


def _tail_logs(runtime, request) -> dict:
    """The last lines of one service's logs, or of all of them merged in time order."""
    every = not str(request.get("service") or "").strip()
    service = "" if every else _requested_service(runtime, request, restartable=False)
    try:
        lines = int(request.get("lines") or LOG_LINES_DEFAULT)
    except (TypeError, ValueError):
        lines = LOG_LINES_DEFAULT
    lines = max(1, min(LOG_LINES_MAX, lines))
    launcher = _stack_launcher(runtime)
    args = (["logs", "--no-color", "--timestamps", "--tail", str(lines)] if every
            else ["logs", "--no-color", "--no-log-prefix", "--tail", str(lines), service])
    ok, out, err = launcher.run_docker_compose(args, timeout=60)
    if not ok:
        raise ValueError(redact_text(err or out or "Could not read the logs.")[:MAX_MESSAGE_CHARS])
    text = redact_text(_merge_logs(out, lines) if every else (out or ""))
    if len(text) > MAX_LOG_CHARS:
        text = text[-MAX_LOG_CHARS:]
    return {"exit_code": 0, "service": service, "lines": lines, "logs": text, "findings": []}


#: operation name -> callable. Nothing outside this table can run.
HANDLERS = {
    "check": lambda runtime, request: _run_check(runtime),
    "agent-check": lambda runtime, request: _check_resident(runtime),
    "check-fix-preview": lambda runtime, request: _preview_fixes(runtime),
    "check-fix-apply": _apply_fixes,
    "agent-update": lambda runtime, request: _update_resident_pair(
        runtime, request, Path(runtime.package_trigger.parent) / REQUEST_FILENAME,
    ),
    "services": lambda runtime, request: _list_services(runtime),
    "service-restart": lambda runtime, request: _restart_service(runtime, request, op="restart"),
    "service-start": lambda runtime, request: _restart_service(runtime, request, op="start"),
    "service-stop": lambda runtime, request: _restart_service(runtime, request, op="stop"),
    "service-recreate": lambda runtime, request: _restart_service(runtime, request, op="recreate"),
    "logs-tail": _tail_logs,
}


class OpsResponder:
    """Answers one DjangoLux operation request per tick, at most once each."""

    def __init__(self, runtime):
        self.runtime = runtime
        state_dir = runtime.package_trigger.parent
        self.request = state_dir / REQUEST_FILENAME
        self.ack = state_dir / ACK_FILENAME
        self.result = state_dir / RESULT_FILENAME
        self.last_token: Optional[str] = str(_read_json(self.ack).get("token") or "") or None

    def pending(self) -> Optional[dict]:
        request = _read_json(self.request)
        token = str(request.get("token") or "").strip()
        if not token or token == self.last_token:
            return None
        return request

    def answer(self) -> Optional[str]:
        """Perform a pending operation and publish its result. Never raises."""
        request = self.pending()
        if request is None:
            return None
        token = str(request["token"])
        operation = str(request.get("operation") or "").strip().lower()
        handler = HANDLERS.get(operation)
        error = ""
        result = {"exit_code": 2, "findings": []}
        if handler is None:
            error = (
                f"This Composer does not perform the operation {operation!r}. "
                "Update Composer, or run it on the host."
            )
        else:
            try:
                answer = handler(self.runtime, request)
            except ValueError as exc:
                # A refusal: the operator is told why, and nothing was written.
                error = redact_text(str(exc))[:MAX_MESSAGE_CHARS]
            except Exception as exc:  # noqa: BLE001 - reported, never raised at the loop
                error = f"The operation failed: {redact_text(str(exc))}"[:MAX_MESSAGE_CHARS]
            else:
                if isinstance(answer, dict):
                    result = answer
                else:
                    error = f"The operation {operation!r} returned no usable result."

        if isinstance(result, dict) and result.pop("deferred", False) and not error:
            # The helper container owns this answer. Remember the token so the
            # loop does not start it twice while it runs.
            self.last_token = token
            return token
        payload = {
            "schema_version": 1,
            "token": token,
            "operation": operation,
            "finished_at": _now(),
            "error": error,
            **result,
        }
        # Result first, then the ack: DjangoLux reads the result only after it
        # sees the ack, so an ack can never point at a result that is not there.
        try:
            _atomic_json(self.result, payload)
            _atomic_json(self.ack, {
                "token": token,
                "operation": operation,
                "exit_code": payload.get("exit_code", 0),
                "error": error,
                "finished_at": payload["finished_at"],
            })
        except OSError as exc:
            print(f"⚠ operation {operation} could not be published: {redact_text(str(exc))}", flush=True)
            return None
        self.last_token = token
        return token
