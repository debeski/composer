import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

from .cli import (
    parse_agent_check_args,
    parse_agent_args,
    parse_agent_off_args,
    parse_agent_restart_args,
    parse_agent_update_args,
    parse_args,
    parse_check_args,
    parse_enable_agent_args,
    parse_enable_executor_args,
    parse_executor_args,
    parse_log_args,
    parse_pull_args,
    parse_restart_args,
    parse_run_args,
    parse_stop_args,
    parse_update_args,
    parse_update_self_args,
    parse_watch_args,
)
from .checkup import CheckupMixin
from .config import ConfigMixin
from .confirmation import confirm
from .constants import ERROR, EXECUTOR_SERVICE, IDLE, OK, RUNNING
from .docker_compose_manager import DockerComposeMixin
from .health_monitor import HealthMonitorMixin
from .post_start_hooks import PostStartHooksMixin
from .rendering import RenderingMixin
from .secrets_manager import SecretsMixin
from .service_selection import parse_service_list
from .status_writer import StatusWriterMixin
from .version import read_composer_version
from .version_gate import VersionGateMixin


AGENT_SERVICE = "composer-agent"
DEFAULT_SELF_IMAGE = "debeski/composer:latest"


class DockerComposeLauncher(
    PostStartHooksMixin,
    HealthMonitorMixin,
    CheckupMixin,
    ConfigMixin,
    SecretsMixin,
    VersionGateMixin,
    StatusWriterMixin,
    DockerComposeMixin,
    RenderingMixin,
):
    def __init__(self):
        self.app_url = "http://localhost"
        self.composer_version = read_composer_version()
        self.loaded_secrets: List[str] = []
        self.debug_mode = False
        self.no_migrate = False
        self.force_makemigrations = False
        self.secrets_source = None
        self.compose_file = None
        self.active_compose_files: List[str] = []
        self.dev_mode = False
        self.target_app = None
        self.update_images = False
        self.pull_only_mode = False
        self.pull_service = None
        self.up_service = None
        self.restart_mode = False
        self.restart_service = None
        self.restart_services: List[str] = []
        self.down_mode = False
        self.down_volumes = False
        self.down_services: List[str] = []
        self.stop_command = "stop"
        self.purge = False
        self.assume_yes = False
        self.last_progress_text = ""
        self.last_progress_label = ""
        self.last_runtime_diagnostic = ""
        self.last_render_line_count = 0
        self.compose_runtime_override: Optional[Path] = None
        self.build_images = False

        # Status reporting (phase 1) — opt-in via --status-file / COMPOSER_STATUS_FILE.
        self.status_file: Optional[str] = None
        # Console log — opt-in via COMPOSER_LOG_FILE (set by `composer watch`).
        self.log_file: Optional[str] = None
        # Version gate (phase 2) — opt-in via COMPOSER_ACTIVE_VERSION_FILE.
        self.force = False
        self.version_label: Optional[str] = None
        self.active_version_file: Optional[str] = None
        self.active_version_key: Optional[str] = None
        self.gate_images: List[str] = []
        self.gate_target_version: Optional[str] = None
        self.gate_active_version: Optional[str] = None
        self.exclude_services: List[str] = []

        self.sections = {
            "secrets": IDLE,
            "pull": IDLE,
            "compose": IDLE,
            "health": IDLE,
            "post_start": IDLE,
        }

        self.services: List[str] = []
        self.monitored_services: List[str] = []
        self.service_state: Dict[str, str] = {}

    def cleanup(self):
        for k in self.loaded_secrets:
            os.environ.pop(k, None)
        self.remove_runtime_compose_override()

    def handle_interrupt(self):
        if self.last_render_line_count or self.last_progress_text:
            print("\r\033[2K", end="")
        print("\nInterrupted by user. Exiting cleanly.", flush=True)

    def resolve_active_compose_files(self):
        """Populate self.active_compose_files from self.compose_file/self.dev_mode."""
        if self.compose_file:
            self.active_compose_files = [self.compose_file]
            return
        base_file = "compose.yml"
        if not Path(base_file).exists() and Path("docker-compose.yml").exists():
            base_file = "docker-compose.yml"
        self.active_compose_files = [base_file]
        if self.dev_mode:
            self.active_compose_files.append("compose.dev.yml")

    def handle_run(self, argv):
        """`composer run [opts] <service> <command...>` — exec into a service."""
        run_args = parse_run_args(argv)
        if not run_args.command:
            print(
                "✖ run: no command given.\n"
                "  Usage: composer run [-m] [-s] [-F] <service> <command...>",
                file=sys.stderr,
            )
            sys.exit(2)

        self.compose_file = run_args.file
        self.dev_mode = run_args.dev
        self.resolve_active_compose_files()

        code = self.exec_in_service(
            run_args.service,
            run_args.command,
            manage=run_args.manage,
            shell=run_args.shell,
            fresh=run_args.fresh,
        )
        sys.exit(code)

    def handle_log(self, argv):
        """`composer log [opts] [service...]` — read Compose service logs."""
        log_args = parse_log_args(argv)
        tail = str(log_args.tail).strip().lower()
        if tail in {"0", "-1", "all"}:
            tail = "all"
        elif not tail.isdigit():
            print(
                f"✖ log: --tail expects a line count or 'all', got {log_args.tail!r}.",
                file=sys.stderr,
            )
            sys.exit(2)

        self.compose_file = log_args.file
        self.dev_mode = log_args.dev
        self.resolve_active_compose_files()

        code = self.stream_service_logs(
            log_args.service,
            tail=tail,
            follow=log_args.follow,
            timestamps=log_args.timestamps,
            since=log_args.since,
            until=log_args.until,
            no_color=log_args.no_color,
        )
        sys.exit(code)

    def handle_update_self(self, argv):
        parse_update_self_args(argv)
        image = os.environ.get("COMPOSER_SELF_IMAGE") or DEFAULT_SELF_IMAGE
        print(f"Current Composer version: {self.composer_version}")
        print(f"Pulling {image}...")
        code = self.run_command_interactive(["docker", "pull", image])
        if code != 0:
            sys.exit(code)
        ok, out, err = self.run_command(
            ["docker", "run", "--rm", "--entrypoint", "cat", image, "/app/VERSION"],
            timeout=30,
        )
        if not ok:
            print(
                f"✖ Pulled {image}, but could not read its version: {err or out}",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"Installed Composer version: {out.strip()}")

    def configure_update(self, argv):
        """Configure `composer update [opts] [service...]` for the update pipeline."""
        update_args = parse_update_args(argv)
        self.update_images = True
        self.pull_only_mode = False
        if update_args.service:
            self.pull_service = list(update_args.service)
            self.up_service = list(update_args.service)
        self.compose_file = update_args.file
        self.dev_mode = update_args.dev
        self.build_images = update_args.build
        self.force = update_args.force
        self.no_migrate = update_args.no_migrate
        self.force_makemigrations = update_args.make_migrations
        self.target_app = update_args.app
        self.resolve_active_compose_files()
        self.status_file = (
            update_args.status_file or os.environ.get("COMPOSER_STATUS_FILE") or None
        )
        self.log_file = os.environ.get("COMPOSER_LOG_FILE") or None
        self.version_label = os.environ.get("COMPOSER_VERSION_LABEL") or None
        self.active_version_file = os.environ.get("COMPOSER_ACTIVE_VERSION_FILE") or None
        self.active_version_key = os.environ.get("COMPOSER_ACTIVE_VERSION_KEY") or None
        self.exclude_services = parse_service_list(
            os.environ.get("COMPOSER_EXCLUDE_SERVICES")
        )

    def configure_pull(self, argv):
        pull_args = parse_pull_args(argv)
        self.update_images = True
        self.pull_only_mode = True
        if pull_args.service:
            self.pull_service = list(pull_args.service)
        self.compose_file = pull_args.file
        self.dev_mode = pull_args.dev
        self.resolve_active_compose_files()
        self.status_file = (
            pull_args.status_file or os.environ.get("COMPOSER_STATUS_FILE") or None
        )
        self.log_file = os.environ.get("COMPOSER_LOG_FILE") or None
        self.exclude_services = parse_service_list(
            os.environ.get("COMPOSER_EXCLUDE_SERVICES")
        )

    @staticmethod
    def _agent_target_argv(args, *, status_file=False):
        argv = []
        if args.dev:
            argv.append("-d")
        if args.file:
            argv.extend(["-f", args.file])
        if status_file and args.status_file:
            argv.extend(["--status-file", args.status_file])
        argv.append(AGENT_SERVICE)
        return argv

    def _resident_pair_scope(self):
        """Resident services the agent-* commands act on: composer-agent, plus
        composer-executor when the hardened topology defines it.

        agent-update in particular must recreate both from the one shared image
        so they can never drift to different versions (both are debeski/composer).
        Legacy stacks with no executor resolve to just composer-agent.
        """
        pair = [AGENT_SERVICE]
        try:
            if not getattr(self, "active_compose_files", None):
                self.resolve_active_compose_files()
            ok, out, _ = self.run_docker_compose(["config", "--services"], timeout=10)
            if ok:
                defined = {s.strip() for s in out.splitlines() if s.strip()}
                if EXECUTOR_SERVICE in defined:
                    pair.append(EXECUTOR_SERVICE)
        except Exception:
            pass
        return pair

    def configure_agent_update(self, argv):
        args = parse_agent_update_args(argv)
        self.configure_update(self._agent_target_argv(args, status_file=True))
        self.no_migrate = True
        self.active_version_file = None
        self.active_version_key = None
        pair = self._resident_pair_scope()
        self.pull_service = list(pair)
        self.up_service = list(pair)
        self.monitored_services = list(pair)
        self.exclude_services = [
            service for service in self.exclude_services if service not in pair
        ]

    def configure_agent_restart(self, argv):
        args = parse_agent_restart_args(argv)
        self.configure_restart(self._agent_target_argv(args, status_file=True))
        pair = self._resident_pair_scope()
        # Use the multi-service list form (restart_service singular is bypassed).
        self.restart_service = None
        self.restart_services = list(pair)
        self.monitored_services = list(pair)
        self.exclude_services = [
            service for service in self.exclude_services if service not in pair
        ]

    def configure_agent_off(self, argv):
        args = parse_agent_off_args(argv)
        self.configure_stop(
            self._agent_target_argv(args),
            command="agent-off",
        )
        self.down_services = self._resident_pair_scope()

    def configure_stop(self, argv, command="stop"):
        """Configure `composer stop [opts] [service...]` for the down pipeline."""
        stop_args = parse_stop_args(argv, prog=f"composer {command}")
        if stop_args.service and (stop_args.volumes or stop_args.purge):
            print(
                f"✖ {command}: -v/--volumes and -p/--purge act on the whole project "
                "and cannot be scoped to services.\n"
                f"  Drop the service names, or run 'composer {command}' with them and "
                "no destructive flag.",
                file=sys.stderr,
            )
            sys.exit(2)
        self.stop_command = command
        self.down_mode = True
        self.down_volumes = stop_args.volumes
        self.purge = stop_args.purge
        self.down_services = list(stop_args.service)
        self.assume_yes = stop_args.yes
        self.compose_file = stop_args.file
        self.dev_mode = stop_args.dev
        self.resolve_active_compose_files()

    def confirm_stop(self) -> bool:
        """Gate volume/image destruction behind an explicit y/yes."""
        if not (self.down_volumes or self.purge):
            return True
        flag = "--purge" if self.purge else "--volumes"
        action = f"composer {self.stop_command} {flag} permanently destroys project data"
        consequences = ["Named volumes are removed (databases, uploads, caches)."]
        if self.purge:
            consequences.append("Locally built untagged images and orphan containers are removed.")
            consequences.append("Dangling builder cache is pruned (host-wide, unreferenced only).")
        consequences.append(f"Compose files: {', '.join(self.active_compose_files)}")
        return confirm(action, consequences, assume_yes=self.assume_yes)

    def configure_restart(self, argv):
        """Configure `composer restart [opts] [service]` for the restart pipeline."""
        restart_args = parse_restart_args(argv)
        self.restart_mode = True
        self.restart_service = restart_args.service
        self.compose_file = restart_args.file
        self.dev_mode = restart_args.dev
        self.resolve_active_compose_files()
        self.status_file = (
            restart_args.status_file
            or os.environ.get("COMPOSER_STATUS_FILE")
            or None
        )
        self.log_file = os.environ.get("COMPOSER_LOG_FILE") or None
        self.exclude_services = parse_service_list(
            os.environ.get("COMPOSER_EXCLUDE_SERVICES")
        )
        if not self.restart_service:
            self.restart_services = parse_service_list(
                os.environ.get("COMPOSER_RESTART_SERVICES")
            )
            excluded = set(self.exclude_services)
            self.restart_services = [
                service for service in self.restart_services if service not in excluded
            ]

    def run(self):
        try:
            argv = sys.argv[1:]
            if any(
                token == "-uo" or token.startswith("--update-only")
                for token in argv
            ):
                print(
                    "✖ -uo/--update-only has been replaced by 'composer pull'.",
                    file=sys.stderr,
                )
                sys.exit(2)
            if argv == ["--update"] or (argv and argv[0] == "update-self"):
                self.handle_update_self([] if argv == ["--update"] else argv[1:])
                return
            if argv and argv[0] == "run":
                self.handle_run(argv[1:])
                return
            if argv and argv[0] == "watch":
                from .watcher import run_watch

                sys.exit(run_watch(parse_watch_args(argv[1:])))
            if argv and argv[0] == "agent":
                from .agent import run_agent

                sys.exit(run_agent(parse_agent_args(argv[1:])))
            if argv and argv[0] == "executor":
                from .executor import run_executor

                sys.exit(run_executor(parse_executor_args(argv[1:])))
            if argv and argv[0] == "enable-agent":
                from .agent_installer import run_enable_agent

                sys.exit(run_enable_agent(parse_enable_agent_args(argv[1:])))
            if argv and argv[0] == "enable-executor":
                from .agent_installer import run_enable_executor

                sys.exit(run_enable_executor(parse_enable_executor_args(argv[1:])))
            if argv and argv[0] in {"log", "logs"}:
                self.handle_log(argv[1:])
                return
            if argv and argv[0] == "check":
                sys.exit(self.run_checkup(parse_check_args(argv[1:])))
            if argv and argv[0] == "agent-check":
                from .watcher import run_agent_check

                sys.exit(run_agent_check(parse_agent_check_args(argv[1:])))
            if argv and argv[0] == "agent-update":
                self.configure_agent_update(argv[1:])
            elif argv and argv[0] == "agent-restart":
                self.configure_agent_restart(argv[1:])
            elif argv and argv[0] == "agent-off":
                self.configure_agent_off(argv[1:])
            elif argv and argv[0] in {"restart", "-r", "--restart"}:
                self.configure_restart(argv[1:])
            elif argv and argv[0] in {"stop", "down"}:
                self.configure_stop(argv[1:], command=argv[0])
            elif argv and argv[0] == "update":
                self.configure_update(argv[1:])
            elif argv and argv[0] == "pull":
                self.configure_pull(argv[1:])
            else:
                args = parse_args()

                if args.version:
                    print(f"composer {self.composer_version}")
                    return
                self.no_migrate = args.no_migrate
                self.force_makemigrations = args.make_migrations
                self.dev_mode = args.dev
                self.compose_file = args.file
                self.resolve_active_compose_files()

                self.target_app = args.app
                self.build_images = args.build
                if args.update:
                    # -u: pull then recreate. A service name scopes both the pull
                    # and the recreate so only that service is updated and restarted
                    # (Compose still starts its dependencies; dependents are left
                    # untouched unless their own image changed).
                    self.update_images = True
                    if isinstance(args.update, str):
                        self.pull_service = args.update
                        self.up_service = args.update
                self.down_mode = args.down
                self.stop_command = "--down"
                self.down_volumes = args.volumes
                self.purge = args.purge
                self.assume_yes = args.yes

                # Status reporting + version gate config (env, overridable by flags).
                self.status_file = args.status_file or os.environ.get("COMPOSER_STATUS_FILE") or None
                self.log_file = os.environ.get("COMPOSER_LOG_FILE") or None
                self.force = args.force
                self.version_label = os.environ.get("COMPOSER_VERSION_LABEL") or None
                self.active_version_file = os.environ.get("COMPOSER_ACTIVE_VERSION_FILE") or None
                self.active_version_key = os.environ.get("COMPOSER_ACTIVE_VERSION_KEY") or None
                self.exclude_services = parse_service_list(os.environ.get("COMPOSER_EXCLUDE_SERVICES"))

            self.extract_config()
            if self.dev_mode:
                # Dev mode always runs with debug on, regardless of the
                # compose's DEBUG/DEBUG_STATUS value or its absence.
                self.debug_mode = True

            self.discover_services(silent=True)

            if self.down_mode:
                if not self.confirm_stop():
                    sys.exit(1)
                scope = ", ".join(self.down_services) if self.down_services else "all services"
                action = (
                    "Stopping containers"
                    if self.down_services
                    else "Stopping and removing containers"
                )
                print(f"🛑 {action} ({scope})...")
                if self.down_volumes or self.purge:
                    print("   (Volumes will be removed)")
                if self.purge:
                    print("   (Purging built images, networks, orphans, and build cache)")
                ok, err = self.down_containers()
                if not ok:
                    print(f"✖ Failed to stop containers:\n  {err.strip()}")
                    sys.exit(1)
                if self.purge:
                    cache_ok, cache_err = self.prune_build_cache()
                    if not cache_ok:
                        print(f"⚠ Failed to prune build cache:\n  {cache_err.strip()}")
                print("✅ Containers stopped")
                return

            if self.restart_mode:
                self.write_status("restarting")
                if self.services:
                    self.update_service_states()
                self.render()

                self.sections["secrets"] = RUNNING
                self.render()
                ok, err = self.resolve_secrets()
                if not ok:
                    self.sections["secrets"] = ERROR
                    self.write_status("failed", error=err)
                    self.render(err)
                    sys.exit(1)
                self.sections["secrets"] = OK

                self.sections["compose"] = RUNNING
                self.render()
                ok, out, err = self.restart_containers()
                if not ok:
                    self.sections["compose"] = ERROR
                    diagnostics = self.collect_service_diagnostics()
                    detail = self.build_failure_detail(out, err, diagnostics)
                    self.write_status("failed", error=detail)
                    self.render(f"Failed to restart containers\n\n{detail}")
                    sys.exit(1)
                self.sections["compose"] = OK

                self.sections["health"] = RUNNING
                self.render()
                health_ok, health_detail = self.monitor_health()
                if not health_ok:
                    self.sections["health"] = ERROR
                    self.write_status("failed", error=health_detail)
                    self.render(health_detail)
                    sys.exit(1)
                self.sections["health"] = OK
                self.render()

                self.write_status("ready")
                print("\n🎉 Services restarted")
                return

            self.write_status("starting")
            if self.services:
                self.update_service_states()
            self.render()

            self.sections["secrets"] = RUNNING
            self.render()

            ok, err = self.resolve_secrets()
            if not ok:
                self.sections["secrets"] = ERROR
                self.write_status("failed", error=err)
                self.render(err)
                sys.exit(1)
            self.sections["secrets"] = OK

            if self.update_images:
                self.sections["pull"] = RUNNING
                self.write_status("pulling")
                self.render()
                ok, out, err = self.pull_images()
                if not ok:
                    self.sections["pull"] = ERROR
                    detail = self.build_failure_detail(out, err)
                    self.write_status("failed", error=detail)
                    self.render(f"Failed to pull images\n\n{detail}")
                    sys.exit(1)
                self.sections["pull"] = OK

                if self.pull_only_mode:
                    self.write_status("pulled")
                    self.render()
                    print("\n✅ Images pulled")
                    return

                # Preflight version gate: refuse to recreate onto an older image
                # version than the deployment's active one (opt-in; see
                # VersionGateMixin). Runs after pull so the target label is local.
                gate_ok, gate_msg = self.preflight_version_gate()
                if not gate_ok:
                    self.sections["compose"] = ERROR
                    self.write_status("failed", error=gate_msg)
                    self.render(gate_msg)
                    sys.exit(1)

            self.sections["compose"] = RUNNING
            self.write_status("recreating")
            self.render()
            if not self.discover_services():
                self.sections["compose"] = ERROR
                detail = self.last_runtime_diagnostic or "Check the compose file and environment values."
                self.write_status("failed", error=detail)
                self.render("Failed to read compose services\n\n" + detail)
                sys.exit(1)

            ok, out, err = self.launch_containers()
            if not ok:
                self.sections["compose"] = ERROR
                diagnostics = self.collect_service_diagnostics()
                detail = self.build_failure_detail(out, err, diagnostics)
                self.write_status("failed", error=detail)
                self.render(f"Failed to start containers\n\n{detail}")
                sys.exit(1)
            self.sections["compose"] = OK

            self.sections["health"] = RUNNING
            self.render()

            health_ok, health_detail = self.monitor_health()
            if not health_ok:
                self.sections["health"] = ERROR
                self.write_status("failed", error=health_detail)
                self.render(health_detail)
                sys.exit(1)
            self.sections["health"] = OK

            self.sections["post_start"] = RUNNING
            self.write_status("migrating")
            self.render()
            hooks_ok, hooks_detail = self.run_post_start_hooks()
            if not hooks_ok:
                self.sections["post_start"] = ERROR
                self.write_status("failed", error=hooks_detail)
                self.render(f"Failed to execute post_start commands\n\n{hooks_detail}")
            else:
                self.sections["post_start"] = OK
                self.write_status("ready")
                self.render()

            print("\n🎉 Environment ready")

        except KeyboardInterrupt:
            self.handle_interrupt()
            raise SystemExit(130)
        finally:
            self.cleanup()
