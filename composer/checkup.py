import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import wrappers
from .config import ConfigMixin
from .confirmation import confirm
from .proxy_cleanup import inspect_legacy_proxy_routes
from .secrets_manager import SecretsMixin
from .stack_cleanup import OBSOLETE_SERVICES

# First DjangoLux whose inline updates Composer can drive end to end.
DLUX_COMPOSER_UPDATER_MIN = (1, 8, 0)

OK = "ok"
WARN = "warn"
FAIL = "fail"

_ICONS = {OK: "✔", WARN: "⚠", FAIL: "✖"}

# In-container doctor invoked by `check --deep`. DjangoLux owns the deep,
# app-level checks; composer only relays them. Overridable so the seam does not
# hard-code a DLUX command name.
DEFAULT_DEEP_SERVICE = "web"
DEFAULT_DEEP_COMMAND = "python manage.py dlux_doctor"


def _result(level: str, name: str, message: str, fix: str = "") -> Dict[str, Any]:
    entry = {"level": level, "name": name, "message": message}
    if fix:
        entry["fix"] = fix
    return entry


class CheckupMixin(ConfigMixin, SecretsMixin):
    """`composer check` — a doctor for the *outside* of a DLUX stack.

    Composer owns host/compose/secrets/topology checks and relays the deep,
    app-level checks to the container (`--deep`). It replaces one-off migration
    commands: everything the operator must verify before/around a deploy lives
    here, and safe fixes route through the same guarded transforms.
    """

    def _check_docker(self) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        ok, out, err = self.run_command(["docker", "version", "--format", "{{.Server.Version}}"], timeout=10)
        if ok:
            results.append(_result(OK, "docker", f"Docker daemon reachable ({out.strip() or 'unknown'})."))
        else:
            results.append(
                _result(
                    FAIL,
                    "docker",
                    "Docker is not available or the daemon is unreachable.",
                    fix="Install Docker and ensure the daemon is running.",
                )
            )
            return results
        ok, out, _ = self.run_command(["docker", "compose", "version", "--short"], timeout=10)
        if ok:
            results.append(self._compose_version_result(out))
        else:
            results.append(
                _result(
                    WARN,
                    "compose",
                    "Docker Compose plugin not detected; falling back to legacy docker-compose.",
                    fix="Install the Docker Compose plugin.",
                )
            )
        return results

    def _check_compose_files(self) -> Dict[str, Any]:
        missing = [f for f in self.active_compose_files if not os.path.exists(f)]
        if missing:
            return _result(
                FAIL,
                "compose-files",
                "Compose file(s) not found: " + ", ".join(missing),
                fix="Run from the project directory, or pass -f/--file.",
            )
        return _result(OK, "compose-files", "Compose file(s) present: " + ", ".join(self.active_compose_files))

    def _check_compose_parses(self) -> Dict[str, Any]:
        if self.discover_services(silent=True):
            return _result(
                OK,
                "compose-config",
                f"Compose config resolves ({len(self.services)} service(s)).",
            )
        detail = (self.last_runtime_diagnostic or "docker compose config failed.").strip().splitlines()
        return _result(
            FAIL,
            "compose-config",
            "Compose config does not resolve: " + (detail[0] if detail else "unknown error"),
            fix="Fix the reported YAML/interpolation/network error; missing env vars are reported separately.",
        )

    def _check_secrets(self) -> Dict[str, Any]:
        candidates = self.plaintext_env_candidates()
        if not candidates:
            return _result(
                WARN,
                "secrets",
                "No plaintext env file found (.env / secrets/.env / .secrets/.env).",
                fix="Create the env file the deployment expects, or confirm secrets come from the environment.",
            )
        path = candidates[0]
        try:
            values = self.parse_env_file(path)
        except OSError as exc:
            return _result(
                FAIL,
                "secrets",
                f"Secrets file {path} exists but is not readable: {exc}",
                fix="Fix file permissions/ownership (see start.sh's readability guard).",
            )
        except ValueError as exc:
            return _result(FAIL, "secrets", f"Secrets file {path} could not be parsed: {exc}")
        if not values:
            return _result(
                FAIL,
                "secrets",
                f"Secrets file {path} contains no environment values.",
                fix="Populate the env file; an empty file silently falls through to compose defaults.",
            )
        return _result(OK, "secrets", f"Secrets source {path} readable ({len(values)} value(s)).")

    def _check_required_vars(self) -> Dict[str, Any]:
        required = self.required_compose_vars()
        if not required:
            return _result(OK, "env-vars", "No externally-required compose variables are unmet.")
        available = set(os.environ)
        inherited = self.inherited_secret_keys() or []
        available.update(inherited)
        for candidate in self.plaintext_env_candidates():
            try:
                available.update(self.parse_env_file(candidate).keys())
            except (OSError, ValueError):
                continue
        missing = sorted(required - available)
        if missing:
            return _result(
                FAIL,
                "env-vars",
                "Compose references variables with no value: " + ", ".join(missing),
                fix="Add them to the env file or the environment (${VAR:-default} would make them optional).",
            )
        return _result(OK, "env-vars", f"All {len(required)} required compose variable(s) are supplied.")

    # DjangoLux projects generated from the 1.8.0 scaffold run their runtime
    # reconcile and migrations as Compose init containers (`pre_start`), which
    # landed in Compose 5.3.0. An older plugin silently ignores the key, so the
    # stack would start with no migrations applied and every gated service
    # waiting forever — a check is the only way that surfaces before deploy.
    COMPOSE_INIT_CONTAINER_MIN = (5, 3, 0)

    @staticmethod
    def _parse_compose_version(raw):
        cleaned = str(raw or "").strip().lstrip("vV").split("-")[0].split("+")[0]
        parts = cleaned.split(".")[:3]
        if not parts or not parts[0]:
            return None
        try:
            numbers = [int(part) for part in parts]
        except ValueError:
            return None
        # Pad to three components: "5.3" means 5.3.0, but the bare tuple (5, 3)
        # compares as LESS than (5, 3, 0) and would be refused.
        return tuple(numbers + [0] * (3 - len(numbers)))

    def _compose_version_result(self, raw) -> Dict[str, Any]:
        version = self._parse_compose_version(raw)
        minimum = ".".join(map(str, self.COMPOSE_INIT_CONTAINER_MIN))
        if version is None:
            return _result(
                WARN, "compose",
                f"Docker Compose present but its version could not be read "
                f"({str(raw).strip() or 'no output'}); {minimum}+ is required for "
                "DjangoLux init containers.",
            )
        shown = ".".join(map(str, version))
        if version < self.COMPOSE_INIT_CONTAINER_MIN:
            return _result(
                FAIL, "compose",
                f"Docker Compose {shown} predates init containers (pre_start), which "
                f"DjangoLux projects use to run migrations before the stack starts. "
                f"On this version those steps are ignored and gated services never "
                f"start.",
                fix=f"Upgrade the Docker Compose plugin to {minimum} or newer.",
            )
        return _result(OK, "compose", f"Docker Compose {shown} present (init containers supported).")

    def _check_topology(self) -> Dict[str, Any]:
        services = set(self.services)
        has_agent = "composer-agent" in services
        has_executor = "composer-executor" in services
        has_legacy = "composer-updater" in services
        has_proxy = "docker-socket-proxy" in services
        if has_agent and has_legacy:
            return _result(
                FAIL,
                "topology",
                "Conflicting composer-agent and composer-updater services detected.",
                fix=(
                    "Review the mixed topology manually; Composer refuses to guess "
                    "which generated block owns the deployment."
                ),
            )
        if has_executor and not has_agent:
            return _result(
                WARN,
                "topology",
                "composer-executor is present without composer-agent; the resident pair is incomplete.",
                fix="Both roles are required. Recreate the composer-agent + composer-executor pair.",
            )
        if has_agent:
            if has_executor:
                # Hardened: Docker authority is isolated in the executor; the
                # network-facing agent has none. The proxy, if present, is the
                # agent's read-only path (health/availability).
                note = "Hardened topology: composer-executor holds Docker authority; composer-agent has none."
                if has_proxy:
                    note += " docker-socket-proxy provides the agent's read-only Docker access."
                else:
                    note += " No docker-socket-proxy (agent performs no Docker reads)."
                return _result(OK, "topology", note)
            # Legacy-agent: valid and supported (backwards compatible, so this
            # never FAILs), but the network-facing agent still drives Docker
            # directly. A WARN surfaces the fix hint and nudges toward hardening.
            if not has_proxy:
                return _result(
                    WARN,
                    "topology",
                    "Managed by composer-agent. docker-socket-proxy is missing, so the agent cannot drive Docker.",
                    fix="Re-run 'composer check --fix' or 'composer agent enable --apply'.",
                )
            return _result(
                WARN,
                "topology",
                "Managed by composer-agent (the agent drives Docker directly through docker-socket-proxy).",
                fix=(
                    "Harden with 'composer check --fix' (runs executor enable) or "
                    "'composer executor enable --apply': moves Docker authority off the network-facing "
                    "agent into composer-executor and demotes docker-socket-proxy to read-only. "
                    "See docs/executor-hardening.md."
                ),
            )
        if has_legacy:
            return _result(
                WARN,
                "topology",
                "Legacy composer-updater topology detected.",
                fix="Migrate with 'composer check --fix' (runs agent enable) or 'composer agent enable --apply'.",
            )
        return _result(
            FAIL,
            "topology",
            "No Composer service found. Since DjangoLux 1.8.0 the updater hands inline "
            "updates to Composer, so a Composer service is part of the deployment — not "
            "only the deploying machine. This stack has no update path.",
            fix=(
                "Run 'composer check --fix' to install docker-socket-proxy, "
                "composer-executor and composer-agent."
            ),
        )

    def _check_removed_services(self) -> Dict[str, Any]:
        present = sorted(OBSOLETE_SERVICES.intersection(self.services))
        if not present:
            return _result(OK, "removed-services", "No obsolete DLUX services detected.")
        return _result(
            WARN,
            "removed-services",
            "Obsolete DLUX service(s) detected: " + ", ".join(present) + ".",
            fix=(
                "Run 'composer check --fix' to remove their Compose service definitions; "
                "named volumes and stored data are preserved."
            ),
        )

    # The composer-side loops that can process a DjangoLux package request.
    PACKAGE_LOOP_SERVICES = ("composer-executor", "composer-agent", "composer-updater")

    def _package_loop_service(self) -> Optional[str]:
        services = set(self.services)
        for name in self.PACKAGE_LOOP_SERVICES:
            if name in services:
                return name
        return None

    def _mounts_dlux_runtime(self, service: str) -> Optional[bool]:
        """Does `service` mount the runtime volume? None when it can't be read.

        The package trigger, its ack and the availability report all live on that
        volume; a loop that cannot see it cannot execute an update.
        """
        ok, out, _err = self.run_docker_compose(["config", "--format", "json"], timeout=20)
        if not ok:
            return None
        try:
            model = json.loads(out)
        except ValueError:
            return None
        definition = (model.get("services") or {}).get(service)
        if not isinstance(definition, dict):
            return None
        for mount in definition.get("volumes") or []:
            source = mount.get("source") if isinstance(mount, dict) else str(mount).split(":")[0]
            if str(source or "").endswith("dlux_runtime"):
                return True
        return False

    def _check_dlux_updater_executor(self) -> Dict[str, Any]:
        """Is this stack ready for DjangoLux to hand its updates to Composer?

        DjangoLux 1.8.0 stops performing inline updates in-container: it writes an
        intent file on the runtime volume and Composer stages, verifies, activates
        and health-gates the release from outside the container being swapped —
        which an in-container updater cannot do for a release that stops it from
        starting. 1.9.0 deletes the in-container executor code.

        The `dlux-updater` SERVICE is not retired by any of this, and this check
        never proposes removing it. It also runs `dlux_reconcile` and `migrator`,
        and `web` gates its own start on its health; it is the queue worker that
        writes the hand-off. What 1.9.0 removes lives inside it, not around it.
        """
        if "dlux-updater" not in set(self.services):
            return _result(OK, "dlux-updater-executor",
                           "No in-container DjangoLux update executor.")
        runtime = self._dlux_runtime_version()
        minimum = ".".join(map(str, DLUX_COMPOSER_UPDATER_MIN))
        if runtime is None:
            return _result(
                WARN, "dlux-updater-executor",
                "A 'dlux-updater' service is present but its DjangoLux version could "
                "not be read, so its update path cannot be classified.",
                fix="Check the service starts, then re-run 'composer check'.",
            )

        loop = self._package_loop_service()
        if loop is None:
            return _result(
                FAIL, "dlux-updater-executor",
                f"No composer service is running an update loop, so a DjangoLux "
                f"{minimum} package request would never be executed. Composer is a "
                "required service for DjangoLux updates, not an optional companion.",
                fix="Run 'composer check --fix' to install the Composer services.",
            )
        mounted = self._mounts_dlux_runtime(loop)
        if mounted is False:
            return _result(
                FAIL, "dlux-updater-executor",
                f"'{loop}' does not mount the dlux_runtime volume, so it cannot see "
                "DjangoLux's update requests or publish what is available.",
                fix=(
                    "Add 'dlux_runtime:/opt/dlux-runtime:rw' to that service's volumes, "
                    "then recreate it."
                ),
            )

        if runtime < DLUX_COMPOSER_UPDATER_MIN:
            got = ".".join(map(str, runtime))
            return _result(
                OK, "dlux-updater-executor",
                f"DjangoLux {got} still updates itself in-container; '{loop}' is ready "
                f"to take over the moment the image ships {minimum} or newer. Nothing "
                "to change now.",
            )
        return _result(
            OK, "dlux-updater-executor",
            f"DjangoLux {'.'.join(map(str, runtime))} hands inline updates to '{loop}'. "
            "The 'dlux-updater' service stays — it also runs dlux_reconcile and "
            "migrator, and web gates its start on it.",
        )

    def _check_proxy_routes(self) -> Dict[str, Any]:
        inspection = inspect_legacy_proxy_routes(".")
        if inspection["unsupported"]:
            return _result(
                FAIL,
                "proxy-routes",
                "Unrecognized pgAdmin proxy route(s): "
                + ", ".join(inspection["unsupported"])
                + ".",
                fix="Review these custom routes manually before removing pgAdmin.",
            )
        if inspection["recognized"]:
            return _result(
                WARN,
                "proxy-routes",
                "Legacy pgAdmin proxy route(s) detected: "
                + ", ".join(inspection["recognized"])
                + ".",
                fix=(
                    "Run 'composer check --fix' to archive, validate, remove, "
                    "and reload the active proxy."
                ),
            )
        return _result(OK, "proxy-routes", "No legacy pgAdmin proxy routes detected.")

    def _dlux_runtime_version(self, service: Optional[str] = None):
        """(major, minor, patch) of the dlux baked into the project image, read
        from the image via `dlux --version`. Uses `run --no-deps` (a fresh
        container) so it works even when dlux-updater is crash-looping, and never
        starts db/redis. Returns None when it can't be determined."""
        from .agent_installer import parse_dlux_version

        service = service or (
            "dlux-updater" if "dlux-updater" in set(self.services) else "web"
        )
        ok, out, _err = self.run_docker_compose(
            ["run", "--rm", "--no-deps", "--entrypoint", "python", "-T",
             service, "-m", "dlux", "--version"],
            timeout=60,
        )
        return parse_dlux_version(out) if ok else None

    def _resident_agent_version(self) -> Optional[str]:
        if "composer-agent" not in set(self.services):
            return None
        ok, out, _ = self.run_docker_compose(
            ["exec", "-T", "composer-agent", "cat", "/app/VERSION"], timeout=10
        )
        if ok and out.strip():
            return out.strip().splitlines()[0]
        return None

    def _check_versions(self) -> Dict[str, Any]:
        deployer = self.composer_version
        resident = self._resident_agent_version()
        if resident is None:
            return _result(
                OK,
                "versions",
                f"Deploying composer {deployer}; resident agent version unavailable (not running or not enrolled).",
            )
        if resident == deployer:
            return _result(OK, "versions", f"Deploying composer and resident agent both {deployer}.")
        return _result(
            WARN,
            "versions",
            f"Version drift: deploying composer {deployer}, resident composer-agent {resident}.",
            fix="Update the resident agent's image so both match, if that matters for the change you're shipping.",
        )

    def _check_wrappers(self) -> List[Dict[str, Any]]:
        """Report drift between the project's launcher wrappers and this image.

        Composer owns start.sh/start.ps1 (see `composer/wrappers.py`), and the
        image it runs from carries the reference copies, so this is the one
        check that needs neither the stack up nor a network.
        """
        results: List[Dict[str, Any]] = []
        for entry in wrappers.inspect_wrappers("."):
            name = entry["name"]
            baked = entry["baked_version"]
            found = entry["version"]
            status = entry["status"]
            if status == wrappers.CURRENT:
                results.append(_result(OK, f"wrapper:{name}", f"{name} is at wrapper version {baked}."))
            elif status == wrappers.MISSING:
                results.append(
                    _result(
                        WARN,
                        f"wrapper:{name}",
                        f"{name} is absent; this project cannot be launched the way the others are.",
                        fix="Run 'composer check --fix' to write the copy baked into this image.",
                    )
                )
            elif status == wrappers.UNVERSIONED:
                results.append(
                    _result(
                        WARN,
                        f"wrapper:{name}",
                        f"{name} predates wrapper versioning and differs from version {baked}.",
                        fix="Run 'composer check --fix'; the current file is archived under .xclude/ first.",
                    )
                )
            elif status == wrappers.STALE:
                results.append(
                    _result(
                        WARN,
                        f"wrapper:{name}",
                        f"{name} is wrapper version {found}, this composer ships {baked}.",
                        fix="Run 'composer check --fix' to update it.",
                    )
                )
            elif status == wrappers.MODIFIED:
                results.append(
                    _result(
                        WARN,
                        f"wrapper:{name}",
                        f"{name} declares wrapper version {found} but its contents do not match "
                        "what that version shipped — it has local edits.",
                        fix=(
                            "Diff it against /app/wrappers/ inside the composer image. "
                            "'composer check --fix' replaces it, archiving your copy under .xclude/."
                        ),
                    )
                )
            elif status == wrappers.AHEAD:
                results.append(
                    _result(
                        WARN,
                        f"wrapper:{name}",
                        f"{name} is wrapper version {found}, newer than the {baked} this composer "
                        "ships — the image is behind, not the wrapper.",
                        fix="Run './start.sh self update'. Do not 'check --fix' this; it would downgrade the wrapper.",
                    )
                )
        return results

    def _run_deep(self, service: str, command: str) -> Dict[str, Any]:
        argv = command.split()
        ok, out, err = self.run_docker_compose(["exec", "-T", service] + argv, timeout=120)
        detail = (out or err or "").strip()
        if ok:
            return _result(OK, "deep", f"In-container doctor ({service}: {command}) passed.\n{detail}".rstrip())
        return _result(
            WARN,
            "deep",
            f"In-container doctor ({service}: {command}) unavailable or reported issues.\n{detail}".rstrip(),
            fix="Ensure the service is running and provides the deep-check command (override with --deep-command).",
        )

    def _requested_channel(self, args) -> str:
        if getattr(args, "beta", False):
            return "beta"
        if getattr(args, "stable", False):
            return "stable"
        return ""

    def _check_composer_channel(self) -> Dict[str, Any]:
        """Does the wrapper's channel agree with what the stack actually runs?

        These are two different files, changed by two different operations, and
        an operator debugging a Composer beta needs to know when only one of them
        moved — a deployer on :beta talking to an agent on :latest is a
        configuration nobody chose and nothing else reports.
        """
        from . import channel_config
        from .agent_installer import composer_images_in_block

        state = channel_config.describe(".")
        note = f"Composer channel: {state['channel']} ({state['image']})"
        if state["pinned"]:
            note += " — COMPOSER_SELF_IMAGE pin overrides the channel"
        try:
            declared = set(composer_images_in_block(self._compose_contents()))
        except Exception:
            declared = set()
        if not declared:
            return _result(OK, "composer-channel", note)
        expected = state["channel_image"]
        drifted = sorted(image for image in declared if image != expected)
        if drifted:
            return _result(
                WARN,
                "composer-channel",
                f"{note}, but the resident pair runs {', '.join(drifted)}.",
                fix=f"Run 'composer check --{state['channel']}' to move both together.",
            )
        return _result(OK, "composer-channel", f"{note}; resident pair agrees.")

    def _compose_contents(self) -> str:
        for candidate in (self.compose_file, "compose.yml", "docker-compose.yml"):
            if not candidate:
                continue
            path = Path(candidate)
            if path.is_file():
                return path.read_text(encoding="utf-8")
        return ""

    def _switch_channel(self, args, channel: str) -> List[Dict[str, Any]]:
        """Apply a requested channel change, or explain why it did not happen."""
        from .agent_installer import AgentInstallError, switch_composer_channel
        from .channel_config import image_for_channel

        image = image_for_channel(channel)
        try:
            preview = switch_composer_channel(
                ".", channel=channel, compose_file=args.file or "", apply=False,
            )
        except AgentInstallError as exc:
            return [_result(FAIL, "composer-channel-switch", str(exc))]

        if not confirm(
            f"Switch this project to the {channel} Composer channel ({image})",
            [
                "The wrapper and the resident agent/executor pair both move.",
                "Running containers are recreated on the new image.",
            ],
            assume_yes=args.yes,
        ):
            return [_result(WARN, "composer-channel-switch", "Channel switch declined.")]

        try:
            outcome = switch_composer_channel(
                ".", channel=channel, compose_file=args.file or "", apply=True,
            )
        except AgentInstallError as exc:
            return [_result(FAIL, "composer-channel-switch", str(exc))]

        if not outcome.get("applied") and not outcome.get("channel_file"):
            return [_result(
                FAIL,
                "composer-channel-switch",
                f"The switch to {channel} did not complete; the project is unchanged.",
            )]
        detail = f"Composer channel set to {channel} ({image})."
        if not preview.get("files"):
            detail += " The resident pair already ran that image."
        return [_result(OK, "composer-channel-switch", detail)]

    def run_checkup(self, args) -> int:
        self.compose_file = args.file
        self.dev_mode = args.dev
        self.resolve_active_compose_files()

        # Before the diagnosis, so the checks below report the state the
        # operator asked for rather than the one they are leaving.
        switched: List[Dict[str, Any]] = []
        requested = self._requested_channel(args)
        if requested:
            switched = self._switch_channel(args, requested)

        results: List[Dict[str, Any]] = []
        results.extend(self._check_docker())
        results.append(self._check_compose_files())
        results.append(self._check_compose_parses())
        results.append(self._check_secrets())
        results.extend(self._check_wrappers())
        results.append(self._check_composer_channel())
        if self.services:
            results.append(self._check_required_vars())
            results.append(self._check_topology())
            results.append(self._check_removed_services())
            results.append(self._check_dlux_updater_executor())
            results.append(self._check_proxy_routes())
            results.append(self._check_versions())
            if args.deep:
                results.append(self._run_deep(args.deep_service, args.deep_command))

        fixed = switched + (self._maybe_fix(args, results) if args.fix else [])

        if args.json:
            import json

            print(json.dumps({"results": results, "fixes": fixed}, indent=2))
        else:
            self._print_checkup(results, fixed)

        return 1 if any(r["level"] == FAIL for r in results + fixed) else 0

    def _maybe_fix(self, args, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        fixes: List[Dict[str, Any]] = []
        legacy = "composer-updater" in set(self.services) and "composer-agent" not in set(self.services)
        needs_hardening = (
            "composer-agent" in set(self.services) and "composer-executor" not in set(self.services)
        )
        # Already-hardened stacks are not caught by legacy/needs_hardening, so
        # targeted agent-block repairs would otherwise be skipped. A dry-run
        # reports a file change only when something must be normalized.
        needs_agent_block_migration = False
        if "composer-executor" in set(self.services):
            try:
                from .agent_installer import enable_executor

                needs_agent_block_migration = bool(
                    enable_executor(".", compose_file=args.file or "", apply=False).get("files")
                )
            except Exception:
                needs_agent_block_migration = False
        # Older DjangoLux scaffolds may still import the local tools supervisor
        # or bind-mount ./tools into /app/tools. Detect it from the compose (a
        # dry-run reports a file change), then gate on the dlux version the IMAGE
        # actually ships — the only authoritative signal on a pulled deployment,
        # which has no requirements.txt.
        needs_updater_migration = False
        updater_migration_blocked = ""
        if self.services:
            from .agent_installer import dlux_runtime_migration_floor, migrate_dlux_updater

            try:
                legacy_present = bool(
                    migrate_dlux_updater(".", compose_file=args.file or "", apply=False).get("files")
                )
            except Exception:
                legacy_present = False
            if legacy_present:
                try:
                    required = dlux_runtime_migration_floor(".", compose_file=args.file or "")
                except Exception as exc:
                    updater_migration_blocked = (
                        f"Compose still references the local tools runtime, but Composer could "
                        f"not classify the required packaged module floor: {exc}"
                    )
                else:
                    runtime = self._dlux_runtime_version()
                    if runtime is not None and runtime >= required:
                        needs_updater_migration = True
                    else:
                        got = ".".join(map(str, runtime)) if runtime else "unknown"
                        minimum = ".".join(map(str, required))
                        updater_migration_blocked = (
                            f"Compose still references the local tools runtime, but the project image ships dlux "
                            f"{got} (needs >= {minimum} for the packaged runtime). Update the project "
                            "image, then re-run 'composer check --fix'."
                        )
        # DjangoLux 1.8.0 hands inline updates to Composer, so a Composer service
        # is part of a DjangoLux deployment, not just the deploying machine. A
        # stack with none gets the hardened trio installed.
        needs_install = False
        if not (set(self.services) & {"composer-agent", "composer-executor", "composer-updater"}):
            try:
                from .agent_installer import install_composer_stack

                needs_install = bool(
                    install_composer_stack(".", compose_file=args.file or "", apply=False).get("files")
                )
            except Exception:
                needs_install = False
        # Retire dlux-updater into Compose init containers on celery. Gated on
        # the dlux the IMAGE ships: on an older release that service still owns
        # the update path, and on Compose older than 5.3.0 `pre_start` is
        # silently ignored, which would leave the stack unmigrated on boot.
        needs_init_containers = False
        init_containers_blocked = ""
        if "dlux-updater" in set(self.services):
            from .agent_installer import migrate_dlux_init_containers

            compose_ok, compose_out, _ = self.run_command(
                ["docker", "compose", "version", "--short"], timeout=10)
            compose_version = self._parse_compose_version(compose_out) if compose_ok else None
            runtime = self._dlux_runtime_version()
            minimum = ".".join(map(str, DLUX_COMPOSER_UPDATER_MIN))
            if compose_version is None or compose_version < self.COMPOSE_INIT_CONTAINER_MIN:
                init_containers_blocked = (
                    "'dlux-updater' can be retired into Compose init containers, but this "
                    f"host's Docker Compose is older than "
                    f"{'.'.join(map(str, self.COMPOSE_INIT_CONTAINER_MIN))} (it would ignore "
                    "the pre_start steps and boot unmigrated). Upgrade the Compose plugin, "
                    "then re-run 'composer check --fix'."
                )
            elif runtime is None or runtime < DLUX_COMPOSER_UPDATER_MIN:
                got = ".".join(map(str, runtime)) if runtime else "unknown"
                init_containers_blocked = (
                    f"'dlux-updater' still owns the update path for DjangoLux {got}; it is "
                    f"retired once the image ships {minimum} or newer."
                )
            else:
                try:
                    needs_init_containers = bool(
                        migrate_dlux_init_containers(
                            ".", compose_file=args.file or "", apply=False).get("files"))
                except Exception as exc:
                    init_containers_blocked = f"Cannot retire 'dlux-updater': {exc}"
        needs_dev_override_migration = False
        dev_override_blocked = ""
        dev_override_file = ""
        base_compose_file = ""
        if getattr(args, "dev", False):
            for file in self.active_compose_files:
                if os.path.basename(file) == "compose.dev.yml":
                    dev_override_file = file
                elif not base_compose_file:
                    base_compose_file = file
            if dev_override_file:
                try:
                    from .agent_installer import migrate_dlux_dev_override

                    dev_change = bool(
                        migrate_dlux_dev_override(
                            ".",
                            compose_file=dev_override_file,
                            base_file=base_compose_file or "compose.yml",
                            apply=False,
                        ).get("files")
                    )
                except Exception as exc:
                    dev_override_blocked = f"Cannot normalize compose.dev.yml: {exc}"
                else:
                    if dev_change and init_containers_blocked:
                        dev_override_blocked = (
                            "compose.dev.yml still carries updater-era overrides, but "
                            "the base dlux-updater retirement is blocked: "
                            + init_containers_blocked
                        )
                    else:
                        needs_dev_override_migration = dev_change
        # A native Compose post_start hook creates two runners. Existing DLUX
        # updater projects can also be missing both the native hook and label,
        # which creates zero runners. The guarded transform repairs either form.
        needs_post_start_migration = False
        try:
            from .agent_installer import enable_post_start_label

            needs_post_start_migration = bool(
                enable_post_start_label(".", compose_file=args.file or "", apply=False).get("files")
            )
        except Exception:
            needs_post_start_migration = False
        needs_restart_labels = False
        try:
            from .agent_installer import normalize_restart_labels

            needs_restart_labels = bool(
                normalize_restart_labels(".", compose_file=args.file or "", apply=False).get("files")
            )
        except Exception:
            needs_restart_labels = False
        if updater_migration_blocked:
            fixes.append(_result(WARN, "dlux-updater-runtime", updater_migration_blocked))
        if init_containers_blocked:
            fixes.append(_result(WARN, "dlux-init-containers", init_containers_blocked))
        if dev_override_blocked:
            fixes.append(_result(WARN, "dev-compose", dev_override_blocked))
        # An AHEAD wrapper is deliberately not fixable: the image is the stale
        # side there, and writing the baked copy would downgrade the project.
        stale_wrappers = [
            entry for entry in wrappers.inspect_wrappers(".") if entry["status"] in wrappers.FIXABLE
        ]
        obsolete = sorted(OBSOLETE_SERVICES.intersection(self.services))
        proxy_inspection = inspect_legacy_proxy_routes(".")
        if proxy_inspection["unsupported"]:
            fixes.append(
                _result(
                    FAIL,
                    "fix:proxy-routes",
                    "Refusing to rewrite unrecognized pgAdmin proxy routes: "
                    + ", ".join(proxy_inspection["unsupported"]),
                )
            )
            return fixes
        proxy_routes = proxy_inspection["recognized"]
        if (not legacy and not obsolete and not proxy_routes and not needs_hardening
                and not needs_agent_block_migration and not needs_updater_migration
                and not needs_post_start_migration and not stale_wrappers
                and not needs_install and not needs_init_containers
                and not needs_restart_labels and not needs_dev_override_migration):
            return fixes
        consequences = []
        if stale_wrappers:
            consequences.append(
                "Replace launcher wrappers with the copies baked into this composer image: "
                + ", ".join(f"{entry['name']} ({entry['status']})" for entry in stale_wrappers)
                + "."
            )
            if any(entry["status"] == wrappers.MODIFIED for entry in stale_wrappers):
                consequences.append(
                    "One or more of those wrappers carries local edits; the current file is "
                    "archived under .xclude/ before it is replaced."
                )
        if obsolete:
            consequences.append(
                "Remove Compose service definitions: "
                + ", ".join(obsolete)
                + " (named volumes and stored data are kept)."
            )
            consequences.append(
                "Stop and remove only those obsolete service containers."
            )
        if proxy_routes:
            consequences.append(
                "Archive, validate, and remove pgAdmin routes from: "
                + ", ".join(proxy_routes)
                + "."
            )
            consequences.append(
                "Reload the active proxy, or restart only Nginx when its live "
                "configuration is generated from a template."
            )
        if legacy:
            consequences.extend(
                [
                    "Migrate composer-updater to composer-agent.",
                    "Create or refresh docker-socket-proxy and composer-agent.",
                    "Then harden that agent topology into composer-executor in the same run.",
                ]
            )
        if needs_init_containers:
            consequences.append(
                "Retire the dlux-updater service: its runtime reconcile and migrations "
                "become Compose init containers (pre_start) on celery, its depends_on "
                "edges are removed, celery gains write access to the runtime volume and "
                "staticfiles, and web's org.dlux.post-start migrator hook is dropped "
                "(it would now be a second, redundant run). The dlux_runtime volume and "
                "its releases are kept."
            )
        if needs_dev_override_migration:
            consequences.append(
                "Normalize compose.dev.yml for the init-container topology: remove the "
                "development dlux-updater override, keep celery's dlux_runtime mount "
                "read-write, and disable inline updates in dev containers."
            )
        if needs_install:
            consequences.append(
                "Install the Composer services this stack is missing (docker-socket-proxy, "
                "composer-executor, composer-agent) plus their volumes and the docker_proxy "
                "network. DjangoLux 1.8.0 hands inline updates to Composer, so without them "
                "this deployment has no update path."
            )
        if needs_hardening:
            consequences.extend(
                [
                    "Harden composer-agent into the executor topology: add composer-executor "
                    "(sole Docker-write authority), demote docker-socket-proxy to read-only, and "
                    "keep the agent read-only.",
                    "Recreate docker-socket-proxy, composer-executor, and composer-agent.",
                ]
            )
        if needs_agent_block_migration:
            consequences.append(
                "Normalize the generated Composer resident block: use nested "
                "agent/executor run commands and add cap_add: DAC_READ_SEARCH to "
                "composer-executor when missing so it can read the project's 0600 "
                ".secrets/.env to deploy."
            )
        if needs_updater_migration:
            consequences.append(
                "Normalize DjangoLux runtime wiring: replace local tools.dlux_runtime_supervisor "
                "commands with python -m dlux.updater.supervisor, replace local tools.smtp_relay "
                "commands with python -m dlux.smtp_relay, remove generated local tools/ bind "
                "mounts, and add the dlux-updater pre-migration dlux_reconcile guard when that "
                "service still exists."
            )
        if needs_post_start_migration:
            consequences.append(
                "Replace the native Compose post_start hook with the org.dlux.post-start "
                "label composer reads, so Compose stops running an unflagged second copy "
                "alongside composer's own flagged run."
            )
        if needs_restart_labels:
            consequences.append(
                "Add missing org.dlux.restart labels to generated DLUX stack services so "
                "Composer can classify safe versus protected restart targets from the "
                "Compose file."
            )
        consequences.extend(
            [
                "Validate the candidate with docker compose config before replacement.",
                "Preserve original deployment files under .xclude/.",
            ]
        )
        if not confirm(
            "composer check --fix will apply safe stack migrations",
            consequences,
            assume_yes=getattr(args, "yes", False),
        ):
            fixes.append(_result(WARN, "fix", "Changes declined."))
            return fixes

        if stale_wrappers:
            from pathlib import Path

            from .stack_cleanup import _archive_root

            root = wrappers.baked_root()
            try:
                archive = _archive_root(Path("."))
                for entry in stale_wrappers:
                    wrappers.install_wrapper(Path("."), entry["name"], root, archive)
                fixes.append(
                    _result(
                        OK,
                        "fix:wrappers",
                        "Updated "
                        + ", ".join(entry["name"] for entry in stale_wrappers)
                        + f" to wrapper version {stale_wrappers[0]['baked_version']}. Backup: {archive}",
                    )
                )
            except OSError as exc:
                fixes.append(_result(FAIL, "fix:wrappers", f"Could not update the wrappers: {exc}"))

        if obsolete or proxy_routes:
            from .stack_cleanup import StackCleanupError, remove_obsolete_services

            environment = self.build_compose_env()
            for candidate in self.plaintext_env_candidates():
                try:
                    environment.update(self.parse_env_file(candidate))
                    break
                except (OSError, ValueError):
                    continue
            try:
                outcome = remove_obsolete_services(
                    ".",
                    self.active_compose_files,
                    environment=environment,
                )
                not_found = sorted(set(obsolete) - set(outcome["removed_services"]))
                if not_found:
                    raise StackCleanupError(
                        "Could not locate removable service blocks for: "
                        + ", ".join(not_found)
                    )
                changes = []
                if outcome["removed_services"]:
                    changes.append(
                        "removed "
                        + ", ".join(outcome["removed_services"])
                        + " service definitions and containers"
                    )
                if outcome["proxy_files"]:
                    changes.append(
                        "removed pgAdmin routes from "
                        + ", ".join(outcome["proxy_files"])
                    )
                if outcome["proxy_services_reloaded"]:
                    changes.append(
                        "reloaded " + ", ".join(outcome["proxy_services_reloaded"])
                    )
                if outcome["proxy_services_restarted"]:
                    changes.append(
                        "restarted " + ", ".join(outcome["proxy_services_restarted"])
                    )
                fixes.append(
                    _result(
                        OK,
                        "fix:obsolete-stack",
                        "; ".join(changes)
                        + "; post-fix checks passed. Backup: "
                        + (outcome.get("backup_root") or "n/a"),
                    )
                )
            except StackCleanupError as exc:
                fixes.append(_result(FAIL, "fix:obsolete-stack", f"Cleanup failed: {exc}"))
                return fixes

        if needs_init_containers:
            from .agent_installer import AgentInstallError, migrate_dlux_init_containers

            try:
                outcome = migrate_dlux_init_containers(
                    ".", compose_file=args.file or "", apply=True)
                fixes.append(_result(
                    OK, "fix:dlux-init-containers",
                    "Retired dlux-updater into Compose init containers on celery. "
                    f"Apply with '{outcome.get('command')}'. Backup: "
                    + (outcome.get("backup_root") or "n/a"),
                ))
            except AgentInstallError as exc:
                fixes.append(_result(
                    FAIL, "fix:dlux-init-containers", f"Retirement failed: {exc}"))
        if needs_dev_override_migration:
            from .agent_installer import AgentInstallError, migrate_dlux_dev_override

            init_failed = any(
                entry["name"] == "fix:dlux-init-containers" and entry["level"] == FAIL
                for entry in fixes
            )
            if not init_failed:
                try:
                    outcome = migrate_dlux_dev_override(
                        ".",
                        compose_file=dev_override_file,
                        base_file=base_compose_file or "compose.yml",
                        apply=True,
                    )
                    if outcome.get("files"):
                        fixes.append(
                            _result(
                                OK,
                                "fix:dev-compose",
                                "Normalized compose.dev.yml for the init-container topology. Backup: "
                                + (outcome.get("backup_root") or "n/a"),
                            )
                        )
                except AgentInstallError as exc:
                    fixes.append(_result(FAIL, "fix:dev-compose", f"Dev override migration failed: {exc}"))
        if needs_install:
            from .agent_installer import AgentInstallError, install_composer_stack

            try:
                outcome = install_composer_stack(".", compose_file=args.file or "", apply=True)
                fixes.append(_result(
                    OK, "fix:install-composer",
                    "Installed docker-socket-proxy, composer-executor and composer-agent. "
                    f"Start them with '{outcome.get('command')}'. Backup: "
                    + (outcome.get("backup_root") or "n/a"),
                ))
            except AgentInstallError as exc:
                fixes.append(_result(FAIL, "fix:install-composer", f"Install failed: {exc}"))
        if legacy:
            from .agent_installer import AgentInstallError, enable_agent

            try:
                outcome = enable_agent(".", compose_file=args.file or "", apply=True)
                fixes.append(
                    _result(OK, "fix:agent-enable", "Migrated to composer-agent. Backup: " + (outcome.get("backup_root") or "n/a"))
                )
            except AgentInstallError as exc:
                fixes.append(_result(FAIL, "fix:agent-enable", f"Migration failed: {exc}"))
            else:
                from .agent_installer import enable_executor

                try:
                    outcome = enable_executor(".", compose_file=args.file or "", apply=True)
                    fixes.append(
                        _result(
                            OK,
                            "fix:executor-enable",
                            "Hardened migrated composer-agent into the executor topology. Backup: "
                            + (outcome.get("backup_root") or "n/a"),
                        )
                    )
                except AgentInstallError as exc:
                    fixes.append(_result(FAIL, "fix:executor-enable", f"Hardening failed: {exc}"))
        if needs_hardening:
            from .agent_installer import AgentInstallError, enable_executor

            try:
                outcome = enable_executor(".", compose_file=args.file or "", apply=True)
                fixes.append(
                    _result(
                        OK,
                        "fix:executor-enable",
                        "Hardened composer-agent into the executor topology. Backup: "
                        + (outcome.get("backup_root") or "n/a"),
                    )
                )
            except AgentInstallError as exc:
                fixes.append(_result(FAIL, "fix:executor-enable", f"Hardening failed: {exc}"))
        if needs_agent_block_migration:
            from .agent_installer import AgentInstallError, enable_executor

            try:
                outcome = enable_executor(".", compose_file=args.file or "", apply=True)
                fixes.append(
                    _result(
                        OK,
                        "fix:secrets-read-cap",
                        "Added cap_add: DAC_READ_SEARCH to composer-executor. Recreate it to apply. Backup: "
                        + (outcome.get("backup_root") or "n/a"),
                    )
                )
            except AgentInstallError as exc:
                fixes.append(_result(FAIL, "fix:secrets-read-cap", f"Capability repair failed: {exc}"))
        if needs_updater_migration:
            from .agent_installer import AgentInstallError, migrate_dlux_updater

            try:
                outcome = migrate_dlux_updater(".", compose_file=args.file or "", apply=True)
                fixes.append(
                    _result(
                        OK,
                        "fix:dlux-updater-runtime",
                        "Normalized DjangoLux runtime commands and local tools mounts. "
                        "Recreate affected services to apply. Backup: "
                        + (outcome.get("backup_root") or "n/a"),
                    )
                )
            except AgentInstallError as exc:
                fixes.append(_result(FAIL, "fix:dlux-updater-runtime", f"Updater migration failed: {exc}"))
        if needs_post_start_migration:
            from .agent_installer import AgentInstallError, enable_post_start_label

            try:
                outcome = enable_post_start_label(".", compose_file=args.file or "", apply=True)
                if outcome.get("files"):
                    fixes.append(
                        _result(
                            OK,
                            "fix:post-start-label",
                            "Installed the org.dlux.post-start label "
                            "(one migrator run per start). Recreate the service to apply. Backup: "
                            + (outcome.get("backup_root") or "n/a"),
                        )
                    )
            except AgentInstallError as exc:
                fixes.append(_result(FAIL, "fix:post-start-label", f"post_start migration failed: {exc}"))
        if needs_restart_labels:
            from .agent_installer import AgentInstallError, normalize_restart_labels

            try:
                outcome = normalize_restart_labels(".", compose_file=args.file or "", apply=True)
                if outcome.get("files"):
                    fixes.append(
                        _result(
                            OK,
                            "fix:restart-labels",
                            "Installed missing org.dlux.restart labels. Backup: "
                            + (outcome.get("backup_root") or "n/a"),
                        )
                    )
            except AgentInstallError as exc:
                fixes.append(_result(FAIL, "fix:restart-labels", f"Restart-label repair failed: {exc}"))
        return fixes

    @staticmethod
    def _print_checkup(results: List[Dict[str, Any]], fixes: List[Dict[str, Any]]):
        print("composer check\n")
        for r in results:
            print(f" {_ICONS[r['level']]} {r['name']}: {r['message']}")
            if r.get("fix") and r["level"] != OK:
                print(f"     ↳ {r['fix']}")
        for f in fixes:
            print(f" {_ICONS[f['level']]} {f['name']}: {f['message']}")
        fails = sum(1 for r in results if r["level"] == FAIL)
        warns = sum(1 for r in results if r["level"] == WARN)
        print("")
        if fails:
            print(f"✖ {fails} problem(s), {warns} warning(s).")
        elif warns:
            print(f"⚠ {warns} warning(s); no blocking problems.")
        else:
            print("✔ All checks passed.")
