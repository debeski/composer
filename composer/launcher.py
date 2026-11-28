import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
    parse_migrate_args,
    parse_pull_args,
    parse_restart_args,
    parse_run_args,
    parse_stop_args,
    parse_update_args,
    parse_update_self_args,
    parse_watch_args,
    update_self_options,
)
from .checkup import CheckupMixin
from .config import ConfigMixin
from .confirmation import confirm
from .constants import (
    ERROR,
    EXECUTOR_SERVICE,
    IDLE,
    OK,
    RUNNING,
)
from .docker_compose_manager import DockerComposeMixin
from .failure_hints import clarify_hook_lines, hook_failures
from .downgrade_guard import DowngradeGuardMixin
from .health_monitor import HealthMonitorMixin
from .post_start_hooks import PostStartHooksMixin
from .progress import PullProgress
from .rendering import RenderingMixin
from .secrets_manager import SecretsMixin
from .service_selection import parse_service_list
from .session import install_hangup_guard
from .status_writer import StatusWriterMixin
from .version import read_composer_version
from .version_gate import VersionGateMixin


AGENT_SERVICE = "composer-agent"
DEFAULT_SELF_IMAGE = "debeski/composer:latest"

# Commands whose whole point is the terminal they were typed in: they end with
# it. Everything else (deploys, updates, restarts, the resident roles) keeps
# running when the terminal closes and only stops on Ctrl+C.
TERMINAL_BOUND_COMMANDS = {"run", "migrate", "log", "logs"}
REMOVED_COMMANDS = {
    "--update": "update self",
    "update-self": "update self",
    "agent-check": "agent check",
    "agent-update": "agent update",
    "agent-restart": "agent restart",
    "agent-off": "agent off",
    "watch": "agent watch",
    "dlux-update": "dlux update",
    "enable-agent": "agent enable",
    "enable-executor": "executor enable",
}


def declared_project_name(path) -> str:
    """The top-level ``name:`` a Compose file declares, or "" (read without a YAML parser)."""
    import re

    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = re.search(r"^name:[ \t]*([^#\r\n]*)", text, re.MULTILINE)
    return match.group(1).strip().strip("'\"") if match else ""


def default_project_name(directory) -> str:
    """Compose's project name for a directory with no ``name:``: lower case, ``[a-z0-9_-]`` only."""
    import re

    return re.sub(r"[^a-z0-9_-]", "", Path(directory).resolve().name.lower())


def project_name_conflict(files: List[str], directory=".") -> str:
    """Why the active Compose files make another project than the first file alone, or "".

    The last ``name:`` wins in Compose, so an overlay naming another project
    starts a second stack beside the real one (fresh volumes, a second
    Docker-authority pair: R2-083).
    """
    if len(files) < 2 or os.environ.get("COMPOSE_PROJECT_NAME", "").strip():
        return ""
    base = declared_project_name(files[0]) or default_project_name(directory)
    for other in files[1:]:
        named = declared_project_name(other)
        if named and named != base:
            return (
                f"{other} names the Compose project '{named}', and {files[0]} makes it '{base}': Compose would "
                f"start a second stack, with its own volumes, beside the real one. Give {other} the same "
                f"`name: {base}`, or remove its `name:` line."
            )
    return ""


#: Start of a Compose temporary name: ``<12 hex>_<project>-<service>-<n>`` (a recreate in progress).
INTERRUPTED_NAME = __import__("re").compile(r"^/?([0-9a-f]{12})_(.+)$")
#: What `python -m dlux.ocr.serve` needs: the django-lux[ocr] extra.
OCR_COMMAND = "dlux.ocr.serve"
OCR_MODULES = ("indra_ocr", "onnxruntime")
OCR_PROBE = (
    "import importlib.util, sys; "
    "missing = [m for m in sys.argv[1:] if importlib.util.find_spec(m) is None]; "
    "print('ocr-engine: ' + ('missing ' + ','.join(missing) if missing else 'present'))"
)


def stop_on_sigterm() -> Dict[str, bool]:
    """Let `docker stop` end a resident role at once instead of killing it at the timeout (R2-073).

    PID 1 in a container ignores SIGTERM unless it handles it, so the pair sat
    out the whole stop timeout and was SIGKILLed, possibly mid-write. SIGTERM now
    stops the loop the way Ctrl+C does; the returned flag says it came.
    """
    import signal

    state = {"signalled": False}

    def _stop(signum, frame):
        state["signalled"] = True
        raise KeyboardInterrupt()

    try:
        signal.signal(signal.SIGTERM, _stop)
    except (OSError, ValueError, AttributeError):
        pass
    return state


def registry_hint(output: str) -> str:
    """A pointer to the registry login when a pull was refused for want of one (R2-086)."""
    lowered = (output or "").lower()
    markers = ("unauthorized", "denied", "authentication required", "no basic auth credentials")
    if not any(marker in lowered for marker in markers):
        return ""
    return (
        "\n\nThe registry refused the pull. For a private image, set COMPOSER_REGISTRY_HOST, "
        "COMPOSER_REGISTRY_USERNAME and COMPOSER_REGISTRY_PASSWORD in the secrets file "
        "(docs/private-registry.md)."
    )


def _group_usage(group: str, commands: List[str]) -> str:
    return f"Usage: composer {group} {{{'|'.join(commands)}}} ..."


def _normalize_leading_global_options(argv: List[str]) -> List[str]:
    """Move leading global -f/-d options behind the command they scope."""
    leading: List[str] = []
    rest = list(argv)
    while rest:
        token = rest[0]
        if token in {"-d", "--dev"}:
            leading.append(rest.pop(0))
            continue
        if token == "-f":
            if len(rest) < 2:
                return argv
            leading.extend(rest[:2])
            rest = rest[2:]
            continue
        if token.startswith("--file="):
            leading.append(rest.pop(0))
            continue
        if token == "--file":
            if len(rest) < 2:
                return argv
            leading.extend(rest[:2])
            rest = rest[2:]
            continue
        break
    if not leading or not rest:
        return argv
    if rest[0] in {"self", "agent", "executor", "dlux", "relay", "maintenance"}:
        if len(rest) < 2:
            return argv
        return rest[:2] + leading + rest[2:]
    return rest[:1] + leading + rest[1:]


def _update_self_args(argv: List[str]) -> Optional[List[str]]:
    """The arguments `update self` takes among `update ... self`, or None if they name more.

    Its own options (``--pin``, which wrapper 7 passes, ``--allow-downgrade``,
    ``--allow-unverified-composer``, ``-h``) pass through. Global -f/-d options
    may sit anywhere, since leading ones are moved behind the command. A deployer
    pull has no Compose file to scope, so they drop. A service name or an
    update option means more than the deployer, so it is refused.
    """
    accepted = update_self_options()
    kept: List[str] = []
    selves = 0
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in {"-f", "--file"}:
            index += 2
            continue
        if token == "self":
            selves += 1
        elif token in accepted:
            kept.append(token)
        elif token not in {"-d", "--dev"} and not token.startswith("--file="):
            return None
        index += 1
    return kept if selves == 1 else None


class DockerComposeLauncher(
    PostStartHooksMixin,
    HealthMonitorMixin,
    CheckupMixin,
    ConfigMixin,
    SecretsMixin,
    VersionGateMixin,
    DowngradeGuardMixin,
    StatusWriterMixin,
    DockerComposeMixin,
    RenderingMixin,
):
    def __init__(self):
        self.app_url = "http://localhost"
        self.composer_version = read_composer_version()
        self.loaded_secrets: List[str] = []
        self.debug_mode = False
        # `-nm`: the hook still runs, the migrator just skips schema work and
        # still collects static. Distinct from skip_post_start, which suppresses
        # the hook entirely for runs that do not target the application service.
        self.no_migrate = False
        self.skip_config = False
        self.skip_post_start = False
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
        # `agent update` pins the channel's signed release before it pulls.
        self.pin_resident: Optional[Dict[str, bool]] = None
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
        self.pull_progress = PullProgress()
        self.last_runtime_diagnostic = ""
        self.last_render_line_count = 0
        self.compose_runtime_override: Optional[Path] = None
        self.build_images = False

        # Status reporting (phase 1) — opt-in via --status-file / COMPOSER_STATUS_FILE.
        self.status_file: Optional[str] = None
        # Console log — opt-in via COMPOSER_LOG_FILE (set by `composer agent watch`).
        self.log_file: Optional[str] = None
        # Version gate (phase 2) — opt-in via COMPOSER_ACTIVE_VERSION_FILE.
        self.force = False
        self.version_label: Optional[str] = None
        self.active_version_file: Optional[str] = None
        self.active_version_key: Optional[str] = None
        self.gate_images: List[str] = []
        self.gate_target_version: Optional[str] = None
        self.gate_active_version: Optional[str] = None
        # Service the runtime-release verdict is asked in, and what it answered.
        self.runtime_gate_service: Optional[str] = None
        self.gate_runtime_verdict: Optional[str] = None
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

    def release_project_lock(self) -> None:
        stack = getattr(self, "_lock_stack", None)
        if stack is not None:
            self._lock_stack = None
            stack.close()

    def lock_command(self) -> str:
        """What this run is, as another run's lock refusal names it."""
        if self.pin_resident is not None:
            return "composer agent update"
        if self.down_mode:
            return f"composer {self.stop_command.lstrip('-')}"
        if self.restart_mode:
            return "composer restart"
        if self.update_images:
            return "composer update"
        return "composer up (./start.sh)"

    def take_project_lock(self) -> None:
        """Hold the project's operation lock for the rest of this run, or exit naming its holder.

        Deploys, updates, restarts, stops, `check --fix` and the Operations
        applies share it (``check_lock``): two of them at once recreated the same
        containers under each other (R2-016, R2-079). A pull changes no
        container and does not take it.
        """
        from contextlib import ExitStack

        from . import check_lock

        if self.pull_only_mode:
            return
        stack = ExitStack()
        try:
            previous = stack.enter_context(check_lock.hold(".", command=self.lock_command()))
        except check_lock.CheckFixLockError as exc:
            stack.close()
            self.write_status("failed", error=str(exc))
            print(f"\n✖ {exc}", file=sys.stderr, flush=True)
            sys.exit(1)
        self._lock_stack = stack
        if previous:
            warnings = getattr(self, "deploy_warnings", None)
            if warnings is None:
                warnings = self.deploy_warnings = []
            warnings.append(
                f"The previous {check_lock.describe(previous)} was stopped before it finished, so it may have "
                "left a service half-recreated. This run deploys every service again."
            )

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

    def handle_migrate(self, argv):
        """`composer migrate`: run DLUX's migrator with visible, attached output."""
        args = parse_migrate_args(argv)
        self.compose_file = args.file
        self.dev_mode = args.dev
        self.resolve_active_compose_files()

        command = self.migrator_command_for_service(args.service)
        code = self.exec_in_service(args.service, command + args.migrator_args)
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
        from . import image_pin
        from .agent_installer import AgentInstallError
        from .channel_config import resolve_self_image

        args = parse_update_self_args(argv)
        print(f"Current Composer version: {self.composer_version}")
        if not os.environ.get("COMPOSER_SELF_IMAGE", "").strip():
            try:
                resolution, outcome = image_pin.update_pin(
                    ".",
                    allow_downgrade=args.allow_downgrade,
                    allow_unverified=args.allow_unverified_composer,
                )
            except (image_pin.PinError, AgentInstallError) as exc:
                print(f"✖ Composer was not updated: {exc}", file=sys.stderr)
                sys.exit(1)
            self.report_pin(resolution, outcome)
            if not self.pin_moved(resolution, outcome):
                # Before the first pin the deployer image is the channel tag, and
                # pulling it could move the deployer backwards.
                if not args.pin:
                    print("Nothing to update: the deployer keeps the image it has.")
                return
        if args.pin:
            return
        image = resolve_self_image()
        print(f"Pulling {image}...")
        self.pull_progress.reset()
        self.last_progress_text = ""
        ok, out, err = self.run_command_streaming(
            ["docker", "pull", image],
            progress_callback=lambda line: self.emit_pull_progress("Pull", line),
        )
        self.finish_progress_line()
        if not ok:
            print(f"✖ Failed to pull {image}\n{self.summarize_output(out)}", file=sys.stderr)
            sys.exit(1)
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
        print("The updated deployer runs from the next command.")

    @staticmethod
    def report_pin(resolution, outcome) -> None:
        pin = resolution.pin
        if resolution.note:
            print(("⚠ " if pin is not None and not pin.verified else "") + resolution.note)
        if resolution.changed:
            source = "signed release" if pin.verified else "UNVERIFIED registry digest"
            print(f"🔒 Composer pinned to {pin.ref} ({source}).")
        if outcome.get("files"):
            print(f"   The resident pair now runs it from its next recreate ({', '.join(outcome['files'])}).")

    @staticmethod
    def pin_moved(resolution, outcome) -> bool:
        """Did resolving the pin change what the deployment runs (the lock or the block)?"""
        return bool(resolution.changed or outcome.get("files") or outcome.get("lock_changed"))

    def pin_resident_pair(self) -> Tuple[bool, bool, str]:
        """`agent update`: ``(ok, moved, detail)``. Moves the lock and the block to the channel's signed release.

        The pair is pulled and recreated only when ``moved``. When the resolution
        keeps (nothing newer, an unreadable channel, a release not verifiable
        yet), the block may still name the moving channel tag, and following it
        could move the pair backwards, so nothing is pulled or recreated.
        """
        from . import image_pin
        from .agent_installer import AgentInstallError

        options = self.pin_resident or {}
        if os.environ.get("COMPOSER_SELF_IMAGE", "").strip():
            return True, True, ""
        try:
            resolution, outcome = image_pin.update_pin(
                ".",
                compose_file=self.compose_file or "",
                allow_downgrade=options.get("allow_downgrade", False),
                allow_unverified=options.get("allow_unverified", False),
            )
        except (image_pin.PinError, AgentInstallError) as exc:
            return False, False, f"The resident Composer pair was not updated: {exc}"
        self.report_pin(resolution, outcome)
        if self.pin_moved(resolution, outcome):
            return True, True, ""
        return True, False, "Nothing to update: the resident pair was not pulled or recreated."

    def _resident_on_moving_tag(self, named=None) -> Tuple[List[str], Dict[str, str]]:
        """``(services, images)``: the resident services in scope whose block names no digest."""
        from .stack_lifecycle import RESIDENT_SERVICES, images

        excluded = set(self.exclude_services or [])
        candidates = [
            name for name in RESIDENT_SERVICES
            if name not in excluded and (not named or name in named)
        ]
        if not candidates:
            return [], {}
        defined = images(self.compose_config_json())
        return [name for name in candidates if name in defined and "@sha256:" not in defined[name]], defined

    def _leave_out(self, held: List[str], warning: str) -> None:
        """Drop ``held`` from this run's services and say why in the deploy warnings."""
        self.exclude_services = list(self.exclude_services or []) + held
        self.services = [name for name in (self.services or []) if name not in held]
        for name in held:
            self.service_state.pop(name, None)
        warnings = getattr(self, "deploy_warnings", None)
        if warnings is None:
            warnings = self.deploy_warnings = []
        warnings.append(warning)

    def hold_unpinned_resident_pair(self) -> Optional[List[str]]:
        """Leave a resident pair on a moving tag out of a plain `update` or `pull`.

        The pair moves only through its pin (`agent update`, `update self`,
        `check --beta|--stable`). Pulling the channel tag its block names can move
        it backwards, so while the block names no digest the pair joins
        ``exclude_services`` and leaves a named scope. Returns the services held,
        or None when they were all the services named.
        """
        from .service_selection import scoped_service_list
        from .stack_lifecycle import join

        named = scoped_service_list(self.pull_service) or scoped_service_list(self.up_service)
        held, defined = self._resident_on_moving_tag(named)
        if not held:
            return []
        if named:
            rest = [name for name in named if name not in held]
            if not rest:
                self.exclude_services = list(self.exclude_services or []) + held
                return None
            if scoped_service_list(self.pull_service):
                self.pull_service = rest
            if scoped_service_list(self.up_service):
                self.up_service = rest
        one = len(held) == 1
        self._leave_out(held, (
            f"{join(held)} run{'s' if one else ''} {defined[held[0]]}, a moving tag, so this update left "
            f"{'it' if one else 'them'} as {'it was' if one else 'they were'}: the resident pair moves only "
            "through './start.sh agent update', which pins the channel's signed release."
        ))
        return held

    def _local_image_id(self, image: str) -> str:
        ok, out, _ = self.run_command(["docker", "image", "inspect", "--format", "{{.Id}}", image], timeout=15)
        return out.strip() if ok else ""

    def _composer_image_version(self, image: str) -> str:
        """The Composer release a local image carries: its manifest label, else its /app/VERSION."""
        import json

        from .version import RELEASE_MANIFEST_LABEL

        try:
            manifest = json.loads((self.image_labels(image) or {}).get(RELEASE_MANIFEST_LABEL) or "{}")
        except ValueError:
            manifest = {}
        version = str(manifest.get("version") or "").strip() if isinstance(manifest, dict) else ""
        if version:
            return version
        ok, out, _ = self.run_command(
            ["docker", "run", "--rm", "--network", "none", "--entrypoint", "cat", image, "/app/VERSION"],
            timeout=30,
        )
        return out.strip() if ok else ""

    def hold_older_resident_tag(self) -> List[str]:
        """Keep an unscoped `up` from recreating the pair on an older Composer.

        The block can come to name an older Composer than the running pair in two
        ways: the wrapper pulled the channel tag before the first pin (a new image
        ID behind the same reference), or the pinned block itself names an older
        release (a ``compose.yml`` restored from git or a backup, an edited pin
        line: R2-023). Either way `up` would recreate the pair backwards. A
        running resident whose block names an older release, an image whose
        release cannot be read, or a pinned image whose version is not the one
        its tag names (R2-025) is left out of `up`. A newer or equal release, a
        stopped pair and an explicit ``COMPOSER_SELF_IMAGE`` go ahead as before.
        Returns the services held.
        """
        from . import image_pin
        from .service_selection import scoped_service_list
        from .stack_lifecycle import RESIDENT_SERVICES, images, join
        from .versions import try_parse

        if os.environ.get("COMPOSER_SELF_IMAGE", "").strip() or scoped_service_list(self.up_service):
            return []
        excluded = set(self.exclude_services or [])
        defined = images(self.compose_config_json())
        candidates = [name for name in RESIDENT_SERVICES if name not in excluded and name in defined]
        if not candidates:
            return []
        ok, rows, _ = self.get_compose_ps_entries()
        containers = {
            str(row.get("Service") or ""): str(row.get("ID") or "")
            for row in (rows if ok else [])
            if str(row.get("State") or "").lower() == "running" and row.get("ID")
        }
        held, seen = [], {}
        detail = ""
        running_versions = []
        for name in candidates:
            if not containers.get(name):
                continue
            found, out, _ = self.run_command(
                ["docker", "inspect", "--format", "{{.Image}}", containers[name]], timeout=15)
            running = out.strip() if found else ""
            reference = defined[name]
            tagged = self._local_image_id(reference)
            pin = image_pin.parse_ref(reference)
            if running and running == tagged and pin is not None:
                seen.setdefault(running, self._composer_image_version(running))
                if try_parse(seen[running]) not in (None, try_parse(pin.version)):
                    self.warn_operator(
                        f"{name} runs {reference}, whose tag names Composer {pin.version}, but that image is "
                        f"Composer {seen[running]}: the lock or the block pins a digest of another release. Run "
                        "'./start.sh agent update' to pin the channel's verified release."
                    )
            if not running or running == tagged:
                continue
            for image in (running, tagged):
                if image and image not in seen:
                    seen[image] = self._composer_image_version(image)
            actual = seen.get(tagged, "") if tagged else ""
            mine = try_parse(seen.get(running))
            running_versions.append(seen.get(running) or "")
            if pin is not None and actual and try_parse(actual) is not None and \
                    try_parse(actual) != try_parse(pin.version):
                held.append(name)
                detail = (f"names Composer {pin.version} in its tag, but that image is Composer {actual}: "
                          "the tag and the digest disagree")
                continue
            theirs = try_parse(actual) if actual else (try_parse(pin.version) if pin is not None else None)
            if mine is None or (theirs is not None and theirs >= mine):
                continue
            held.append(name)
            named = actual or (pin.version if pin is not None else "")
            detail = (
                f"now names Composer {named}, older than the running {seen[running]}"
                if theirs is not None else
                f"now names an image whose Composer release cannot be read (running: {seen[running]})"
            )
        if held:
            one = len(held) == 1
            self._leave_out(held, (
                f"{join(held)} {'was' if one else 'were'} left as {'it is' if one else 'they are'}: "
                f"{defined[held[0]]} {detail}. Run './start.sh agent update', which verifies the channel's "
                "release, or 'check --beta' / 'check --stable' to switch on purpose. To run that image on "
                "purpose, set COMPOSER_SELF_IMAGE to it."
            ))
        self.warn_pair_behind_deployer([name for name in candidates if name not in held], seen, containers)
        return held

    def warn_pair_behind_deployer(self, services: List[str], seen: Dict[str, str], containers: Dict[str, str]) -> None:
        """Say so when the resident pair stays on an older Composer than this deployer (C1-11).

        A plain `./start.sh` never moves the pair forward on its own (only its pin
        does), so a pair left on an old moving tag used to stay there silently.
        """
        from .stack_lifecycle import join
        from .versions import try_parse

        deployer = try_parse(self.composer_version)
        if deployer is None:
            return
        behind, version = [], ""
        for name in services:
            container = containers.get(name)
            if not container:
                continue
            found, out, _ = self.run_command(["docker", "inspect", "--format", "{{.Image}}", container], timeout=15)
            image = out.strip() if found else ""
            if not image:
                continue
            if image not in seen:
                seen[image] = self._composer_image_version(image)
            parsed = try_parse(seen[image])
            if parsed is not None and parsed < deployer:
                behind.append(name)
                version = seen[image]
        if not behind:
            return
        one = len(behind) == 1
        warnings = getattr(self, "deploy_warnings", None)
        if warnings is None:
            warnings = self.deploy_warnings = []
        warnings.append(
            f"{join(behind)} run{'s' if one else ''} Composer {version}, older than this deployer "
            f"({self.composer_version}). Run './start.sh agent update' to move "
            f"{'it' if one else 'them'} to the channel's signed release."
        )

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
        self.skip_config = update_args.skip_config
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
        self.runtime_gate_service = os.environ.get("COMPOSER_RUNTIME_GATE_SERVICE") or None
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

        `agent update` in particular must recreate both from the one shared image
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
        self.pin_resident = {
            "allow_downgrade": args.allow_downgrade,
            "allow_unverified": args.allow_unverified_composer,
        }
        # This run replaces the resident pair, never the application service, so
        # there is nothing to migrate or collect — suppress the hook itself
        # rather than asking the migrator to skip part of its work.
        self.skip_post_start = True
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
            command="agent off",
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
        """Configure `composer restart [opts] [service...]` for the restart pipeline.

        One service keeps the single-service form; several go to Compose in one
        `restart`, in the order given, like `docker compose restart web worker`.
        """
        restart_args = parse_restart_args(argv)
        self.restart_mode = True
        named = list(dict.fromkeys(restart_args.service or []))
        self.restart_service = named[0] if len(named) == 1 else None
        self.restart_services = named if len(named) > 1 else []
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
        if not self.restart_service and not self.restart_services:
            self.restart_services = parse_service_list(
                os.environ.get("COMPOSER_RESTART_SERVICES")
            )
            excluded = set(self.exclude_services)
            self.restart_services = [
                service for service in self.restart_services if service not in excluded
            ]

    def clear_interrupted_recreates(self) -> List[str]:
        """Tidy the containers a killed recreate left behind, before `up` (R2-028).

        Compose recreates a service by renaming the old container to
        ``<12 hex>_<name>``, creating the new one and removing the old. A deploy
        killed in between leaves the renamed one, and the next `up` failed on it
        ("container 2fff…_…-smtp-relay-1 has no healthcheck configured") and took
        the site down. A stopped leftover is removed (its volumes stay); a running
        one gets its own name back when that name is free. Returns what was done.
        """
        project = str((self.compose_config_json() or {}).get("name") or "").strip()
        if not project:
            return []
        ok, out, _ = self.run_command(
            ["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project}",
             "--format", "{{.ID}} {{.Names}} {{.State}}"],
            timeout=20,
        )
        if not ok:
            return []
        rows = [line.split() for line in (out or "").splitlines() if len(line.split()) >= 3]
        names = {row[1].lstrip("/") for row in rows}
        done = []
        for container, name, state in (row[:3] for row in rows):
            match = INTERRUPTED_NAME.match(name)
            if not match:
                continue
            original = match.group(2)
            if state.lower() in {"created", "exited", "dead"}:
                removed, _, _ = self.run_command(["docker", "rm", container], timeout=30)
                if removed:
                    done.append(f"removed {name}, left {state} by an interrupted recreate")
            elif original not in names:
                renamed, _, _ = self.run_command(["docker", "rename", container, original], timeout=30)
                if renamed:
                    names.add(original)
                    done.append(f"renamed {name} back to {original}")
        if done:
            print(f"ℹ A previous deploy stopped halfway: {'; '.join(done)}.", flush=True)
            self.append_console("Interrupted recreate tidied: " + "; ".join(done))
        return done

    def leave_out_ocr_without_engine(self) -> List[str]:
        """Leave out an OCR service whose image has no OCR engine, and stop it (R2-057).

        `docs/ocr.md` allows a project without the django-lux[ocr] extra, but the
        scaffold's `ocr` service still runs ``python -m dlux.ocr.serve`` on the app
        image, which then crash-loops and failed every `./start.sh`. The image is
        asked (``--network none``, ``--pull never``) whether it ships the engine;
        an image that cannot be asked is left to `up` as before.
        """
        from .service_selection import scoped_service_list

        config = self.compose_config_json() or {}
        scoped = scoped_service_list(self.up_service)
        held = []
        for name, spec in (config.get("services") or {}).items():
            if not isinstance(spec, dict) or name in (self.exclude_services or []):
                continue
            if scoped and name not in scoped:
                continue
            command = spec.get("command")
            text = " ".join(map(str, command)) if isinstance(command, list) else str(command or "")
            image = str(spec.get("image") or "")
            if OCR_COMMAND not in text or not image:
                continue
            ok, out, _ = self.run_command(
                ["docker", "run", "--rm", "--pull", "never", "--network", "none", "--entrypoint", "python",
                 image, "-I", "-c", OCR_PROBE, *OCR_MODULES],
                timeout=60,
            )
            if ok and "ocr-engine: missing" in (out or ""):
                held.append(str(name))
        for name in held:
            self.run_docker_compose(["stop", name], timeout=120)
            self._leave_out([name], (
                f"{name} was left out and stopped: its image has no OCR engine (the django-lux[ocr] extra), so "
                f"'python -m {OCR_COMMAND}' cannot start. Build the image with the extra, or add "
                f"COMPOSER_EXCLUDE_SERVICES={name} to the secrets file to say so."
            ))
        return held

    def prepare_app_data(self, services: Optional[List[str]] = None) -> Tuple[bool, str]:
        """``(ready, note)``: give a non-root app image access to its data before ``up``.

        ``services`` scopes it to a recreate of those services: nothing to do
        when none of them runs the app image.
        """
        from . import app_permissions

        config = self.compose_config_json() or {}
        image = app_permissions.app_image(config)
        if not image:
            return True, ""
        if services is not None and not set(services) & set(app_permissions.app_services(config)):
            return True, ""
        return app_permissions.ensure_for_image(self, image, config)

    def run(self):
        try:
            argv = _normalize_leading_global_options(sys.argv[1:])
            pipeline_configured = False
            if not argv or argv[0] not in TERMINAL_BOUND_COMMANDS:
                install_hangup_guard()
            if any(
                token == "-uo" or token.startswith("--update-only")
                for token in argv
            ):
                print(
                    "✖ -uo/--update-only has been replaced by 'composer pull'.",
                    file=sys.stderr,
                )
                sys.exit(2)
            if argv and argv[0] in REMOVED_COMMANDS:
                print(
                    f"✖ '{argv[0]}' was removed. Use 'composer {REMOVED_COMMANDS[argv[0]]}'.",
                    file=sys.stderr,
                )
                sys.exit(2)
            if argv and argv[0] == "self":
                if len(argv) == 1 or argv[1] in {"-h", "--help"}:
                    print(_group_usage("self", ["update"]))
                    return
                if argv[1] != "update":
                    print(_group_usage("self", ["update"]), file=sys.stderr)
                    sys.exit(2)
                print("Note: 'self update' is deprecated; use 'composer update self'.", file=sys.stderr)
                self.handle_update_self(argv[2:])
                return
            # Wrapper 7 resolves the pin with `update self --pin` from the
            # deployer it runs; an older wrapper passes a bare `update self`
            # and still gets the pull for its next run.
            if argv and argv[0] == "update" and "self" in argv[1:]:
                self_args = _update_self_args(argv[1:])
                if self_args is None:
                    print(
                        "Usage: composer update self [--pin] [--allow-downgrade] "
                        "[--allow-unverified-composer]  (updates only the deployer; "
                        "takes no services or update options)",
                        file=sys.stderr,
                    )
                    sys.exit(2)
                self.handle_update_self(self_args)
                return
            if argv and argv[0] == "run":
                self.handle_run(argv[1:])
                return
            if argv and argv[0] == "migrate":
                self.handle_migrate(argv[1:])
                return
            if argv and argv[0] == "dlux":
                from .app_permissions import SHARED_UMASK
                from .dlux_package_cli import parse_dlux_update_args, run_dlux_update

                commands = ["check", "update", "rollback", "channel"]
                if len(argv) == 1 or argv[1] in {"-h", "--help"}:
                    print(_group_usage("dlux", commands))
                    return
                if argv[1] not in commands:
                    print(_group_usage("dlux", commands), file=sys.stderr)
                    sys.exit(2)
                # These write the runtime volume (here, or in the sibling they re-run
                # in): group-writable like every other Composer writer there.
                os.umask(SHARED_UMASK)
                if argv[1] == "channel":
                    from .dlux_package_cli import parse_dlux_channel_args, run_dlux_channel

                    channel_args = parse_dlux_channel_args(argv[2:])
                    sys.exit(run_dlux_channel(channel_args, argv[2:]))
                args = parse_dlux_update_args(argv[2:], action=argv[1])
                sys.exit(run_dlux_update(args, argv[2:]))
            if argv and argv[0] == "maintenance":
                from .maintenance_cli import COMMANDS as maintenance_commands, run_maintenance

                if len(argv) == 1 or argv[1] in {"-h", "--help"}:
                    print(_group_usage("maintenance", maintenance_commands))
                    return
                if argv[1] not in maintenance_commands:
                    print(_group_usage("maintenance", maintenance_commands), file=sys.stderr)
                    sys.exit(2)
                sys.exit(run_maintenance(argv[1], argv[2:]))
            if argv and argv[0] == "relay":
                from .relay_cli import COMMANDS as relay_commands, run_relay

                if len(argv) == 1 or argv[1] in {"-h", "--help"}:
                    print(_group_usage("relay", relay_commands))
                    return
                if argv[1] not in relay_commands:
                    print(_group_usage("relay", relay_commands), file=sys.stderr)
                    sys.exit(2)
                sys.exit(run_relay(argv[1], argv[2:]))
            if argv and argv[0] == "app-permissions":
                from .app_permissions import run_cli

                sys.exit(run_cli(argv[1:]))
            if argv and argv[0] == "agent":
                commands = ["check", "update", "restart", "off", "watch", "run", "enable"]
                if len(argv) == 1 or argv[1] in {"-h", "--help"}:
                    print(_group_usage("agent", commands))
                    return
                action = argv[1]
                if action == "check":
                    from .watcher import run_agent_check

                    sys.exit(run_agent_check(parse_agent_check_args(argv[2:])))
                if action == "update":
                    self.configure_agent_update(argv[2:])
                    pipeline_configured = True
                elif action == "restart":
                    self.configure_agent_restart(argv[2:])
                    pipeline_configured = True
                elif action == "off":
                    self.configure_agent_off(argv[2:])
                    pipeline_configured = True
                elif action == "watch":
                    from .watcher import run_watch

                    sys.exit(run_watch(parse_watch_args(argv[2:])))
                elif action == "run":
                    from .agent import run_agent

                    stopping = stop_on_sigterm()
                    code = run_agent(parse_agent_args(argv[2:]))
                    sys.exit(0 if stopping["signalled"] else code)
                elif action == "enable":
                    from .agent_installer import run_enable_agent

                    sys.exit(run_enable_agent(parse_enable_agent_args(argv[2:])))
                else:
                    print(_group_usage("agent", commands), file=sys.stderr)
                    sys.exit(2)
            if argv and argv[0] == "executor":
                commands = ["run", "enable"]
                if len(argv) == 1 or argv[1] in {"-h", "--help"}:
                    print(_group_usage("executor", commands))
                    return
                if argv[1] == "run":
                    from .executor import run_executor

                    stopping = stop_on_sigterm()
                    code = run_executor(parse_executor_args(argv[2:]))
                    sys.exit(0 if stopping["signalled"] else code)
                if argv[1] == "enable":
                    from .agent_installer import run_enable_executor

                    sys.exit(run_enable_executor(parse_enable_executor_args(argv[2:])))
                print(_group_usage("executor", commands), file=sys.stderr)
                sys.exit(2)
            if not pipeline_configured:
                if argv and argv[0] in {"log", "logs"}:
                    self.handle_log(argv[1:])
                    return
                if argv and argv[0] == "check":
                    sys.exit(self.run_checkup(parse_check_args(argv[1:])))
                if argv and argv[0] in {"restart", "-r", "--restart"}:
                    self.configure_restart(argv[1:])
                    pipeline_configured = True
                elif argv and argv[0] in {"stop", "down"}:
                    self.configure_stop(argv[1:], command=argv[0])
                    pipeline_configured = True
                elif argv and argv[0] == "update":
                    self.configure_update(argv[1:])
                    pipeline_configured = True
                elif argv and argv[0] == "pull":
                    self.configure_pull(argv[1:])
                    pipeline_configured = True
                else:
                    args = parse_args()

                    if args.version:
                        print(f"composer {self.composer_version}")
                        return
                    self.no_migrate = args.no_migrate
                    self.skip_config = args.skip_config
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

            conflict = project_name_conflict(self.active_compose_files)
            if conflict:
                print(f"✖ {conflict}", file=sys.stderr)
                sys.exit(2)
            self.take_project_lock()
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
                self.mark_services_updating(
                    self.restart_service or self.restart_services or None
                )
                self.render()
                ok, out, err = self.restart_containers()
                if not ok:
                    self.sections["compose"] = ERROR
                    diagnostics = self.collect_service_diagnostics(culprits=hook_failures(f"{out}\n{err}"))
                    detail = clarify_hook_lines(self.build_failure_detail(out, err, diagnostics))
                    self.write_status("failed", error=detail)
                    self.render(f"Failed to restart containers\n\n{detail}")
                    sys.exit(1)
                self.sections["compose"] = OK

                self.sections["health"] = RUNNING
                self.render()
                health_ok, health_detail = self.monitor_health()
                if not health_ok:
                    self.sections["health"] = ERROR
                    health_detail = self.keep_site_reachable(health_detail)
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
                self.mark_services_updating(self.pull_service)
                self.write_status("pulling")
                self.render()
                if self.pin_resident is not None:
                    ok, moved, detail = self.pin_resident_pair()
                    if not ok:
                        self.sections["pull"] = ERROR
                        self.write_status("failed", error=detail)
                        self.render(detail)
                        sys.exit(1)
                    if not moved:
                        self.sections["pull"] = OK
                        self.write_status("ready")
                        self.render()
                        print(f"\n✅ {detail}")
                        return
                elif self.hold_unpinned_resident_pair() is None:
                    detail = (
                        "The resident pair runs a moving Composer tag and moves only through "
                        "'./start.sh agent update', which pins the channel's signed release. "
                        "Nothing was pulled or recreated."
                    )
                    self.sections["pull"] = ERROR
                    self.write_status("failed", error=detail)
                    self.render(detail)
                    sys.exit(2)
                ok, out, err = self.pull_images()
                if not ok:
                    self.sections["pull"] = ERROR
                    detail = self.build_failure_detail(out, err) + registry_hint(f"{out}\n{err}")
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
            self.mark_services_updating(self.up_service)
            self.write_status("recreating")
            self.render()
            if not self.discover_services():
                self.sections["compose"] = ERROR
                detail = self.last_runtime_diagnostic or "Check the compose file and environment values."
                self.write_status("failed", error=detail)
                self.render("Failed to read compose services\n\n" + detail)
                sys.exit(1)
            if self.pin_resident is None:
                self.hold_older_resident_tag()

            # An app image older than the release that migrated the database
            # is refused before anything is recreated (finding #11).
            guard_ok, guard_msg = self.downgrade_guard()
            if not guard_ok:
                self.sections["compose"] = ERROR
                self.write_status("failed", error=guard_msg)
                self.render(guard_msg)
                sys.exit(1)
            if guard_msg:
                print(f"⚠ {guard_msg}")

            # A DjangoLux image that runs as a non-root uid needs group access to
            # data a root image wrote. Granted here, before anything is recreated;
            # if it cannot be granted the running stack is left as it is.
            ready, note = self.prepare_app_data()
            if not ready:
                self.sections["compose"] = ERROR
                self.write_status("failed", error=note)
                self.render(note)
                sys.exit(1)
            if note:
                print(f"🔐 {note}")

            self.clear_interrupted_recreates()
            self.leave_out_ocr_without_engine()

            ok, out, err = self.launch_containers()
            # `up` ran the migrator whether or not it then succeeded.
            self.record_deployed_release()
            if not ok:
                self.sections["compose"] = ERROR
                diagnostics = self.collect_service_diagnostics(culprits=hook_failures(f"{out}\n{err}"))
                detail = clarify_hook_lines(self.build_failure_detail(out, err, diagnostics))
                detail = self.keep_site_reachable(detail)
                self.write_status("failed", error=detail)
                self.render(f"Failed to start containers\n\n{detail}")
                sys.exit(1)
            self.sections["compose"] = OK
            started, _ = self.ensure_edge_proxy()
            if started:
                self.emit_status("Compose", f"Started {', '.join(started)}, which Compose left stopped")

            self.sections["health"] = RUNNING
            self.render()

            health_ok, health_detail = self.monitor_health()
            if not health_ok:
                self.sections["health"] = ERROR
                health_detail = self.keep_site_reachable(health_detail)
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
                sys.exit(1)
            self.sections["post_start"] = OK
            # A crash loop slower than the settle window shows only now (R2-031).
            stable_ok, stable_detail = self.confirm_stable()
            if not stable_ok:
                self.sections["health"] = ERROR
                stable_detail = self.keep_site_reachable(stable_detail)
                self.write_status("failed", error=stable_detail)
                self.render(stable_detail)
                sys.exit(1)
            self.write_status("ready")
            self.render()

            print("\n🎉 Environment ready")
            for warning in getattr(self, "deploy_warnings", None) or []:
                print(f"⚠ {warning}")

        except KeyboardInterrupt:
            self.handle_interrupt()
            raise SystemExit(130)
        finally:
            try:
                self.cleanup()
            finally:
                self.release_project_lock()
