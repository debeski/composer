import os
from typing import Any, Dict, List, Optional

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
        if has_agent:
            note = "Managed by composer-agent."
            if not has_proxy:
                return _result(
                    WARN,
                    "topology",
                    note + " docker-socket-proxy is missing, so the agent cannot drive Docker.",
                    fix="Re-run 'composer check --fix' or 'composer enable-agent --apply'.",
                )
            return _result(OK, "topology", note)
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
        if not legacy and not obsolete and not proxy_routes:
            return fixes
        consequences = []
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
