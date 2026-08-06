import os
from typing import Any, Dict, List, Optional

from . import wrappers
from .config import ConfigMixin
from .confirmation import confirm
from .proxy_cleanup import inspect_legacy_proxy_routes
from .secrets_manager import SecretsMixin
from .stack_cleanup import OBSOLETE_SERVICES

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
            results.append(_result(OK, "compose", f"Docker Compose v2 present ({out.strip() or 'unknown'})."))
        else:
            results.append(
                _result(
                    WARN,
                    "compose",
                    "Docker Compose v2 plugin not detected; falling back to legacy docker-compose.",
                    fix="Install the Docker Compose v2 plugin.",
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
                    fix="Re-run 'composer check --fix' or 'composer enable-agent --apply'.",
                )
            return _result(
                WARN,
                "topology",
                "Managed by composer-agent (the agent drives Docker directly through docker-socket-proxy).",
                fix=(
                    "Harden with 'composer check --fix' (runs enable-executor) or "
                    "'composer enable-executor --apply': moves Docker authority off the network-facing "
                    "agent into composer-executor and demotes docker-socket-proxy to read-only. "
                    "See docs/executor-hardening.md."
                ),
            )
        if has_legacy:
            return _result(
                WARN,
                "topology",
                "Legacy composer-updater topology detected.",
                fix="Migrate with 'composer check --fix' (runs enable-agent) or 'composer enable-agent --apply'.",
            )
        return _result(
            WARN,
            "topology",
            "No composer-agent or composer-updater service found; this stack is not composer-managed.",
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

    def _dlux_runtime_version(self):
        """(major, minor, patch) of the dlux baked into the project image, read
        from the image via `dlux --version`. Uses `run --no-deps` (a fresh
        container) so it works even when dlux-updater is crash-looping, and never
        starts db/redis. Returns None when it can't be determined."""
        from .agent_installer import parse_dlux_version

        ok, out, _err = self.run_docker_compose(
            ["run", "--rm", "--no-deps", "--entrypoint", "python", "-T",
             "dlux-updater", "-m", "dlux", "--version"],
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
                        fix="Run 'composer check --fix'; the current file is archived under .xpose/ first.",
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
                            "'composer check --fix' replaces it, archiving your copy under .xpose/."
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
                        fix="Run './start.sh update-self'. Do not 'check --fix' this; it would downgrade the wrapper.",
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

    def run_checkup(self, args) -> int:
        self.compose_file = args.file
        self.dev_mode = args.dev
        self.resolve_active_compose_files()

        results: List[Dict[str, Any]] = []
        results.extend(self._check_docker())
        results.append(self._check_compose_files())
        results.append(self._check_compose_parses())
        results.append(self._check_secrets())
        results.extend(self._check_wrappers())
        if self.services:
            results.append(self._check_required_vars())
            results.append(self._check_topology())
            results.append(self._check_removed_services())
            results.append(self._check_proxy_routes())
            results.append(self._check_versions())
            if args.deep:
                results.append(self._run_deep(args.deep_service, args.deep_command))

        fixed = self._maybe_fix(args, results) if args.fix else []

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
        # Already-hardened stacks are not caught by legacy/needs_hardening, so a
        # deployer missing the secrets read capability would otherwise be skipped.
        # A dry-run reports a file change only when the cap must be added.
        needs_secret_cap = False
        if "composer-executor" in set(self.services):
            try:
                from .agent_installer import enable_executor

                needs_secret_cap = bool(
                    enable_executor(".", compose_file=args.file or "", apply=False).get("files")
                )
            except Exception:
                needs_secret_cap = False
        # The dlux-owned updater block may carry the legacy runtime wiring (the
        # retired tools supervisor path / no pre-migration reconcile). Detect it
        # from the compose (a dry-run reports a file change), then gate on the
        # dlux version the IMAGE actually ships — the only authoritative signal on
        # a pulled deployment, which has no requirements.txt.
        needs_updater_migration = False
        updater_migration_blocked = ""
        if "dlux-updater" in set(self.services):
            from .agent_installer import DLUX_PACKAGED_RUNTIME_MIN, migrate_dlux_updater

            try:
                legacy_present = bool(
                    migrate_dlux_updater(".", compose_file=args.file or "", apply=False).get("files")
                )
            except Exception:
                legacy_present = False
            if legacy_present:
                runtime = self._dlux_runtime_version()
                if runtime is not None and runtime >= DLUX_PACKAGED_RUNTIME_MIN:
                    needs_updater_migration = True
                else:
                    got = ".".join(map(str, runtime)) if runtime else "unknown"
                    minimum = ".".join(map(str, DLUX_PACKAGED_RUNTIME_MIN))
                    updater_migration_blocked = (
                        f"dlux-updater uses the legacy runtime, but the project image ships dlux "
                        f"{got} (needs >= {minimum} for the packaged runtime). Update the project "
                        "image, then re-run 'composer check --fix'."
                    )
        # A native Compose post_start hook is run by Compose itself, unflagged,
        # while composer separately execs the same command after health with its
        # flags — two overlapping migrators. Folding it into the label composer
        # reads leaves exactly one runner.
        needs_post_start_migration = False
        try:
            from .agent_installer import enable_post_start_label

            needs_post_start_migration = bool(
                enable_post_start_label(".", compose_file=args.file or "", apply=False).get("files")
            )
        except Exception:
            needs_post_start_migration = False
        if updater_migration_blocked:
            fixes.append(_result(WARN, "dlux-updater-runtime", updater_migration_blocked))
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
                and not needs_secret_cap and not needs_updater_migration
                and not needs_post_start_migration and not stale_wrappers):
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
                    "archived under .xpose/ before it is replaced."
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
                ]
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
        if needs_secret_cap:
            consequences.append(
                "Add cap_add: DAC_READ_SEARCH to composer-executor so it can read the "
                "project's 0600 .secrets/.env to deploy (read-only override; fixes inline "
                "updates failing the secrets guard)."
            )
        if needs_updater_migration:
            consequences.append(
                "Migrate the dlux-updater command to the packaged runtime "
                "(python -m dlux.updater.supervisor) and add the pre-migration dlux_reconcile "
                "guard, so a stale runtime release can't wedge the site in maintenance."
            )
        if needs_post_start_migration:
            consequences.append(
                "Replace the native Compose post_start hook with the org.dlux.post-start "
                "label composer reads, so Compose stops running an unflagged second copy "
                "alongside composer's own flagged run."
            )
        consequences.extend(
            [
                "Validate the candidate with docker compose config before replacement.",
                "Preserve original deployment files under .xpose/.",
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

        if legacy:
            from .agent_installer import AgentInstallError, enable_agent

            try:
                outcome = enable_agent(".", compose_file=args.file or "", apply=True)
                fixes.append(
                    _result(OK, "fix:enable-agent", "Migrated to composer-agent. Backup: " + (outcome.get("backup_root") or "n/a"))
                )
            except AgentInstallError as exc:
                fixes.append(_result(FAIL, "fix:enable-agent", f"Migration failed: {exc}"))
        if needs_hardening:
            from .agent_installer import AgentInstallError, enable_executor

            try:
                outcome = enable_executor(".", compose_file=args.file or "", apply=True)
                fixes.append(
                    _result(
                        OK,
                        "fix:enable-executor",
                        "Hardened composer-agent into the executor topology. Backup: "
                        + (outcome.get("backup_root") or "n/a"),
                    )
                )
            except AgentInstallError as exc:
                fixes.append(_result(FAIL, "fix:enable-executor", f"Hardening failed: {exc}"))
        if needs_secret_cap:
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
                        "Migrated dlux-updater to the packaged runtime + reconcile guard. "
                        "Recreate it to apply. Backup: " + (outcome.get("backup_root") or "n/a"),
                    )
                )
            except AgentInstallError as exc:
                fixes.append(_result(FAIL, "fix:dlux-updater-runtime", f"Updater migration failed: {exc}"))
        if needs_post_start_migration:
            from .agent_installer import AgentInstallError, enable_post_start_label

            try:
                outcome = enable_post_start_label(".", compose_file=args.file or "", apply=True)
                fixes.append(
                    _result(
                        OK,
                        "fix:post-start-label",
                        "Moved the post_start hook onto the org.dlux.post-start label "
                        "(one migrator run per start). Recreate the service to apply. Backup: "
                        + (outcome.get("backup_root") or "n/a"),
                    )
                )
            except AgentInstallError as exc:
                fixes.append(_result(FAIL, "fix:post-start-label", f"post_start migration failed: {exc}"))
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
