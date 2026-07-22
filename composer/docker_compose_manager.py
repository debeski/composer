import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .constants import (
    DEFAULT_RESIDENT_SERVICE,
    ENV_NAME_RE,
    INHERITED_SECRET_KEYS_ENV,
    SERVICE_FAILED,
    SERVICE_HEALTHY,
    SERVICE_NOT_SEEN,
    SERVICE_STARTING,
)
from .output_utils import OutputUtilsMixin
from .subprocess_runner import SubprocessRunnerMixin


class DockerComposeMixin(OutputUtilsMixin, SubprocessRunnerMixin):
    def resident_secret_keys(self) -> List[str]:
        loaded = list(getattr(self, "loaded_secrets", []) or [])
        raw_inherited = os.environ.get(INHERITED_SECRET_KEYS_ENV, "")
        inherited = [key.strip() for key in raw_inherited.split(",")]
        return sorted(
            {
                key
                for key in loaded + inherited
                if ENV_NAME_RE.fullmatch(key) and key in os.environ
            }
        )

    def build_compose_base_args(self) -> List[str]:
        base_args = []
        for file in self.active_compose_files:
            base_args.extend(["-f", file])

        if self.compose_runtime_override and self.compose_runtime_override.exists():
            base_args.extend(["-f", str(self.compose_runtime_override)])
        return base_args

    def build_compose_env(self) -> Dict[str, str]:
        env = os.environ.copy()
        if self.dev_mode and not self.compose_file:
            env["NGINX_PORT"] = "81"
        if self.dev_mode:
            # Dev mode forces debug on, overriding any DEBUG value the
            # environment/compose may carry (covers ${DEBUG} interpolation).
            env["DEBUG"] = "True"
            env["DEBUG_STATUS"] = "True"
        env["COMPOSER_VERSION"] = self.composer_version
        env.setdefault("BUILDKIT_PROGRESS", "plain")
        return env

    def get_compose_commands(self, args: List[str]) -> List[List[str]]:
        base_args = self.build_compose_base_args()
        return [
            ["docker", "compose"] + base_args + args,
            ["docker-compose"] + base_args + args,
        ]

    def resolve_compose_cli(self) -> List[str]:
        """Pick the working Compose CLI once.

        Interactive exec/run inherit the terminal and can't inspect captured
        output, so the usual streaming fallback doesn't apply. Probe the plugin
        form quietly and fall back to the legacy `docker-compose` binary.
        """
        ok, out, err = self.run_command(["docker", "compose", "version"], timeout=10)
        if ok:
            return ["docker", "compose"]
        if self.should_fallback_to_docker_compose(out, err):
            return ["docker-compose"]
        return ["docker", "compose"]

    def exec_in_service(
        self,
        service: str,
        command: List[str],
        manage: bool = False,
        shell: bool = False,
        fresh: bool = False,
    ) -> int:
        """Run a command inside a Compose service, attached to the terminal.

        Defaults to `docker compose exec` (the running container); `fresh` uses
        `docker compose run --rm` for a one-off container. `manage` prepends the
        Django entrypoint; `shell` wraps the command in `sh -c`. Returns the
        child exit code.
        """
        cmd = list(command)
        if manage:
            cmd = ["python", "manage.py"] + cmd
        if shell:
            cmd = ["sh", "-c", " ".join(cmd)]

        interactive = sys.stdin.isatty() and sys.stdout.isatty()
        action = ["run", "--rm"] if fresh else ["exec"]
        if not interactive:
            # Disable TTY allocation for piped/non-interactive use (CI, scripts).
            action.append("-T")
        action.append(service)

        argv = self.resolve_compose_cli() + self.build_compose_base_args() + action + cmd
        return self.run_command_interactive(argv, env=self.build_compose_env())

    def should_fallback_to_docker_compose(self, stdout: str, stderr: str) -> bool:
        combined = f"{stdout}\n{stderr}".lower()
        if "is not a docker command" in combined:
            return True
        return (
            "no such file or directory" in combined
            and ("'docker'" in combined or '"docker"' in combined or "docker.exe" in combined)
        )

    def run_docker_compose(
        self,
        args: List[str],
        timeout: Optional[float] = None,
    ) -> Tuple[bool, str, str]:
        env = self.build_compose_env()
        commands = self.get_compose_commands(args)

        success, out, err = self.run_command(commands[0], timeout=timeout, env=env)
        if success or not self.should_fallback_to_docker_compose(out, err):
            return success, out, err
        return self.run_command(commands[1], timeout=timeout, env=env)

    def run_docker_compose_streaming(
        self,
        args: List[str],
        timeout: Optional[float] = None,
        progress_callback=None,
    ) -> Tuple[bool, str, str]:
        env = self.build_compose_env()
        commands = self.get_compose_commands(args)

        success, out, err = self.run_command_streaming(
            commands[0],
            timeout=timeout,
            env=env,
            progress_callback=progress_callback,
        )
        if success or not self.should_fallback_to_docker_compose(out, err):
            return success, out, err
        return self.run_command_streaming(
            commands[1],
            timeout=timeout,
            env=env,
            progress_callback=progress_callback,
        )

    def get_compose_ps_entries(self, include_all: bool = False) -> Tuple[bool, List[Dict[str, str]], str]:
        args = ["ps"]
        if include_all:
            args.append("--all")
        args.extend(["--format", "json"])

        ok, out, err = self.run_docker_compose(args, timeout=10)
        if not ok:
            return False, [], self.build_failure_detail(out, err)
        return True, self.parse_compose_json_output(out), ""

    def collect_service_diagnostics(self, include_logs: bool = True) -> str:
        ok, services, detail = self.get_compose_ps_entries(include_all=True)
        if not ok:
            return detail

        excluded = set(getattr(self, "exclude_services", []) or [])
        issues: List[str] = []
        failed_services: List[str] = []

        for service in services:
            name = service.get("Service") or service.get("Name") or "unknown"
            if name in excluded:
                continue
            state = str(service.get("State", "")).lower()
            health = str(service.get("Health", "")).lower()
            exit_code = str(service.get("ExitCode", "")).strip()

            if state in {"exited", "dead"} or health == "unhealthy":
                message = f"{name}: state={state or 'unknown'}"
                if health:
                    message += f", health={health}"
                if exit_code and exit_code != "0":
                    message += f", exit_code={exit_code}"
                issues.append(message)
                failed_services.append(name)
            elif state == "restarting":
                issues.append(f"{name}: state=restarting")

        sections: List[str] = []
        if issues:
            sections.append("Service state:\n" + "\n".join(f"- {issue}" for issue in issues))

        if include_logs:
            for service_name in failed_services[:3]:
                ok, out, err = self.run_docker_compose(
                    ["logs", "--no-color", "--tail", "25", service_name],
                    timeout=15,
                )
                summary = self.summarize_output(out, err, max_lines=12)
                if summary:
                    sections.append(f"{service_name} logs:\n{summary}")

        return "\n\n".join(section for section in sections if section.strip())

    def sync_runtime_compose_override(self) -> bool:
        if not self.services:
            self.remove_runtime_compose_override()
            return True

        if self.compose_runtime_override is None:
            try:
                # Compose reads overrides client-side, so this file does not
                # need to live in (or be writable through) the project mount.
                fd, path = tempfile.mkstemp(
                    prefix=".composer-runtime-",
                    suffix=".compose.yml",
                )
                os.close(fd)
                self.compose_runtime_override = Path(path)
            except OSError as exc:
                self.last_runtime_diagnostic = (
                    f"Failed to create Composer runtime override: {exc}"
                )
                return False

        lines = ["services:"]
        resident_service = os.environ.get(
            "COMPOSER_WATCH_SELF_SERVICE", DEFAULT_RESIDENT_SERVICE
        ).strip()
        secret_keys = self.resident_secret_keys()
        for service in self.services:
            service_env = [
                f"      COMPOSER_VERSION: {json.dumps(self.composer_version)}",
            ]
            if service == resident_service and secret_keys:
                service_env.append(
                    f"      {INHERITED_SECRET_KEYS_ENV}: "
                    f"{json.dumps(','.join(secret_keys))}"
                )
                service_env.extend(
                    f"      {json.dumps(key)}: {json.dumps(os.environ[key])}"
                    for key in secret_keys
                )
            if self.dev_mode:
                # Override file is applied last, so this wins over any DEBUG the
                # project's compose files declare for the service.
                service_env.append(f"      DEBUG: {json.dumps('True')}")
                service_env.append(f"      DEBUG_STATUS: {json.dumps('True')}")
            lines.extend(
                [
                    f"  {service}:",
                    "    environment:",
                    *service_env,
                ]
            )

        try:
            self.compose_runtime_override.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception as exc:
            self.last_runtime_diagnostic = f"Failed to write Composer runtime override: {exc}"
            return False

        return True

    def remove_runtime_compose_override(self):
        if not self.compose_runtime_override:
            return
        try:
            self.compose_runtime_override.unlink(missing_ok=True)
        except Exception:
            pass
        self.compose_runtime_override = None

    def discover_services(self, silent: bool = False) -> bool:
        ok, out, err = self.run_docker_compose(["config", "--services"], timeout=10)
        if not ok:
            if not silent:
                self.last_runtime_diagnostic = self.build_failure_detail(out, err)
            return False
        discovered = [s for s in out.splitlines() if s]
        excluded = set(getattr(self, "exclude_services", []) or [])
        self.services = [s for s in discovered if s not in excluded]
        if excluded and discovered and not self.services:
            self.last_runtime_diagnostic = (
                "No compose services remain after COMPOSER_EXCLUDE_SERVICES="
                + ",".join(sorted(excluded))
            )
            return False
        self.service_state = {s: SERVICE_NOT_SEEN for s in self.services}
        if not self.sync_runtime_compose_override():
            return False
        self.last_runtime_diagnostic = ""
        return True

    def update_service_states(self) -> bool:
        ok, services, detail = self.get_compose_ps_entries()
        if not ok:
            self.last_runtime_diagnostic = detail
            return False

        seen = set()
        excluded = set(getattr(self, "exclude_services", []) or [])
        for svc in services:
            try:
                name = svc["Service"]
                if name in excluded:
                    continue
                state = str(svc.get("State", "")).lower()
                health = str(svc.get("Health", "")).lower()
                exit_code = str(svc.get("ExitCode", "")).strip()

                seen.add(name)

                if state == "running":
                    if not health or health == "healthy":
                        self.service_state[name] = SERVICE_HEALTHY
                    elif health == "starting":
                        self.service_state[name] = SERVICE_STARTING
                    else:
                        self.service_state[name] = SERVICE_FAILED
                elif state in {"created", "restarting", "starting"}:
                    self.service_state[name] = SERVICE_STARTING
                elif state in {"exited", "dead"} or (exit_code and exit_code != "0"):
                    self.service_state[name] = SERVICE_FAILED
                else:
                    self.service_state[name] = SERVICE_NOT_SEEN
            except Exception:
                continue

        for s in self.services:
            if s not in seen:
                self.service_state[s] = SERVICE_NOT_SEEN
        self.last_runtime_diagnostic = ""
        return True

    def launch_containers(self) -> Tuple[bool, str, str]:
        if not os.path.exists("Dockerfile") and os.path.exists("dockerfile"):
            try:
                os.rename("dockerfile", "Dockerfile")
            except OSError:
                pass

        self.last_progress_text = ""
        self.last_progress_label = ""
        up_args = ["up", "-d"]
        if self.build_images:
            up_args.append("--build")
        # -u <service> scopes the recreate to that service (Compose recreates it
        # only if its image/config changed and starts its dependencies).
        if isinstance(self.up_service, str):
            up_args.append(self.up_service)
        elif getattr(self, "exclude_services", None):
            if not self.services:
                return False, "", (
                    "No compose services remain after COMPOSER_EXCLUDE_SERVICES; "
                    "refusing to run an unscoped compose up."
                )
            up_args.extend(self.services)
        return self.run_docker_compose_streaming(
            up_args,
            progress_callback=lambda line: self.emit_progress("Compose", line),
        )

    def restart_containers(self) -> Tuple[bool, str, str]:
        self.last_progress_text = ""
        self.last_progress_label = ""
        restart_args = ["restart"]
        if isinstance(self.restart_service, str):
            restart_args.append(self.restart_service)
        return self.run_docker_compose_streaming(
            restart_args,
            progress_callback=lambda line: self.emit_progress("Restart", line),
        )

    def down_containers(self) -> Tuple[bool, str]:
        down_args = ["down"]
        # --purge implies volume removal even when -v is omitted.
        if self.down_volumes or self.purge:
            down_args.append("-v")
        if self.purge:
            # Remove locally built (untagged) images and any orphaned containers
            # for this compose. Networks are already removed by `down` itself.
            down_args.extend(["--rmi", "local", "--remove-orphans"])
        ok, _, err = self.run_docker_compose(down_args)
        return ok, err

    def prune_build_cache(self) -> Tuple[bool, str]:
        # BuildKit cache cannot be scoped to a single compose project, so prune
        # only dangling/unreferenced cache layers (safe for other projects).
        success, _, err = self.run_command(
            ["docker", "builder", "prune", "-f"],
            timeout=120,
        )
        return success, err

    def pull_images(self) -> Tuple[bool, str, str]:
        pull_args = ["pull"]
        if isinstance(self.pull_service, str):
            pull_args.append(self.pull_service)
        elif getattr(self, "exclude_services", None):
            if not self.services and not self.discover_services(silent=True):
                return False, "", self.last_runtime_diagnostic
            if not self.services:
                return False, "", (
                    "No compose services remain after COMPOSER_EXCLUDE_SERVICES; "
                    "refusing to run an unscoped compose pull."
                )
            pull_args.extend(self.services)
        self.last_progress_text = ""
        self.last_progress_label = ""
        return self.run_docker_compose_streaming(
            pull_args,
            progress_callback=lambda line: self.emit_progress("Pull", line),
        )
