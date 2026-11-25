import json
import os
import shlex
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .constants import (
    DEFAULT_RESIDENT_SERVICE,
    ENV_NAME_RE,
    INHERITED_SECRET_KEYS_ENV,
    MIGRATOR_FLAGS_ENV,
    SERVICE_FAILED,
    SERVICE_HEALTHY,
    SERVICE_NOT_SEEN,
    SERVICE_STARTING,
    SERVICE_UPDATING,
    SUPERVISED_ONE_SHOT_PREFIX,
    SUPERVISOR_MODULE,
)
from . import registry_login
from .failure_hints import likely_cause, next_steps
from .output_utils import OutputUtilsMixin
from .service_selection import scoped_service_list
from .subprocess_runner import SubprocessRunnerMixin

# Services that front the stack and serve the maintenance page while `web` is down.
EDGE_PROXY_SERVICES = ("caddy", "nginx", "proxy")


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
        # The generated project runs the migrator as a native Compose `pre_start`
        # step, which is static in the compose file. Compose interpolates this
        # variable into it, so a deploy's -mm / -nm / -a still reach it. Always
        # set, never left to leak in from the ambient environment: an empty value
        # is the correct default and a stale export must not become a flag.
        env[MIGRATOR_FLAGS_ENV] = " ".join(self.migrator_flags())
        env.setdefault("BUILDKIT_PROGRESS", "plain")
        # A private WEB_IMAGE registry: pulls (and an `up` that has to fetch an
        # image) need the login, and the daemon takes it from this client only.
        login = registry_login.docker_config(env)
        if login:
            env["DOCKER_CONFIG"] = login
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
        if manage:
            cmd = self.supervised_prefix(service, cmd) + cmd

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

    def collect_service_diagnostics(
        self, include_logs: bool = True, log_services: Optional[List[str]] = None
    ) -> str:
        """Failing services' state, and the last log lines of those and ``log_services``."""
        ok, services, detail = self.get_compose_ps_entries(include_all=True)
        if not ok:
            return detail

        excluded = set(getattr(self, "exclude_services", []) or [])
        issues: List[str] = []
        failed_services: List[str] = []
        exit_codes: Dict[str, str] = {}

        for service in services:
            name = service.get("Service") or service.get("Name") or "unknown"
            if name in excluded:
                continue
            state = str(service.get("State", "")).lower()
            health = str(service.get("Health", "")).lower()
            exit_code = str(service.get("ExitCode", "")).strip()
            exit_codes[name] = exit_code

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
                failed_services.append(name)

        sections: List[str] = []
        if issues:
            sections.append("Service state:\n" + "\n".join(f"- {issue}" for issue in issues))

        if include_logs:
            wanted = list(log_services or [])
            wanted += [name for name in failed_services if name not in wanted]
            for service_name in wanted[:3]:
                ok, out, err = self.run_docker_compose(
                    ["logs", "--no-color", "--tail", "25", service_name],
                    timeout=15,
                )
                summary = self.summarize_output(out, err, max_lines=12)
                if summary:
                    sections.append(f"{service_name} logs:\n{summary}")
                cause = likely_cause(service_name, f"{out}\n{err}", exit_codes.get(service_name))
                if cause:
                    sections.append(f"{service_name}: {cause}")
            sections.append(next_steps(wanted[:3]))

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
            if getattr(self, "skip_config", False):
                service_env.append('      DLUX_SKIP_CONFIG_IMPORT: "True"')
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

    def supervised_prefix(self, service: str, command: List[str]) -> List[str]:
        """The DjangoLux runtime supervisor prefix for a one-shot command, or [].

        A project ``manage.py`` generated before DjangoLux learned to resolve the
        runtime-active release imports the baked image package, so an exec'd
        ``collectstatic`` or doctor would run a different release than the one
        serving. When the service's own command runs under the supervisor, run
        the one-shot under it too (``--no-watch``, as the pre_start hooks do).
        """
        if any(SUPERVISOR_MODULE in token for token in command):
            return []
        config = self.compose_config_json() or {}
        services = config.get("services") if isinstance(config, dict) else None
        spec = services.get(service) if isinstance(services, dict) else None
        if not isinstance(spec, dict):
            return []
        tokens: List[str] = []
        for key in ("entrypoint", "command"):
            value = spec.get(key)
            if isinstance(value, str):
                try:
                    tokens += shlex.split(value)
                except ValueError:
                    continue
            elif isinstance(value, list):
                tokens += [str(item) for item in value]
        if any(SUPERVISOR_MODULE in token for token in tokens):
            return list(SUPERVISED_ONE_SHOT_PREFIX)
        return []

    def compose_config_json(self) -> Optional[dict]:
        """Resolved compose config, or None if it cannot be read or parsed.

        Interpolated and merged across every active file, so callers see one
        view of a service instead of reconciling overrides themselves.
        """
        ok, out, err = self.run_docker_compose(
            ["config", "--format", "json"], timeout=20
        )
        if not ok:
            self.last_runtime_diagnostic = self.build_failure_detail(out, err)
            return None
        try:
            parsed = json.loads(out)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def discover_services(self, silent: bool = False) -> bool:
        ok, out, err = self.run_docker_compose(["config", "--services"], timeout=10)
        if not ok:
            if not silent:
                self.last_runtime_diagnostic = self.build_failure_detail(out, err)
            return False
        discovered = [s for s in out.splitlines() if s]
        excluded = set(getattr(self, "exclude_services", []) or [])
        monitored = set(getattr(self, "monitored_services", []) or [])
        if monitored:
            discovered = [service for service in discovered if service in monitored]
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

    def mark_services_updating(self, scope=None):
        """Flag the services an operation is about to replace.

        Their last known health was measured on the container being replaced, so
        leaving them green through the pull and recreate reports a state that is
        no longer being observed. Health monitoring resolves each one back.
        """
        targets = scoped_service_list(scope) or self.services
        excluded = set(getattr(self, "exclude_services", []) or [])
        for service in targets:
            if service not in excluded:
                self.service_state[service] = SERVICE_UPDATING

    def update_service_states(self) -> bool:
        ok, services, detail = self.get_compose_ps_entries()
        if not ok:
            self.last_runtime_diagnostic = detail
            return False

        seen = set()
        excluded = set(getattr(self, "exclude_services", []) or [])
        self.service_container_ids = {}
        self.restarting_services = []
        for svc in services:
            try:
                name = svc["Service"]
                if name in excluded:
                    continue
                state = str(svc.get("State", "")).lower()
                health = str(svc.get("Health", "")).lower()
                exit_code = str(svc.get("ExitCode", "")).strip()

                seen.add(name)
                container_id = str(svc.get("ID") or "").strip()
                if container_id:
                    self.service_container_ids.setdefault(name, []).append(container_id)
                if state == "restarting" and name in self.services:
                    self.restarting_services.append(name)

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

    def container_restart_markers(self) -> Dict[str, Dict[str, Tuple[int, str]]]:
        """``{service: {container_id: (RestartCount, StartedAt)}}`` for the last poll.

        Best effort: an inspect that fails yields what it could read, so a
        container removed between the two calls is simply absent.
        """
        ids = {
            container_id: service
            for service, containers in (getattr(self, "service_container_ids", {}) or {}).items()
            if service in self.services
            for container_id in containers
        }
        if not ids:
            return {}
        _, out, _ = self.run_command(
            ["docker", "inspect", "--format", "{{.Id}} {{.RestartCount}} {{.State.StartedAt}}", *ids],
            timeout=10,
        )
        markers: Dict[str, Dict[str, Tuple[int, str]]] = {}
        for line in (out or "").splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            full_id = parts[0]
            service = next(
                (svc for cid, svc in ids.items() if full_id.startswith(cid) or cid.startswith(full_id)),
                None,
            )
            if service is None:
                continue
            try:
                count = int(parts[1])
            except ValueError:
                continue
            started_at = parts[2] if len(parts) > 2 else ""
            markers.setdefault(service, {})[full_id] = (count, started_at)
        return markers

    def edge_proxy_services(self) -> List[str]:
        """The stack's edge proxy services within this run's scope."""
        proxies = [s for s in self.services if s in EDGE_PROXY_SERVICES]
        scoped = scoped_service_list(getattr(self, "up_service", None))
        if scoped:
            proxies = [s for s in proxies if s in scoped]
        return proxies

    def ensure_edge_proxy(self) -> Tuple[List[str], str]:
        """Start the edge proxy on its own when a deploy left it stopped.

        The proxy depends on `web`, so Compose can leave it "Created" when the
        app does not come up, and the site then has no front door at all. Started
        with ``--no-deps`` it serves the maintenance page on 502/503/504 while the
        app is down. Returns ``(started, error)``.
        """
        proxies = self.edge_proxy_services()
        if not proxies:
            return [], ""
        ok, entries, detail = self.get_compose_ps_entries(include_all=True)
        if not ok:
            return [], detail
        running = {
            entry.get("Service")
            for entry in entries
            if str(entry.get("State", "")).lower() == "running"
        }
        stopped = [proxy for proxy in proxies if proxy not in running]
        if not stopped:
            return [], ""
        ok, out, err = self.run_docker_compose(["up", "-d", "--no-deps", *stopped], timeout=120)
        if not ok:
            return [], self.build_failure_detail(out, err)
        return stopped, ""

    def keep_site_reachable(self, detail: str) -> str:
        """``detail`` plus what happened to the edge proxy after a failed deploy."""
        proxies = self.edge_proxy_services()
        if not proxies:
            return detail
        started, error = self.ensure_edge_proxy()
        names = ", ".join(proxies)
        if error:
            note = (
                f"The edge proxy ({names}) is not running and could not be started, so "
                f"the site is unreachable:\n{error}"
            )
        else:
            how = "was started on its own and " if started else ""
            note = (
                f"The edge proxy ({names}) {how}is running: visitors get the maintenance "
                "page whenever web cannot answer."
            )
        return f"{detail}\n\n{note}".strip()

    def launch_containers(self) -> Tuple[bool, str, str]:
        if not os.path.exists("Dockerfile") and os.path.exists("dockerfile"):
            try:
                os.rename("dockerfile", "Dockerfile")
            except OSError:
                pass

        self.last_progress_text = ""
        self.last_progress_label = ""
        # `up` pulls whatever is missing, so it gets the same bar.
        self.pull_progress.reset()
        up_args = ["up", "-d"]
        if self.build_images:
            up_args.append("--build")
        # A named scope limits the recreate to those services (Compose recreates
        # each only if its image/config changed and starts its dependencies).
        scoped = scoped_service_list(self.up_service)
        if scoped:
            up_args.extend(scoped)
        elif getattr(self, "exclude_services", None):
            if not self.services:
                return False, "", (
                    "No compose services remain after COMPOSER_EXCLUDE_SERVICES; "
                    "refusing to run an unscoped compose up."
                )
            up_args.extend(self.services)
        return self.run_docker_compose_streaming(
            up_args,
            progress_callback=lambda line: self.emit_pull_progress("Compose", line),
        )

    def restart_containers(self) -> Tuple[bool, str, str]:
        self.last_progress_text = ""
        self.last_progress_label = ""
        restart_args = ["restart"]
        if isinstance(self.restart_service, str):
            restart_args.append(self.restart_service)
        elif getattr(self, "restart_services", None):
            restart_args.extend(self.restart_services)
        elif getattr(self, "exclude_services", None):
            if not self.services:
                return False, "", (
                    "No compose services remain after COMPOSER_EXCLUDE_SERVICES; "
                    "refusing to run an unscoped compose restart."
                )
            restart_args.extend(self.services)
        return self.run_docker_compose_streaming(
            restart_args,
            progress_callback=lambda line: self.emit_progress("Restart", line),
        )

    def recreate_containers(self, services: List[str]) -> Tuple[bool, str, str]:
        """Recreate the named services, so Compose runs their `pre_start` steps.

        `restart` reuses the existing container and Compose skips its lifecycle
        hooks, which is how a DjangoLux release carrying migrations reached a
        stack where nothing applied them.
        """
        self.last_progress_text = ""
        self.last_progress_label = ""
        if not services:
            return True, "", ""
        # A recreate runs the app image's entrypoint gate like any `up`.
        prepare = getattr(self, "prepare_app_data", None)
        if prepare is not None:
            ready, note = prepare(list(services))
            if not ready:
                return False, "", note
            if note:
                print(f"🔐 {note}")
        return self.run_docker_compose_streaming(
            ["up", "-d", "--no-deps", "--force-recreate", *services],
            progress_callback=lambda line: self.emit_progress("Recreate", line),
        )

    def migration_applier_services(self) -> List[str]:
        """Services whose `pre_start` applies DjangoLux migrations."""
        config = self.compose_config_json()
        if not isinstance(config, dict):
            return []
        appliers = []
        for name, definition in (config.get("services") or {}).items():
            for hook in (definition or {}).get("pre_start") or []:
                command = (hook or {}).get("command")
                if isinstance(command, list):
                    command = " ".join(str(part) for part in command)
                text = str(command or "")
                if "migrator" in text or "migrate" in text:
                    appliers.append(name)
                    break
        return appliers

    def down_containers(self) -> Tuple[bool, str]:
        services = list(getattr(self, "down_services", []) or [])
        if services:
            ok, _, err = self.run_docker_compose(["stop", *services])
            return ok, err
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

    def stream_service_logs(
        self,
        services: List[str],
        tail: str = "50",
        follow: bool = False,
        timestamps: bool = False,
        since: Optional[str] = None,
        until: Optional[str] = None,
        no_color: bool = False,
    ) -> int:
        """Attach `docker compose logs` to the terminal. Returns the exit code."""
        args = ["logs", "--tail", tail]
        if follow:
            args.append("--follow")
        if timestamps:
            args.append("--timestamps")
        if no_color or not sys.stdout.isatty():
            args.append("--no-color")
        if since:
            args.extend(["--since", since])
        if until:
            args.extend(["--until", until])
        args.extend(services)

        argv = self.resolve_compose_cli() + self.build_compose_base_args() + args
        return self.run_command_interactive(argv, env=self.build_compose_env())

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
        scoped = scoped_service_list(self.pull_service)
        if scoped:
            pull_args.extend(scoped)
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
        self.pull_progress.reset()
        return self.run_docker_compose_streaming(
            pull_args,
            progress_callback=lambda line: self.emit_pull_progress("Pull", line),
        )
