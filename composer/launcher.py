import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

from .cli import parse_args, parse_run_args, parse_watch_args
from .config import ConfigMixin
from .constants import ERROR, IDLE, OK, RUNNING
from .docker_compose_manager import DockerComposeMixin
from .health_monitor import HealthMonitorMixin
from .post_start_hooks import PostStartHooksMixin
from .rendering import RenderingMixin
from .secrets_manager import SecretsMixin
from .status_writer import StatusWriterMixin
from .version import read_composer_version
from .version_gate import VersionGateMixin


class DockerComposeLauncher(
    PostStartHooksMixin,
    HealthMonitorMixin,
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
        self.down_mode = False
        self.down_volumes = False
        self.purge = False
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

        self.sections = {
            "secrets": IDLE,
            "pull": IDLE,
            "compose": IDLE,
            "health": IDLE,
            "post_start": IDLE,
        }

        self.services: List[str] = []
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

    def run(self):
        try:
            argv = sys.argv[1:]
            if argv and argv[0] == "run":
                self.handle_run(argv[1:])
                return
            if argv and argv[0] == "watch":
                from .watcher import run_watch

                sys.exit(run_watch(parse_watch_args(argv[1:])))

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
            elif args.update_only:
                # -uo: pull only. A service name scopes the pull; no compose up,
                # health checks, or post-start hooks run after the pull.
                self.update_images = True
                self.pull_only_mode = True
                if isinstance(args.update_only, str):
                    self.pull_service = args.update_only
            if args.restart:
                self.restart_mode = True
                if isinstance(args.restart, str):
                    self.restart_service = args.restart
            self.down_mode = args.down
            self.down_volumes = args.volumes
            self.purge = args.purge

            # Status reporting + version gate config (env, overridable by flags).
            self.status_file = args.status_file or os.environ.get("COMPOSER_STATUS_FILE") or None
            self.log_file = os.environ.get("COMPOSER_LOG_FILE") or None
            self.force = args.force
            self.version_label = os.environ.get("COMPOSER_VERSION_LABEL") or None
            self.active_version_file = os.environ.get("COMPOSER_ACTIVE_VERSION_FILE") or None
            self.active_version_key = os.environ.get("COMPOSER_ACTIVE_VERSION_KEY") or None

            self.extract_config()
            if self.dev_mode:
                # Dev mode always runs with debug on, regardless of the
                # compose's DEBUG/DEBUG_STATUS value or its absence.
                self.debug_mode = True

            self.discover_services(silent=True)

            if self.down_mode:
                print("🛑 Stopping and removing containers...")
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
