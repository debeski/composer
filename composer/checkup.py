import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import check_lock, wrappers
from .config import ConfigMixin
from .confirmation import confirm
from .proxy_cleanup import inspect_legacy_proxy_routes
from .secrets_manager import SecretsMixin
from . import (
    app_permissions,
    fetch_metadata,
    front_proxy,
    guidance,
    maintenance_migration,
    maintenance_page,
    media_migration,
    redis_persistence,
    relay_env,
    stack_layout,
    stack_lifecycle,
    worker_migration,
)
from .stack_schema import collect_all_stamps, has_env_stamp, invalid_env_stamps, mixed_line_endings
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


def _result(level: str, name: str, message: str, fix: str = "", manual: str = "") -> Dict[str, Any]:
    """One finding. ``manual`` is the step by hand the end-of-run summary names
    when ``check --fix`` has run and cannot repair it (``guidance.summary``)."""
    entry = {"level": level, "name": name, "message": message}
    if fix:
        entry["fix"] = fix
    if manual:
        entry["manual"] = manual
    return entry


def policy_readers(services) -> str:
    """The services among ``services`` that read DLUX_UPDATE_POLICY, as a phrase."""
    from .update_policy_env import READERS

    present = set(services or ())
    found = [name for name in ("web", "worker", "composer-agent", "composer-executor")
             if name in present and name in READERS]
    return stack_lifecycle.join(found) if found else "the services that read it"


#: How a stack whose lock names a Composer from before digest pinning gets out.
PREDATES_PIN_RECOVERY = (
    "Switch with a deployer that can pin: 'docker pull debeski/composer:beta', then "
    "'COMPOSER_SELF_IMAGE=debeski/composer:beta ./start.sh check --beta -y' pins the beta channel's signed "
    "release, rewrites the lock and the block, and recreates the pair. "
    "Stay on beta until Composer 1.11.0 stable is published. See docs/updates.md, 'Releases before signing'."
)


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
        return _result(
            FAIL,
            "compose-config",
            "Compose config does not resolve: " + self._compose_config_error(),
            fix="Fix the reported YAML/interpolation/network error; missing env vars are reported separately.",
        )

    _PROJECT_NAME = re.compile(r"""^name:[ \t]*["']?([^"'#\s]+)""", re.MULTILINE)

    def _check_project_name(self) -> Optional[Dict[str, Any]]:
        """Do the active Compose files agree on the project's ``name:``? Compose takes the last one.

        With ``-d``, a ``compose.dev.yml`` that keeps the scaffold's name starts a
        second project beside the deployed one: its own containers, fresh volumes
        and a second Docker-authority pair. None when they agree or name nothing.
        """
        names: Dict[str, str] = {}
        for name in self.active_compose_files:
            try:
                match = self._PROJECT_NAME.search(Path(name).read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if match:
                names[name] = match.group(1)
        if len(set(names.values())) <= 1:
            return None
        listed = "; ".join(f"{name} names it {value}" for name, value in names.items())
        last = list(names.values())[-1]
        return _result(
            FAIL,
            "project-name",
            f"The active Compose files disagree on the project name ({listed}). Compose takes the last one, so "
            f"this would run project {last}: a second stack with its own containers and fresh volumes beside "
            "the deployed one.",
            fix="Give every Compose file the same top-level name: (the deployed project's), or remove name: from "
            "the override.",
            manual="give every Compose file the same top-level name: by hand",
        )

    def _compose_config_error(self) -> str:
        """Compose's own words for why the configuration does not resolve (its last lines)."""
        ok, out, err = self.run_docker_compose(["config", "--quiet"], timeout=30)
        lines = [line.strip() for line in str(err or out or "").splitlines() if line.strip()]
        if not ok and lines:
            return " ".join(lines[-3:])[:600]
        detail = (self.last_runtime_diagnostic or "").strip().splitlines()
        return detail[0] if detail else "docker compose config failed."

    def _check_secrets(self) -> Dict[str, Any]:
        candidates = self.plaintext_env_candidates()
        if not candidates:
            unsupplied = self.unsupplied_secrets_file_keys()
            if unsupplied:
                shown = ", ".join(unsupplied[:8]) + (f" and {len(unsupplied) - 8} more" if len(unsupplied) > 8 else "")
                return _result(
                    FAIL,
                    "secrets",
                    "No secrets file found (.env / secrets/.env / .secrets/.env), and nothing else supplies "
                    f"{shown}, which the Compose file reads from it. A deploy would run on the Compose file's "
                    "built-in defaults: a placeholder secret key and default database credentials.",
                    fix="Restore the secrets file (usually .secrets/.env, from your backup), or supply those "
                    "variables through the environment, then run the check again.",
                )
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
                f"This Composer process cannot open {path}: {exc}. "
                "Operations cannot validate the deployment's configuration values. "
                "A terminal check may still pass when its launcher has different permissions.",
                fix=self.unreadable_secret_hint(Path(path)),
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
        resident = None
        if not getattr(self, "resident_check", False) and "composer-executor" in self.services:
            resident = self._check_executor_secret_access(Path(path))
            if resident is not None and resident["level"] != OK:
                return resident
        if len(candidates) > 1:
            shadowed = ", ".join(str(other) for other in candidates[1:])
            return _result(
                WARN,
                "secrets",
                f"{path} shadows {shadowed}: './start.sh' and Composer read only {path}, the first of .env, "
                f"secrets/.env and .secrets/.env that exists, so {shadowed} is not used. A deploy then runs on "
                "whatever it lacks from the Compose file's defaults.",
                fix=f"Move {path} out of the project folder (into .xclude/, for example), or merge its keys into "
                f"{candidates[-1]} first.",
                manual=f"move {path} aside so {candidates[-1]} is read",
            )
        loose = self._loose_secrets_mode(Path(path))
        if loose is not None:
            return loose
        return resident or _result(OK, "secrets", f"Secrets source {path} readable ({len(values)} value(s)).")

    @staticmethod
    def _loose_secrets_mode(path: Path) -> Optional[Dict[str, Any]]:
        """WARN when the secrets file's group or others may read or write it (DjangoLux writes it 0600)."""
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except OSError:
            return None
        if not mode & 0o077:
            return None
        who = " and ".join(name for name, bits in (("its group", 0o070), ("other users", 0o007)) if mode & bits)
        return _result(
            WARN,
            "secrets",
            f"{path} is mode {mode:04o}: {who} on this host can read the deployment's secrets (the database "
            "password, SECRET_KEY).",
            fix=f"Run 'chmod 600 {path}'. The user that runs ./start.sh keeps reading it, and composer-executor "
            "reads it through its DAC_OVERRIDE capability.",
        )

    def _check_executor_secret_access(self, path: Path) -> Dict[str, Any]:
        # Open without reading or returning values, under the executor's own identity.
        probe = """import json,os,sys
from pathlib import Path
uid=os.geteuid(); host_uid=None
for line in Path('/proc/self/uid_map').read_text().splitlines():
 start,host,length=map(int,line.split())
 if start<=uid<start+length: host_uid=host+uid-start; break
try:
 with open(sys.argv[1],'rb'): pass
 readable=True
except OSError: readable=False
print(json.dumps({'readable':readable,'host_uid':host_uid}))
"""
        ok, out, _err = self.run_docker_compose(
            ["exec", "-T", "composer-executor", "python", "-c", probe, str(path.absolute())], timeout=15,
        )
        try:
            data = json.loads(out) if ok else {}
        except (ValueError, TypeError):
            data = {}
        if data.get("readable") is True:
            return _result(OK, "secrets", f"Secrets source {path} is readable by the launcher and deployment executor.")
        if data.get("readable") is not False:
            return _result(WARN, "secrets", f"The launcher can read {path}, but deployment-executor access could not be verified.",
                           fix="Ensure composer-executor is running, then run ./start.sh check again.")
        uid = data.get("host_uid")
        if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
            return _result(FAIL, "secrets", f"The launcher can read {path}, but the deployment executor cannot.",
                           fix="Check the deployment executor's host UID and file/directory access on the VM; keep secrets restricted.")
        return _result(FAIL, "secrets", f"The launcher can read {path}, but the deployment executor (host UID {uid}) cannot. "
                       "Operations cannot validate the deployment configuration under that identity.",
                       fix=self.unreadable_secret_hint(path, mapped_uid=uid))

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
                note = (
                    "Hardened topology: composer-executor holds Docker authority and answers "
                    "composer-agent's reads; composer-agent has no Docker access."
                )
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
                    "agent into composer-executor and removes docker-socket-proxy. "
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
                "Run 'composer check --fix' to install composer-executor and composer-agent."
            ),
        )

    # Resident roles and the subcommand each must name. Composer 1.3.13+ exits on
    # the flat `agent --flag ...` form, so the service restart-loops.
    RESIDENT_ROLE_COMMANDS = (("composer-agent", "agent"), ("composer-executor", "executor"))

    def _check_resident_commands(self) -> Optional[Dict[str, Any]]:
        """Flag resident services still started with the pre-nested flat command.

        None when the resolved model cannot be read; `compose-config` already
        reports that, and an unknown is not a finding.
        """
        ok, out, _err = self.run_docker_compose(["config", "--format", "json"], timeout=20)
        if not ok:
            return None
        try:
            services = json.loads(out).get("services") or {}
        except (ValueError, AttributeError):
            return None
        stale = []
        for service, role in self.RESIDENT_ROLE_COMMANDS:
            command = (services.get(service) or {}).get("command")
            if isinstance(command, str):
                command = command.split()
            if not isinstance(command, list) or not command or command[0] != role:
                continue
            if len(command) == 1 or str(command[1]).startswith("-"):
                stale.append(service)
        if not stale:
            return _result(OK, "resident-commands", "Resident Composer services use the nested '<role> run' command.")
        return _result(
            FAIL,
            "resident-commands",
            ", ".join(stale) + " still use the flat '<role> --flag' command, which this Composer "
            "rejects, so the service restart-loops and the stack has no update path.",
            fix="Run 'composer check --fix' to insert the 'run' subcommand.",
        )

    # The switches that make docker-socket-proxy a Docker write path. Any one of
    # them lets a client that reaches the proxy start a privileged container.
    PROXY_WRITE_SWITCHES = ("POST", "EXEC")
    PROXY_BY_HAND = (
        "remove the docker-socket-proxy service (and composer-agent's DOCKER_HOST pointing at it) from the "
        "Compose file by hand: it is outside the generated Composer block, which is all check --fix rewrites"
    )

    def _check_docker_proxy(self) -> Optional[Dict[str, Any]]:
        """FAIL when docker-socket-proxy accepts writes; WARN when it is only a leftover
        beside composer-executor. None when there is none, or it is unreadable."""
        if "docker-socket-proxy" not in set(self.services):
            return None
        ok, out, _err = self.run_docker_compose(["config", "--format", "json"], timeout=20)
        if not ok:
            return None
        try:
            proxy = (json.loads(out).get("services") or {}).get("docker-socket-proxy") or {}
        except (ValueError, AttributeError):
            return None
        environment = proxy.get("environment") or {}
        if isinstance(environment, list):
            environment = dict(item.split("=", 1) for item in environment if "=" in item)
        enabled = [
            name for name in self.PROXY_WRITE_SWITCHES
            if str(environment.get(name, "0")).strip().strip("'\"").lower()
            not in {"0", "false", "no", "off", ""}
        ]
        if enabled:
            return self._writable_proxy(enabled)
        if "composer-executor" in set(self.services):
            return _result(
                WARN,
                "docker-proxy",
                "docker-socket-proxy lets composer-agent read every container's environment "
                "(database password, SECRET_KEY) and files. composer-agent no longer uses it: "
                "composer-executor answers its Docker reads.",
                fix=(
                    "Run 'composer check --fix': it removes the proxy's container and its service "
                    "from the generated Composer block. A proxy outside that block must be removed "
                    "by hand."
                ),
                manual=self.PROXY_BY_HAND,
            )
        return _result(OK, "docker-proxy", "docker-socket-proxy is read-only (POST and EXEC off).")

    def _writable_proxy(self, enabled) -> Dict[str, Any]:
        return _result(
            FAIL,
            "docker-proxy",
            "docker-socket-proxy accepts writes (" + ", ".join(f"{name}=1" for name in enabled)
            + "): anything that reaches it can start a privileged container, which is root "
            "on this host.",
            fix=(
                "Run 'composer check --fix': it hardens the stack so composer-executor holds "
                "Docker authority and removes the proxy. A proxy outside the generated "
                "Composer block must be edited by hand."
            ),
            manual=self.PROXY_BY_HAND,
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
        """(major, minor, patch) of the dlux baked into the service's image, read
        with `dlux --version` in a throwaway container that mounts nothing and has
        no network. It works while the service is crash-looping and starts
        nothing else. `compose run` gave the probe the service's mounts, so a
        plain `check` on a stack that never started created its named volumes
        and bind folders. An image that would have to be built is not built.
        Returns None when it can't be determined."""
        from .agent_installer import parse_dlux_version
        from .app_permissions import service_image

        service = service or (
            "dlux-updater" if "dlux-updater" in set(self.services) else "web"
        )
        config = self.compose_config_json() or {}
        image = service_image(config, service)
        if not image:
            return None
        spec = (config.get("services") or {}).get(service) or {}
        pull = "missing" if spec.get("image") else "never"
        ok, out, _err = self.run_command(
            ["docker", "run", "--rm", "--pull", pull, "--network", "none",
             "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
             "--entrypoint", "python", image, "-m", "dlux", "--version"],
            timeout=60,
        )
        return parse_dlux_version(out) if ok else None

    def _resident_agent_version(self) -> Optional[str]:
        if "composer-agent" not in set(self.services):
            return None
        ok, out, _ = self.run_docker_compose(
            ["exec", "-T", "composer-executor", "cat", "/app/VERSION"], timeout=10
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
                f"Deploying composer {deployer}; deployment executor version unavailable (not running or not enrolled).",
            )
        if resident == deployer:
            return _result(OK, "versions", f"Deploying composer and deployment executor both {deployer}.")
        return _result(
            WARN,
            "versions",
            f"Version drift: deploying composer {deployer}, deployment executor {resident}.",
            fix="Update the deployment executor's image so both match, if that matters for the change you're shipping.",
        )

    def _check_stack_schema(self) -> Dict[str, Any]:
        """Do the generated stack files agree on the stack contract schema?

        Every stamp in every file is read. compose.yml's ``DLUX_STACK_SCHEMA`` is
        the value DjangoLux reads at runtime, so compose.yml stamps that disagree
        with each other (a half restamp) FAIL; other disagreements WARN. Whether
        the schema is the one the running DjangoLux expects is the app's
        `stack.schema` doctor check, relayed by `--deep`.
        """
        compose = next(
            (c for c in (self.compose_file, "compose.yml", "docker-compose.yml") if c and Path(c).is_file()),
            "compose.yml",
        )
        secrets = [str(c) for c in self.plaintext_env_candidates()[:1]]
        self._invalid_compose_stamp = []
        try:
            compose_text = Path(compose).read_text(encoding="utf-8", errors="replace")
        except OSError:
            compose_text = ""
        invalid = invalid_env_stamps(compose_text, stack_layout.NEWEST_KNOWN)
        if invalid:
            self._invalid_compose_stamp = invalid
            schema, why = stack_layout.layout_schema(Path("."), self._maintenance_compose_files())
            values = ", ".join(f'"{value}"' for value in dict.fromkeys(invalid))
            target = (
                f'DLUX_STACK_SCHEMA: "{schema}", the schema its files follow' if schema is not None
                else f"the schema the stack follows ({why})"
            )
            return _result(
                FAIL,
                "stack-schema",
                f"{compose} sets DLUX_STACK_SCHEMA to {values}, which is not a stack schema (1 to "
                f"{stack_layout.NEWEST_KNOWN}). DjangoLux reads that value at runtime, and Composer's own "
                "schema gates cannot judge it.",
                fix=f"Set it by hand to {target}; 'check --fix' does not guess it.",
                manual=f"set DLUX_STACK_SCHEMA in {compose} by hand to {target}",
            )
        stamps = collect_all_stamps(".", compose, secrets)
        stamped = {path: found for path, found in stamps.items() if found}
        if not stamped:
            return _result(
                OK,
                "stack-schema",
                "Stack files carry no schema stamp; they predate DjangoLux stack schema stamping.",
            )
        split = {path: found for path, found in stamped.items() if len(set(found)) > 1}
        if split:
            schema, why = stack_layout.layout_schema(Path("."), self._maintenance_compose_files())
            listed = "; ".join(
                f"{path} says " + " and ".join(str(value) for value in dict.fromkeys(found))
                for path, found in sorted(split.items())
            )
            if schema is None:
                fix = f"Restamp them by hand to the schema the stack follows ({why})."
            else:
                fix = f"Run 'composer check --fix' to restamp them {schema}, the schema the stack's files follow."
            if compose in split:
                return _result(
                    FAIL,
                    "stack-schema",
                    f"A file's own schema stamps disagree ({listed}), left by a restamp that changed only "
                    f"some of them. DjangoLux reads DLUX_STACK_SCHEMA from {compose}, so it runs the stack "
                    "as the schema that value names.",
                    fix=fix,
                )
            return _result(WARN, "stack-schema", f"A file's own schema stamps disagree ({listed}).", fix=fix)
        lowest = {path: min(found) for path, found in stamped.items()}
        schemas = sorted(set(lowest.values()))
        if len(schemas) > 1:
            by_schema = "; ".join(
                f"{schema}: " + ", ".join(sorted(p for p, s in lowest.items() if s == schema))
                for schema in schemas
            )
            return _result(
                WARN,
                "stack-schema",
                f"Stack files declare different stack schemas ({by_schema}).",
                fix="Bring the older files up to date using DjangoLux's docs/stack-schema.md, then restamp them.",
            )
        schema = schemas[0]
        if not stamps.get(compose):
            return _result(
                WARN,
                "stack-schema",
                f"Stack files declare schema {schema}, but {compose} carries no DLUX_STACK_SCHEMA.",
                fix=f'Add DLUX_STACK_SCHEMA: "{schema}" to the x-environment block of {compose}.',
            )
        try:
            env_stamped = has_env_stamp(Path(compose).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            env_stamped = True
        if not env_stamped:
            return _result(
                WARN,
                "stack-schema",
                f"{compose} declares stack schema {schema} in a comment only; DjangoLux reads the "
                "DLUX_STACK_SCHEMA value, which it does not have.",
                fix=f'Add DLUX_STACK_SCHEMA: "{schema}" to the x-environment block of {compose}.',
            )
        unstamped = len(stamps) - len(stamped)
        note = f" {unstamped} other file(s) unstamped." if unstamped else ""
        return _result(OK, "stack-schema", f"{compose} declares stack schema {schema}.{note}")

    MAINTENANCE_AGENT_FLOOR = "1.7.0b1"

    def _maintenance_compose_files(self) -> List[str]:
        return list(self.active_compose_files) or [
            c for c in ("compose.yml", "docker-compose.yml") if Path(c).is_file()
        ][:1]

    def _published_page_has(self, name: str) -> bool:
        """Whether the page composer-agent published on the runtime volume has ``name``.

        Where this process mounts the runtime volume — composer-agent, which runs
        the Operations panel's check and apply — it reads the file: the read-only
        socket proxy refuses ``exec`` there, which made every panel check report a
        published page as missing. On the host, composer-agent is asked.
        """
        page_dir = Path(maintenance_migration.RUNTIME_PAGE_DIR)
        if page_dir.parents[1].is_dir():
            return (page_dir / name).is_file()
        ok, _out, _err = self.run_docker_compose(
            ["exec", "-T", "composer-executor", "test", "-f", f"{page_dir}/{name}"],
            timeout=10,
        )
        return ok

    def _agent_running(self) -> bool:
        """Is composer-agent running? Always, where this process runs inside the resident pair."""
        if getattr(self, "resident_check", False) or Path(maintenance_migration.RUNTIME_PAGE_DIR).parents[1].is_dir():
            return True
        return any(
            row["service"] == "composer-agent" and row["state"] == "running" for row in self._container_states()
        )

    def _maintenance_page_published(self) -> bool:
        return self._published_page_has("index.html")

    def _running_dlux_version(self):
        """(major, minor, patch) of the DjangoLux serving requests: the active
        inline release on the runtime volume, else the one baked into the image."""
        from .agent_installer import parse_dlux_version

        if "web" in set(self.services):
            ok, out, _ = self.run_docker_compose(
                ["exec", "-T", "web", "cat", "/opt/dlux-runtime/state/active.json"], timeout=10
            )
            if ok:
                try:
                    active = json.loads(out)
                except ValueError:
                    active = {}
                if isinstance(active, dict) and active.get("source") == "volume":
                    return parse_dlux_version(active.get("version"))
            return self._dlux_runtime_version("web")
        return self._dlux_runtime_version()

    def _stored_files_way_out(self) -> str:
        """How this stack gets the DjangoLux the stored-files fix needs (1.15.3+).

        Named from the release data (``dlux_upgrade_path``): ``dlux update`` alone
        may refuse the newest release on an old stack schema, which is how the
        advice used to go in a circle (break-it finding #1). Worked out once per run.
        """
        cached = getattr(self, "_stored_files_way_out_cache", None)
        if cached is not None:
            return cached
        from . import dlux_upgrade_path
        from .stack_schema import deployment_schema

        running = self._running_dlux_version()
        if running is not None and tuple(running) >= media_migration.MEDIA_ACCESS_FLOOR:
            advice = "Run 'composer check --fix' to make stored files private (stack schema 4)."
        else:
            stone, error = (None, "") if running is None else dlux_upgrade_path.find_stone(tuple(running))
            if running is None:
                error = "the running DjangoLux version is unknown"
            advice = dlux_upgrade_path.media_way_out(
                running=running, stamp=deployment_schema("."), stone=stone, index_error=error,
            )
        self._stored_files_way_out_cache = advice
        return advice

    def _check_media_privacy(self) -> Dict[str, Any]:
        """Stored files must not be served past the app (DjangoLux 1.15.3 security
        fix, stack schema 4); ``check --fix`` switches a generated proxy over."""
        state = media_migration.inspect(Path("."))
        exposed = sorted(name for name, kind in state["files"].items() if kind == media_migration.LEGACY)
        if exposed:
            return _result(
                FAIL,
                "stored-files",
                "SECURITY: the proxy serves every /media/ file without authorization ("
                + ", ".join(exposed)
                + "), so anyone who can reach this site can download stored files by URL without signing in.",
                fix=self._stored_files_way_out(),
            )
        if state["custom"]:
            return _result(
                WARN,
                "stored-files",
                "The proxy has its own /media/ rules (" + ", ".join(state["custom"]) + "), which Composer leaves "
                "alone; make sure private stored files are not served to signed-out visitors.",
                fix="Compare them with the /media/ blocks of DjangoLux 1.15.3's scaffold .proxy files "
                "(stack schema 4), or replace them with those blocks.",
            )
        if not state["files"]:
            return _result(OK, "stored-files", "No /media/ route in the proxy config.")
        return _result(OK, "stored-files", "Stored files go through DjangoLux's access check (since stack schema 4).")

    def _check_fetch_metadata(self) -> Dict[str, Any]:
        """Does the proxy refuse what other sites' pages make a browser load in the background?"""
        name = "fetch-metadata"
        try:
            state = fetch_metadata.inspect(Path("."))
        except (OSError, UnicodeDecodeError) as exc:
            return _result(WARN, name, f"Could not read the proxy configuration: {exc}")
        if not state["files"]:
            return _result(OK, name, f"No {fetch_metadata.CADDYFILE} or {fetch_metadata.NGINX_TEMPLATE}; nothing to check.")
        missing = state["missing"]
        if missing:
            caddy = str(fetch_metadata.CADDYFILE) in missing
            nginx = str(fetch_metadata.NGINX_TEMPLATE) in missing
            steps = []
            if caddy:
                steps.append(
                    f"in {fetch_metadata.CADDYFILE}, the @cross_site_request matcher and "
                    "'respond @cross_site_request 403', after the public /media/ handler and before the private "
                    "/media/* handler"
                )
            if nginx:
                steps.append(
                    f"in {fetch_metadata.NGINX_TEMPLATE}, the $cross_site_request map and "
                    "'if ($cross_site_request) { return 403; }' in the /media/, / and /health locations"
                )
            restart = " or ".join(service for service, wanted in (("caddy", caddy), ("nginx", nginx)) if wanted)
            return _result(
                WARN,
                name,
                " and ".join(missing) + (" has" if len(missing) == 1 else " have") + " no Fetch Metadata "
                "resource isolation policy (cross_site_request), so another site's page can make a visitor's "
                "browser load this site's pages, private files and endpoints in the background (images, scripts, "
                "fetch(), form posts). CSRF tokens and SameSite cookies still stop forged changes; this is the "
                "layer that refuses such requests before they reach the app.",
                fix=f"Add the block from {fetch_metadata.DOCS} (also in a fresh 'python -m dlux startproject'): "
                + "; ".join(steps)
                + f". Then run './start.sh restart {restart}', since './start.sh' alone does not reload a changed "
                "proxy file.",
            )
        return _result(
            OK,
            name,
            "The proxy refuses requests other sites make in the background (Fetch Metadata, cross_site_request).",
        )

    def _check_front_proxy(self) -> Dict[str, Any]:
        """Which front proxies may Caddy take a client address from (``DLUX_FRONT_PROXY``)?"""
        name = "front-proxy"
        try:
            state = front_proxy.inspect(Path("."), self._maintenance_compose_files(), self.build_compose_env())
        except OSError as exc:
            return _result(WARN, name, f"Could not read the proxy configuration: {exc}")
        caddyfile = str(front_proxy.CADDYFILE)
        compose = state["compose_file"] or "compose.yml"
        where = state["source"]
        secrets = state["secrets"]
        if state["compose"] == front_proxy.NO_SERVICE:
            return _result(OK, name, "No caddy service in this stack; Composer manages Caddy's trusted front proxies only.")
        if state["caddyfile"] == front_proxy.MISSING:
            return _result(OK, name, f"No {caddyfile}; Composer manages the generated Caddyfile's trusted front proxies only.")
        if state["caddyfile"] == front_proxy.CUSTOM:
            return _result(OK, name, f"Caddy's trusted_proxies in {caddyfile} is custom, not managed by Composer.")
        if state["caddyfile"] == front_proxy.NONE:
            return _result(
                OK, name, f"{caddyfile} sets no trusted_proxies, so Caddy trusts no front proxy (custom, not managed by Composer)."
            )
        pending = []
        if state["caddyfile"] == front_proxy.LEGACY:
            pending.append(f"{caddyfile} does not read " + ("it" if state["set"] else "DLUX_FRONT_PROXY"))
        if state["compose"] == front_proxy.ABSENT:
            pending.append(f"{compose} does not pass it to caddy")
        if state["set"] and state["quoted"]:
            return _result(
                FAIL,
                name,
                f"DLUX_FRONT_PROXY in {where} is in quotes. './start.sh' passes the quotes to Caddy as part of "
                "the value, and Caddy does not start with a quoted list"
                + (" once it reads the variable." if pending else "."),
                fix="Remove the quotes and separate the entries with spaces "
                "(DLUX_FRONT_PROXY=10.0.0.5 192.168.10.0/24).",
            )
        if state["invalid"]:
            commas = any("," in entry for entry in state["invalid"])
            return _result(
                FAIL,
                name,
                f"DLUX_FRONT_PROXY in {where} has entries that are not an IP address, a CIDR range or "
                f"private_ranges: {', '.join(state['invalid'])}. "
                + ("Caddy would not start with them once it reads the variable." if pending
                   else "Caddy does not start with them."),
                fix="List the front proxy's addresses or ranges separated by spaces"
                + (", not commas" if commas else "")
                + " (DLUX_FRONT_PROXY=10.0.0.5 192.168.10.0/24), set it to private_ranges, or leave it empty "
                "to trust no proxy.",
            )
        if pending:
            if state["set"]:
                message = (
                    f"DLUX_FRONT_PROXY is set in {where}, but " + " and ".join(pending)
                    + ", so Caddy still trusts forwarded client addresses from every private network."
                )
            else:
                message = (
                    "Caddy trusts forwarded client addresses (X-Forwarded-For) from every private network, and "
                    "the trusted front proxies cannot be chosen yet: " + " and ".join(pending) + "."
                )
            steps = []
            if state["files"]:
                steps.append(
                    f"Run 'composer check --fix' so Caddy reads DLUX_FRONT_PROXY from {secrets} "
                    "(left unset, it keeps trusting every private network)."
                )
            if state["unsupported"]:
                steps.append(
                    f'Add DLUX_FRONT_PROXY: "{front_proxy.COMPOSE_VALUE}" to the caddy service\'s environment '
                    f"in {compose} by hand: {state['unsupported']}"
                )
            return _result(WARN, name, message, fix=" ".join(steps))
        if state["compose"] == front_proxy.CUSTOM:
            return _result(
                OK, name, f"{compose} gives caddy its own DLUX_FRONT_PROXY value (custom, not managed by Composer)."
            )
        if not state["set"]:
            return _result(
                WARN,
                name,
                "Caddy trusts forwarded client addresses (X-Forwarded-For) from every private network, so a "
                "client on a private network that reaches Caddy directly can choose the address DjangoLux "
                "records (login lockout, audit log).",
                fix=f"Set DLUX_FRONT_PROXY in {secrets} to the front proxy's address(es), space-separated "
                "(DLUX_FRONT_PROXY=10.0.0.5), set it empty (DLUX_FRONT_PROXY=) if users reach this server "
                "directly, or set it to private_ranges to keep this on purpose. Without a setting, firewall "
                f"the server so only the front proxy reaches Caddy's published port {state['port']}. Apply a "
                f"changed {secrets} with './start.sh', which recreates caddy.",
            )
        ranges = list(dict.fromkeys(state["ranges"]))
        if not ranges:
            return _result(
                OK, name, f"Caddy trusts no proxy (DLUX_FRONT_PROXY is empty in {where}): it records the address "
                "that connects to it."
            )
        if ranges == [front_proxy.PRIVATE_RANGES]:
            return _result(
                OK, name, "Caddy trusts forwarded client addresses from every private network "
                f"(DLUX_FRONT_PROXY=private_ranges in {where}), a deliberate choice."
            )
        named = ["every private network" if entry == front_proxy.PRIVATE_RANGES else entry for entry in ranges]
        return _result(
            OK, name, f"Caddy trusts forwarded client addresses only from {', '.join(named)} "
            f"(DLUX_FRONT_PROXY in {where})."
        )

    def _check_image_floor(self) -> Optional[Dict[str, Any]]:
        """Is the stack's DjangoLux new enough for the layout its files declare?

        Files newer than the image (an image rolled back under files ``check
        --fix`` moved on) look right to every layout check: a ``worker`` running
        ``dlux_worker`` on a DjangoLux without it crash-loops, and a proxy that
        asks DjangoLux about private files refuses every one when it cannot
        answer. None when the layout or the version cannot be read.
        """
        schema, _why = stack_layout.layout_schema(Path("."), self._maintenance_compose_files())
        if schema is None or schema < 4:
            return None
        needs = [(media_migration.MEDIA_ACCESS_FLOOR, "the private /media/ route, which asks DjangoLux before "
                  "serving a stored file (stack schema 4)")]
        if schema >= 5:
            needs.append((worker_migration.WORKER_FLOOR, "the worker service, which runs 'manage.py dlux_worker' "
                          "(stack schema 5)"))
        running = self._running_dlux_version()
        if running is None:
            return None
        version = ".".join(map(str, running))
        unmet = [(floor, what) for floor, what in needs if tuple(running) < floor]
        if not unmet:
            return _result(OK, "image-floor", f"DjangoLux {version} has what the stack files need (stack schema {schema}).")
        floor = ".".join(map(str, max(item[0] for item in unmet)))
        return _result(
            FAIL,
            "image-floor",
            f"The stack files need DjangoLux {floor} or later for " + " and ".join(what for _, what in unmet)
            + f", but the stack runs DjangoLux {version}. They are newer than the image (an image rolled back "
            "under files 'check --fix' moved on, for example).",
            fix=f"Deploy a project image on DjangoLux {floor} or later, or put back the stack files that match this "
            "image (the originals Composer replaced are under .xclude/composer-check/).",
            manual=f"deploy an image on DjangoLux {floor} or later, or restore the stack files that match this image",
        )

    _PROXY_SERVICES = ("caddy", "nginx")

    def _check_proxy_config(self) -> Optional[Dict[str, Any]]:
        """Does every proxy file the proxy mounts exist, and does the proxy accept it?

        Docker turns a missing bind source into an empty directory, so the proxy
        does not start on the next deploy. The proxy files ``check --fix`` reads
        are also validated in the proxy's own image, the way a repair validates
        what it writes: no network, nothing pulled (an image that is not here is
        not validated) and nothing created but the throwaway container.
        """
        from .proxy_cleanup import PROXY_ROUTE_FILES
        from .stack_cleanup import DOCKER_RUN_ERRORS, StackCleanupError, proxy_validation_command

        name = "proxy-config"
        with self._deployment_environment():
            config = self.compose_config_json() or {}
            environment = dict(os.environ)
        services = config.get("services") if isinstance(config.get("services"), dict) else {}
        proxies = [proxy for proxy in self._PROXY_SERVICES if isinstance(services.get(proxy), dict)]
        if not proxies:
            return None
        root = Path(".").resolve()
        kinds = {(root / relative).resolve(): kind for relative, kind in PROXY_ROUTE_FILES.items()}
        missing, checks = [], []
        for proxy in proxies:
            for volume in services[proxy].get("volumes") or []:
                if not isinstance(volume, dict) or volume.get("type") != "bind":
                    continue
                source = Path(str(volume.get("source") or ""))
                target = str(volume.get("target") or "")
                if not str(source) or not target:
                    continue
                try:
                    label = "./" + str(source.resolve().relative_to(root))
                except ValueError:
                    label = str(source)
                own = source.resolve() in kinds or any(
                    label.startswith(f"./{folder}/") for folder in (".proxy", ".nginx")
                )
                if own and not source.exists():
                    missing.append(f"{proxy} mounts {label} at {target}")
                elif source.is_file() and source.resolve() in kinds:
                    checks.append((proxy, source, target, kinds[source.resolve()], label))
        if missing:
            return _result(
                FAIL, name,
                "A proxy file the stack mounts does not exist: " + "; ".join(missing) + ". Docker would create an "
                "empty directory in its place, and the proxy would not start on the next deploy.",
                fix="Put the file back: Composer keeps every original it replaced under .xclude/composer-check/, and "
                "a fresh 'python -m dlux startproject' has the generated one. Then run './start.sh'.",
            )
        if getattr(self, "resident_check", False) or not checks:
            return _result(OK, name, f"Every proxy file {', '.join(proxies)} mounts is present.")
        validated, skipped = [], []
        for proxy, source, target, kind, label in checks:
            try:
                argv, keys = proxy_validation_command(config, proxy, source, target, kind, pull="never")
            except StackCleanupError:
                skipped.append(label)
                continue
            values = (services[proxy].get("environment") or {})
            run_env = dict(environment)
            run_env.update({key: str(values[key]) for key in keys})
            ok, out, err = self.run_command(argv, timeout=60, env=run_env)
            if ok:
                validated.append(label)
                continue
            text = str(err or out or "").strip()
            if any(marker in text for marker in ("No such image", "Unable to find image")) or not text:
                skipped.append(label)
                continue
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            return _result(
                FAIL, name,
                f"{proxy} rejects {label}: " + " ".join(lines[-2:])[:500] + " The next deploy would bring "
                f"{proxy} down, and the site with it.",
                fix=f"Fix {label} (the originals Composer replaced are under .xclude/composer-check/), then run "
                f"'./start.sh restart {proxy}'.",
            )
        message = f"Every proxy file {', '.join(proxies)} mounts is present"
        if validated:
            message += f"; {', '.join(validated)} passed the proxy's own validation"
        if skipped:
            message += f"; {', '.join(skipped)} not validated ({'its' if len(skipped) == 1 else 'their'} image is not on this host)"
        return _result(OK, name, message + ".")

    _DB_MERGE = re.compile(r"^  db:[ \t]*(?:#.*)?$((?:\n(?:    .*|[ \t]*(?:#.*)?))*)", re.MULTILINE)

    def _check_scaffold_drift(self) -> Optional[Dict[str, Any]]:
        """What a stack walked forward by ``check --fix`` still lacks from the current scaffold.

        ``check --fix`` repairs the Compose and proxy files; the project's
        Dockerfile and entrypoint are the operator's. These are the gaps that
        matter once the app image runs as a non-root uid, plus the database
        receiving the app's shared environment. None when this is not a
        DjangoLux project tree (no Dockerfile or entrypoint.sh here).
        """
        gaps: List[str] = []
        dockerfile, entrypoint = Path("Dockerfile"), Path("entrypoint.sh")
        try:
            docker_text = dockerfile.read_text(encoding="utf-8", errors="replace") if dockerfile.is_file() else ""
            entry_text = entrypoint.read_text(encoding="utf-8", errors="replace") if entrypoint.is_file() else ""
        except OSError:
            return None
        if not docker_text and not entry_text:
            return None
        if docker_text:
            config = self.compose_config_json() or {}
            mounts = sorted({
                str(volume.get("target"))
                for service in app_permissions.app_services(config)
                for volume in ((config.get("services") or {}).get(service) or {}).get("volumes") or []
                if isinstance(volume, dict) and volume.get("target") and not volume.get("read_only")
                and str(volume.get("target")).startswith(("/app/", "/opt/dlux-runtime"))
            })
            prepared = " ".join(
                line for line in docker_text.replace("\\\n", " ").splitlines() if re.search(r"chmod\b.*g\+\S*w", line)
            )
            unprepared = [target for target in mounts if not re.search(rf"(?<![\w/.-]){re.escape(target)}(?![\w/.-])", prepared)]
            if mounts and not prepared:
                gaps.append(
                    "the Dockerfile does not prepare the app's mount points (mkdir, group 0, chmod g+rwX), so a "
                    "non-root image's fresh volumes start root 0755"
                )
            elif unprepared:
                gaps.append(f"the Dockerfile does not prepare {', '.join(unprepared)} (chmod g+rwX)")
        if entry_text:
            if not re.search(r"^\s*umask\s+0?002\b", entry_text, re.MULTILINE):
                gaps.append("entrypoint.sh does not set 'umask 0002', so files the app writes are not group-writable")
            if "dlux.mount_access gate" not in entry_text:
                gaps.append(
                    "entrypoint.sh does not run 'python -m dlux.mount_access gate', so a non-root app that cannot use "
                    "its data fails later, on uploads, instead of at start"
                )
        compose = next((c for c in self._maintenance_compose_files() if Path(c).is_file()), "")
        if compose:
            try:
                block = self._DB_MERGE.search(Path(compose).read_text(encoding="utf-8", errors="replace"))
            except OSError:
                block = None
            if block and re.search(r"^\s*<<:\s*\*", block.group(1), re.MULTILINE):
                gaps.append(f"db in {compose} merges the shared x-environment block, so the database container "
                            "receives the app's secrets (SECRET_KEY, mail and registry logins)")
        if not gaps:
            return _result(OK, "scaffold-drift", "The Dockerfile, entrypoint and db service match the current scaffold's "
                           "non-root preparation.")
        return _result(
            WARN,
            "scaffold-drift",
            "This stack lacks parts of the current DjangoLux scaffold that 'check --fix' does not write: "
            + "; ".join(gaps) + ".",
            fix="Compare with a fresh 'python -m dlux startproject' (DjangoLux's docs/stack-schema.md) and copy the "
            "missing lines by hand, then rebuild the project image. 'check --fix' leaves these files to you.",
            manual="copy the missing lines from a fresh 'python -m dlux startproject' by hand, then rebuild the image",
        )

    def _check_line_endings(self) -> Optional[Dict[str, Any]]:
        """WARN on a stack file that mixes line endings; ``check --fix`` then leaves the files alone."""
        files = list(dict.fromkeys([
            *self._maintenance_compose_files(), "compose.dev.yml", ".proxy/Caddyfile", ".proxy/default.conf.template",
        ]))
        mixed = [name for name in files if Path(name).is_file() and mixed_line_endings(Path(name))]
        self._mixed_endings = mixed
        if not mixed:
            return None
        one = len(mixed) == 1
        return _result(
            WARN,
            "line-endings",
            f"{', '.join(mixed)} mix{'es' if one else ''} LF, CRLF or CR line endings. 'check --fix' changes no stack "
            "file while one does: a rewrite writes one ending for the whole file, so every line of the other kind "
            "would show as changed.",
            fix="Convert " + ("it" if one else "them") + " to one style by hand (for example 'dos2unix "
            + " ".join(mixed) + "', or your editor's line-ending setting), then run './start.sh check --fix'.",
            manual=f"convert {' '.join(mixed)} to one line-ending style by hand, then ./start.sh check --fix",
        )

    def _check_app_permissions(self, deep: bool = False) -> Optional[Dict[str, Any]]:
        """Can the uid the app image runs as use the data its services mount?

        DjangoLux images run as uid 10001 with group 0 and reach data a root
        image wrote through group bits. While the image still runs as root this
        reports whether the data is ready for that move (WARN, fixable at any
        time: root ignores the bits); once it runs as a non-root uid, data it
        cannot use is a FAIL, because DjangoLux refuses to start on it.
        """
        name = "app-permissions"
        self._app_permissions_state = None
        config = self.compose_config_json()
        image = app_permissions.app_image(config or {})
        if not config or not image:
            return None
        kind, uid, gid = app_permissions.image_identity(self.run_command, image)
        if kind == "named":
            return _result(
                WARN, name,
                f"{image} runs as a named user, so Composer cannot tell which uid needs access to its data.",
                fix="Use a numeric USER uid:gid in the Dockerfile (DjangoLux images use 10001:0).",
            )
        unknown = kind == "unknown"
        as_root = kind == "root" or unknown
        if as_root:
            uid, gid = app_permissions.APP_UID, app_permissions.APP_GID
        try:
            summary = app_permissions.run(
                self, "scan", uid=uid, gid=gid, depth=None if deep else app_permissions.QUICK_DEPTH
            )
        except app_permissions.PermissionsError as exc:
            return _result(WARN, name, f"Could not check the app's data: {exc}")
        wrong = app_permissions.totals(summary)["wrong"]
        missing = [] if as_root else summary["missing"]
        self._app_permissions_state = {"uid": uid, "gid": gid, "wrong": wrong, "missing": missing}
        scope = "" if deep else " (top two levels; --deep checks every entry)"
        who = f"uid {uid} (gid {gid})"
        example = app_permissions.examples(summary)
        detail = f", e.g. {example}" if example else ""
        if missing:
            detail += f"; missing: {', '.join(missing)}"
        count = wrong + len(missing)
        entries = f"{count} entr{'y' if count == 1 else 'ies'}"
        fix = (
            "./start.sh check --fix adds the group bits the app needs (only modes change; nothing is moved "
            "or rewritten; the previous root image keeps working)."
        )
        if unknown:
            subject = f"Composer cannot read which user {image} runs as (the image is not on this host)."
            if not count:
                return _result(OK, name, f"{subject} The data is ready for {who}{scope}.")
            return _result(
                WARN, name,
                f"{subject} Before an image running as {who} can use its data, {entries} need group "
                f"access{scope}{detail}. './start.sh' grants it before the first start when the image runs as "
                "a non-root uid.",
                fix=fix,
            )
        if as_root:
            if not count:
                return _result(OK, name, f"The app image runs as root; its data is also ready for {who}{scope}.")
            return _result(
                WARN, name,
                f"The app image runs as root. Before an image running as {who} can use its data, {entries} "
                f"need group access{scope}{detail}.",
                fix=fix,
            )
        if not count:
            return _result(OK, name, f"The app runs as {who} and can use its data{scope}.")
        return _result(
            FAIL, name,
            f"The app runs as {who} but cannot use {entries} of its data{scope}{detail}. "
            "DjangoLux refuses to start until it can.",
            fix=fix,
        )

    def _fix_app_permissions(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Grant the app its data; then start again what stopped on the refusal.

        An empty named volume gets ``app_permissions.KEEP_NAME`` only once every
        app container exists, so Docker has filled it from the image first.
        """
        config = self.compose_config_json() or {}
        keep = bool(config) and not app_permissions.missing_app_containers(self, config)
        try:
            summary = app_permissions.run(self, "apply", uid=state["uid"], gid=state["gid"], keep_empty=keep)
        except app_permissions.PermissionsError as exc:
            return _result(FAIL, "fix:app-permissions", str(exc))
        counts = app_permissions.totals(summary)
        if counts["errors"]:
            text = app_permissions.refusal(
                summary, state["uid"], state["gid"], "run ./start.sh check --fix again",
            )
            if counts["changed"]:
                text += f" The other {counts['changed']} entries were granted"
                text += f" (manifest {summary['manifest']})." if summary.get("manifest") else "."
            commands = app_permissions.host_commands(summary, state["gid"])
            entry = _result(FAIL, "fix:app-permissions", text)
            if commands:
                entry["manual"] = "on the host: " + "; ".join(commands)
            return entry
        detail = (
            f"Granted uid {state['uid']} (gid {state['gid']}) group access to {counts['changed']} "
            f"entr{'y' if counts['changed'] == 1 else 'ies'}"
        )
        if summary.get("manifest"):
            detail += f". Manifest (for 'composer app-permissions revert'): {summary['manifest']}"
        if counts["kept"]:
            detail += (
                f". Added {app_permissions.KEEP_NAME} to {counts['kept']} empty volume(s), so Docker does not "
                "reset their root when it creates a container"
            )
        if counts["hardlinks"]:
            detail += f". {counts['hardlinks']} multiply-linked file(s) were left unchanged"
        if counts["changed"] and state["uid"]:
            restarted = app_permissions.restart_refused(self, config, exited=True)
            if restarted:
                detail += f". Restarted {', '.join(restarted)}, which had stopped on the refusal"
                self._note_recreated(restarted)
        return _result(OK, "fix:app-permissions", detail + ".")

    def _check_update_policy(self) -> Dict[str, Any]:
        """Which update paths the deployment allows, and whether every reader receives the policy."""
        from . import update_policy, update_policy_env

        name = "update-policy"
        raw = self._compose_env_with_secrets().get(update_policy.ENV_KEY, "")
        policy, valid = update_policy.normalize(raw)
        allowed = {
            update_policy.ALL: "every update path is allowed",
            update_policy.INLINE: "DjangoLux inline updates only; application image and Composer updates "
            "are refused unless run from this shell",
            update_policy.NONE: "updates are refused unless run from this shell; checks and rollbacks still run",
        }[policy]
        detail = f"Update policy {policy}: {allowed}."
        model = self.compose_config_json() or {}
        missing = update_policy_env.missing_services(model)
        if missing:
            return _result(
                WARN, name,
                f"{detail} The stack does not pass {update_policy.ENV_KEY} to {', '.join(missing)}, so "
                + ("the resident pair cannot enforce it." if set(missing) & set(update_policy_env.RESIDENTS)
                   else "DjangoLux cannot show or apply it."),
                fix=f"Run 'composer check --fix': it passes {update_policy.ENV_KEY} to "
                f"{policy_readers((model.get('services') or {}).keys())}. "
                "Then run './start.sh' to recreate them.",
            )
        if not valid:
            return _result(
                WARN, name,
                f"{detail} {update_policy.ENV_KEY} has an unknown value in the secrets file, so it counts as none.",
                fix=f"Set {update_policy.ENV_KEY} to all, inline or none in the secrets file, then run './start.sh'.",
            )
        return _result(OK, name, detail)

    def _policy_readers(self, services=None) -> str:
        return policy_readers(self.services if services is None else services)

    def _check_relay_env(self) -> Dict[str, Any]:
        """Can DjangoLux's Email step see the SMTP relay's settings (all but the password)?"""
        name = "relay-env"
        try:
            state = relay_env.inspect(Path("."), self._maintenance_compose_files())
        except (OSError, ValueError) as exc:
            return _result(WARN, name, f"Could not read the Compose file: {exc}")
        compose = state["compose_file"] or "compose.yml"
        if state["state"] == relay_env.NO_SERVICE:
            return _result(OK, name, "No smtp-relay service running DjangoLux's relay; nothing to share.")
        if state["state"] == relay_env.CURRENT:
            return _result(
                OK, name, "web and worker see the SMTP relay's host, port, encryption and user, and the relay gets "
                "its certificate settings; the password reaches smtp-relay only."
            )
        if state["state"] == relay_env.LEGACY:
            tls = (
                "SMTP_RELAY_TLS_VERIFY and SMTP_RELAY_CA_FILE in the secrets file do not reach the relay, so it "
                "always verifies the provider's certificate against the system CAs"
            )
            if relay_env.MAIL in state["pending"]:
                message = (
                    f"{compose} passes the SMTP relay's settings to smtp-relay only, so DjangoLux's Email step "
                    "cannot show the host, port, encryption and user the relay uses"
                    + (f", and {tls}." if relay_env.TLS in state["pending"] else ".")
                )
            else:
                message = f"{compose} does not pass the relay's certificate settings: {tls}."
            return _result(
                WARN,
                name,
                message,
                fix="Run 'composer check --fix' to share the relay's mail and certificate settings through the "
                "x-environment block. The password stays on smtp-relay; web only learns whether one is set.",
            )
        details = "; ".join(state["details"])
        if state["shared_password"]:
            return _result(
                WARN,
                name,
                f"{compose} shares the SMTP relay password with every service that merges x-environment, "
                f"web and worker included ({details}).",
                fix="Move SMTP_RELAY_PASSWORD from the x-environment block to smtp-relay's environment, then "
                "run 'composer check' again.",
            )
        return _result(
            OK,
            name,
            f"The SMTP relay's settings in {compose} are custom, not managed by Composer ({details}); the "
            "Email step shows only what web receives.",
            fix="To have Composer share them, keep the values in the secrets file (SMTP_RELAY_HOST=...) and "
            "pass them to smtp-relay in the generated form (SMTP_RELAY_HOST: \"${SMTP_RELAY_HOST:-...}\"), "
            "then run 'composer check --fix'.",
        )

    def _check_relay_version(self) -> Optional[Dict[str, Any]]:
        """Does smtp-relay check the provider's certificate, as DjangoLux's Email step says it does?

        The relay runs the DjangoLux baked into its image: it mounts no runtime volume, so an inline update
        reaches web and worker but not the relay. Only a web on DjangoLux 1.16.2 or later shows the
        certificate check, so only then is the relay's own version read.
        """
        try:
            state = relay_env.inspect(Path("."), self._maintenance_compose_files())
        except (OSError, ValueError):
            return None
        if state.get("state") not in (relay_env.CURRENT, relay_env.LEGACY, relay_env.CUSTOM):
            return None
        floor = relay_env.CERTIFICATE_CHECK
        web = self._running_dlux_version()
        if web is None or tuple(web) < floor:
            return None
        relay = self._dlux_runtime_version(relay_env.SERVICE)
        if relay is None:
            return None
        relay_version = ".".join(map(str, relay))
        if tuple(relay) >= floor:
            return _result(
                OK, "relay-version",
                f"smtp-relay runs DjangoLux {relay_version}, which checks the mail provider's certificate before "
                "signing in.",
            )
        return _result(
            WARN,
            "relay-version",
            f"smtp-relay runs DjangoLux {relay_version} from the project image while web runs "
            f"{'.'.join(map(str, web))}, so the relay signs in to the mail provider without checking its "
            "certificate, although DjangoLux's Email step shows the check as on. The relay takes DjangoLux from "
            "its image; inline updates do not reach it.",
            fix=f"Move the stack to a project image built on DjangoLux {'.'.join(map(str, floor))} or later (a "
            "project release, or a rebuild from source), then run './start.sh'. The relay then refuses a "
            "provider whose certificate it cannot verify; for a private CA, set SMTP_RELAY_CA_FILE (or "
            "SMTP_RELAY_TLS_VERIFY=False) in the secrets file.",
        )

    @staticmethod
    def _tag_moved_note(image: str, digest: str) -> str:
        """Say so when the tag no longer names the image the last deploy recorded."""
        from .downgrade_guard import read_deployed_image

        deployed = read_deployed_image()
        recorded = deployed.get("repo_digest", "")
        if deployed.get("image") != image or "@" not in recorded or recorded.split("@", 1)[1] == digest:
            return ""
        return (
            f" The tag now names another image than the one deployed here ({recorded}); "
            "'./start.sh update' would deploy it."
        )

    def _check_registry_login(self) -> Optional[Dict[str, Any]]:
        """Can the resident pair read WEB_IMAGE's registry, with the login when it needs one?

        composer-agent's update check and composer-executor's pulls read the login
        (``registry_login``) from their own environment, so it counts only once the
        stack passes it to both. The registry is then asked for the watched tag the
        way the agent asks. The resident check skips that request: composer-executor
        answers it and has no route to a registry.
        """
        from . import registry, registry_login
        from .agent_check_publication import _option_values

        name = "registry-login"
        config = self.compose_config_json() or {}
        services = config.get("services") if isinstance(config.get("services"), dict) else {}
        agent = services.get("composer-agent")
        if not isinstance(agent, dict):
            return None
        command = agent.get("command") or []
        command = command.split() if isinstance(command, str) else [str(word) for word in command]
        environments = {
            role: (services.get(role) or {}).get("environment") or {}
            for role in registry_login.REGISTRY_ROLES if isinstance(services.get(role), dict)
        }
        agent_env = {key: str(value or "") for key, value in environments.get("composer-agent", {}).items()}
        images = list(dict.fromkeys(_option_values(command, "--check-image"))) or (
            [agent_env["WEB_IMAGE"]] if agent_env.get("WEB_IMAGE") else []
        )
        if not images:
            return None
        image = images[0]
        host = registry_login.image_host(image)
        lacking = sorted(
            role for role, environment in environments.items()
            if not all(key in environment for key in registry_login.KEYS)
        )
        if registry_login.credentials(self._compose_env_with_secrets()) and lacking:
            return _result(
                WARN, name,
                f"The secrets file holds a registry login, but the stack does not pass it to {', '.join(lacking)}, "
                f"so updates of {image} are neither noticed nor pulled.",
                fix="Run 'composer check --fix': it passes COMPOSER_REGISTRY_USERNAME, COMPOSER_REGISTRY_PASSWORD and "
                "COMPOSER_REGISTRY_HOST to composer-agent and composer-executor. Then run './start.sh' to recreate them.",
            )
        login = registry_login.credentials_for(image, agent_env)
        if getattr(self, "resident_check", False):
            with_login = "with the registry login" if login else "without a login"
            return _result(OK, name, f"The resident pair reads {image} {with_login}.")
        if self._is_local_build(image):
            return _result(OK, name, f"{image} was built on this machine and never pulled, so there is no registry to read.")
        token = agent_env.get("COMPOSER_REGISTRY_TOKEN") or None
        digest, reason = registry.tag_digest(image, token=token, credentials=login, timeout=10)
        if digest:
            how = "with the registry login" if login else ("with COMPOSER_REGISTRY_TOKEN" if token else "without a login")
            return _result(OK, name, f"{host} answers for {image} {how}.{self._tag_moved_note(image, digest)}")
        setting = (
            "Put a read-only login in the secrets file: COMPOSER_REGISTRY_USERNAME and COMPOSER_REGISTRY_PASSWORD "
            "(for GitLab, a deploy token with read_registry only), then run './start.sh' so composer-agent and "
            "composer-executor read it."
        )
        if reason in (registry.LOGIN_REQUIRED, registry.NOT_FOUND) and registry_login.is_bare_name(image):
            return _result(
                OK, name,
                f"{image} names no registry or namespace and Docker Hub has no official image by that name, "
                "so it is a local build with no registry to read.",
            )
        if reason == registry.LOGIN_REQUIRED:
            return _result(
                WARN, name,
                f"{host} needs a login to read {image}, and the stack passes none, so updates are neither noticed "
                "nor pulled.",
                fix=setting,
            )
        if reason == registry.LOGIN_REFUSED:
            return _result(
                FAIL, name,
                f"{host} refused the registry login for {image}, so updates are neither noticed nor pulled.",
                fix="Check COMPOSER_REGISTRY_USERNAME and COMPOSER_REGISTRY_PASSWORD in the secrets file (a deploy "
                "token must be active and allowed to read this repository), then run './start.sh'.",
            )
        if reason == registry.NOT_FOUND:
            return _result(
                WARN, name,
                f"{host} has no {image}" + ("." if login else ", or hides it until a login is given."),
                fix="Check WEB_IMAGE. " + ("" if login else setting),
            )
        if reason == registry.UNREACHABLE:
            return _result(OK, name, f"{host} is not reachable from here; {image} was not checked.")
        if reason == registry.CERTIFICATE:
            return _result(
                WARN, name,
                f"Composer does not trust {host}'s certificate, so updates of {image} are not noticed. Composer's "
                "update check trusts the public certificate authorities only.",
                fix="Use a registry certificate from a public authority. For pulls, the Docker daemon also needs a "
                "private CA at /etc/docker/certs.d/<registry host>/ca.crt on the host.",
            )
        return _result(WARN, name, f"Composer could not read {image} from {host}.")

    @staticmethod
    def _is_local_build(image: str) -> bool:
        """True when the local image exists with no RepoDigest: built here, never pulled.

        DjangoLux's Updates card reads such an image as a local build and shows no
        registry warning; the check must not ask for a login it does not need.
        """
        import subprocess

        try:
            proc = subprocess.run(
                ["docker", "image", "inspect", image, "--format", "{{json .RepoDigests}}"],
                capture_output=True, text=True, timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if proc.returncode != 0:
            return False
        try:
            digests = json.loads(proc.stdout.strip() or "null")
        except ValueError:
            return False
        return isinstance(digests, list) and not digests

    def _compose_env_with_secrets(self) -> Dict[str, str]:
        """The compose environment plus the secrets file's values, so a recreate never runs on defaults."""
        environment = self.build_compose_env()
        for candidate in self.plaintext_env_candidates():
            try:
                environment.update(self.parse_env_file(candidate))
                break
            except (OSError, ValueError):
                continue
        return environment

    def _check_maintenance_page(self) -> Dict[str, Any]:
        """Composer publishes the page (stack schema 3); a legacy stack still serves
        the project's ``.proxy/maintenance.html`` and is migrated by ``check --fix``."""
        migration = maintenance_migration.inspect(Path("."), self._maintenance_compose_files())
        if migration["legacy"]:
            return _result(
                WARN,
                "maintenance-page",
                "The proxy serves the project's .proxy/maintenance.html ("
                + ", ".join(sorted(migration["files"]))
                + "); since Composer 1.7 composer-agent publishes the page on the runtime volume.",
                fix="Run 'composer check --fix' to move it; a customised page is kept as the "
                ".proxy/maintenance/ override. The resident agent must be Composer 1.7+ "
                "('composer agent update' first).",
            )
        if "composer-agent" not in set(self.services):
            return _result(OK, "maintenance-page", "No composer-agent in this stack; nothing publishes a page.")
        if not self._agent_running():
            return _result(
                OK, "maintenance-page",
                "composer-agent is not running; it publishes the maintenance page when it starts ('./start.sh').",
            )
        if not self._maintenance_page_published():
            return _result(
                WARN,
                "maintenance-page",
                "The maintenance page is not published on the runtime volume, so the proxy has none to show.",
                fix="Run 'composer check --fix' (or restart composer-agent, which publishes it on start).",
            )
        if (Path(".") / maintenance_page.OVERRIDE).is_dir():
            if not self._published_page_has(maintenance_page.CUSTOM_MARKER):
                return _result(
                    WARN,
                    "maintenance-page",
                    f"{maintenance_page.OVERRIDE}/ exists but the published page is still Composer's own.",
                    fix="Run 'composer check --fix' to publish the project page.",
                )
            return _result(OK, "maintenance-page", f"Serving the project page from {maintenance_page.OVERRIDE}/.")
        return _result(OK, "maintenance-page", "Served from the runtime volume, published by composer-agent.")

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
                        fix="Run './start.sh update self'. Do not 'check --fix' this; it would downgrade the wrapper.",
                    )
                )
        return results

    def _run_deep(self, service: str, command: str) -> Dict[str, Any]:
        argv = command.split()
        argv = self.supervised_prefix(service, argv) + argv
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
        """Does the wrapper's channel and digest lock agree with what the stack runs?

        Three files, changed by different operations: ``.composer-channel``
        (the operator's choice), ``.composer-image`` (the digest the last
        verified update pinned) and the generated block. A deployer on one image
        talking to an agent on another is a configuration nobody chose and
        nothing else reports.

        A project with no lock that runs its channel's tag is not a finding: a
        freshly scaffolded stack looks exactly like that, and the next Composer
        update pins it. Pinning needs the network, which `check` never uses.
        """
        from . import channel_config, image_pin
        from .agent_installer import composer_images_in_block

        state = channel_config.describe(".")
        lock = state["lock"]
        note = f"Composer channel: {state['channel']} ({state['image']})"
        if state["pinned"]:
            note += " — COMPOSER_SELF_IMAGE pin overrides the channel"
        try:
            declared = set(composer_images_in_block(self._compose_contents()))
        except Exception:
            declared = set()
        if lock is not None and image_pin.predates_signing(lock.version) and not state["pinned"]:
            return _result(
                WARN,
                "composer-channel",
                f"{note}, but Composer {lock.version} predates digest pinning: the wrapper runs it from "
                ".composer-image as the deployer, and it can neither read the pinned Composer block nor move "
                "the pin again.",
                fix=PREDATES_PIN_RECOVERY,
            )
        if lock is not None and lock.prerelease and state["channel"] == channel_config.STABLE:
            return _result(
                WARN,
                "composer-channel",
                f"{note}, but the pinned Composer {lock.version} is a beta.",
                fix="Run 'composer check --stable' to pin the stable channel's release.",
            )
        if not declared:
            return _result(OK, "composer-channel", note)
        if lock is None:
            expected = state["channel_image"]
            drifted = sorted(image for image in declared if image != expected)
            if drifted:
                return _result(
                    WARN,
                    "composer-channel",
                    f"{note}, but the generated block names {', '.join(drifted)}.{self._running_pair_note()}",
                    fix=f"Run 'composer check --{state['channel']}' to move both together.",
                )
            return _result(
                OK,
                "composer-channel",
                f"{note}; resident pair agrees. Not pinned yet: the next Composer update pins a signed digest.",
            )
        drifted = sorted(image for image in declared if image != lock.ref)
        if drifted:
            return _result(
                WARN,
                "composer-channel",
                f"{note}, but the generated block names {', '.join(drifted)}.{self._running_pair_note()}",
                fix="Run 'composer check --fix' to pin the resident pair to .composer-image.",
            )
        # The block names the pin; the containers keep the image they were created from.
        stale = sorted(
            f"{service} still runs {image}"
            for service, image in self._resident_running_images().items()
            if image and image != lock.ref
        )
        if stale:
            return _result(
                WARN,
                "composer-channel",
                f"{note}; the generated block names the pin, but {'; '.join(stale)}: the pair was not recreated "
                "since the pin moved.",
                fix=f"Run '{stack_lifecycle.APPLY_COMMAND}': it recreates composer-agent and composer-executor "
                "on the pinned image.",
            )
        return _result(OK, "composer-channel", f"{note}; resident pair pinned to Composer {lock.short}.")

    def _running_pair_note(self) -> str:
        """What the resident pair's containers run now, which the block does not say."""
        running = self._resident_running_images()
        if not running:
            return " The resident pair is not running."
        version = self._resident_agent_version()
        shown = "; ".join(f"{service} runs {image}" for service, image in sorted(running.items()))
        return f" Running now: {shown}" + (f" (Composer {version})." if version else ".")

    def _resident_running_images(self) -> Dict[str, str]:
        """``{service: image}``: the reference each running resident container was created from.

        Empty when Docker cannot say, so `check` reports the block alone.
        """
        try:
            ok, rows, _detail = self.get_compose_ps_entries()
        except Exception:
            return {}
        return {
            str(row.get("Service")): str(row.get("Image") or "")
            for row in (rows if ok else [])
            if isinstance(row, dict) and row.get("Service") in stack_lifecycle.RESIDENT_SERVICES
            and str(row.get("State") or "").lower() == "running"
        }

    def _fix_composer_pin(self, args) -> List[Dict[str, Any]]:
        """Pin the resident pair to the existing lock. Offline: never resolves a new one."""
        from .agent_installer import AgentInstallError, pin_composer_image
        from .image_pin import predates_signing, read_lock

        lock = read_lock(".")
        if lock is None or os.environ.get("COMPOSER_SELF_IMAGE", "").strip():
            return []
        if predates_signing(lock.version):
            return [_result(
                WARN, "fix:composer-pin",
                f"The resident pair was not pinned to {lock.ref}: Composer {lock.version} predates digest "
                "pinning and could not manage the pin from there. The project is unchanged.",
                fix=PREDATES_PIN_RECOVERY,
            )]
        try:
            preview = pin_composer_image(".", pin=lock, compose_file=args.file or "", apply=False)
        except AgentInstallError as exc:
            return [_result(FAIL, "fix:composer-pin", str(exc))]
        if not preview.get("files"):
            return []
        if not confirm(
            f"Pin the resident Composer pair to {lock.ref}",
            [
                "The generated block's debeski/composer lines are rewritten; the original is kept under .xclude/.",
                "composer-agent and composer-executor are then recreated on that image (an Operations panel "
                "apply leaves them to './start.sh' on the server).",
            ],
            assume_yes=args.yes,
        ):
            return [_result(WARN, "fix:composer-pin", "Pinning the resident pair declined.")]
        if getattr(self, "_capture_lifecycle", False) and not getattr(self, "_fix_confirmed", False):
            self._fix_confirmed = True
            self._fix_baseline = self._lifecycle_baseline()
        try:
            outcome = pin_composer_image(".", pin=lock, compose_file=args.file or "", apply=True)
        except AgentInstallError as exc:
            return [_result(FAIL, "fix:composer-pin", str(exc))]
        return [_result(OK, "fix:composer-pin", f"Resident pair pinned to Composer {lock.short}.")]

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
        from . import image_pin
        from .agent_installer import AgentInstallError, switch_composer_channel
        from .channel_config import image_for_channel

        image = image_for_channel(channel)
        # A switch is the operator moving channels on purpose, so it may go to an
        # older release (beta -> stable). It is still a verified one.
        try:
            resolution = image_pin.resolve(
                channel,
                current=image_pin.read_lock("."),
                running=self.composer_version,
                allow_downgrade=True,
                allow_unverified=bool(getattr(args, "allow_unverified_composer", False))
                or image_pin.allow_unverified_from_env(),
            )
        except image_pin.PinError as exc:
            if exc.way_out:
                fix = exc.way_out
            elif exc.unavailable:
                fix = "Retry when GitHub is reachable, or pass --allow-unverified-composer to pin the registry digest."
            else:
                fix = "No flag overrides a signature or statement that does not match; report it."
            return [_result(
                FAIL,
                "composer-channel-switch",
                f"The {channel} channel's Composer release could not be pinned: {exc} The project is unchanged.",
                fix=fix,
            )]
        pin = resolution.pin
        image = pin.ref
        try:
            preview = switch_composer_channel(
                ".", channel=channel, compose_file=args.file or "", apply=False, pin=pin,
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

        # What runs before the write, so the lifecycle step recreates what the switch changed.
        baseline = self._lifecycle_baseline() if preview.get("files") else None
        try:
            outcome = switch_composer_channel(
                ".", channel=channel, compose_file=args.file or "", apply=True, pin=pin,
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
            detail += " The generated block already named that image."
        if resolution.note and not pin.verified:
            entries = [_result(WARN, "composer-channel-switch", f"{detail} {resolution.note}")]
        else:
            entries = [_result(OK, "composer-channel-switch", detail)]
        if preview.get("files"):
            # As `check --fix` does: the pair is recreated on the new pin, or, from
            # the Operations panel, recorded as pending for './start.sh'.
            entries.extend(self._finish_fix(args, baseline))
        return entries

    def collect_checkup(self, args) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Run the checks (and any fixes ``args`` asks for) and return them.

        Split out of ``run_checkup`` so a caller that is not a terminal — the
        deployment executor answering DjangoLux's Operations card — gets the same
        results without the printing, and without a second list of checks that
        could drift from this one.

        ``--fix`` runs one at a time per project (``check_lock``): a second one
        is refused before it reads anything, and a run that was killed is
        reported by the next.
        """
        self._fix_summary = None
        if not getattr(args, "fix", False):
            return self._collect_checkup(args)
        try:
            with check_lock.hold(".") as interrupted:
                results, fixed = self._collect_checkup(args)
        except check_lock.CheckFixLockError as exc:
            return [_result(FAIL, "check-fix", str(exc))], []
        if interrupted:
            fixed.insert(0, _result(
                WARN,
                "check-fix",
                f"The previous 'check --fix' ({check_lock.describe(interrupted)}) was stopped before it "
                "finished, so one of its fixes may have stopped halfway. This run checked everything again; "
                "the originals of what each fix changed are under .xclude/composer-check/.",
            ))
        return results, fixed

    def _collect_checkup(self, args) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        self.compose_file = args.file
        self.dev_mode = args.dev
        self.resolve_active_compose_files()
        self._worker_gate_cache = None
        self._celery_probe_cache = None
        self._stored_files_way_out_cache = None
        # A confirmed --fix takes the stack's state before its first write, then
        # brings the running stack to the files it wrote (_finish_fix).
        self._capture_lifecycle = bool(getattr(args, "fix", False))
        self._fix_confirmed = False
        self._fix_baseline = None
        self._fix_recreated = set()
        self._fix_force_recreate = set()
        self._operator_choices = {}
        self._skipped_questions = []
        self._written_secrets = {}

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
        project_name = self._check_project_name()
        if project_name is not None:
            results.append(project_name)
        results.append(self._check_secrets())
        results.extend(self._check_wrappers())
        results.append(self._check_composer_channel())
        if self.services:
            results.append(self._check_required_vars())
            results.append(self._check_topology())
            resident = self._check_resident_commands()
            if resident is not None:
                results.append(resident)
            proxy = self._check_docker_proxy()
            if proxy is not None:
                results.append(proxy)
            results.append(self._check_removed_services())
            results.append(self._check_dlux_updater_executor())
            results.append(self._check_proxy_routes())
            results.append(self._check_stack_schema())
            endings = self._check_line_endings()
            if endings is not None:
                results.append(endings)
            proxy_files = self._check_proxy_config()
            if proxy_files is not None:
                results.append(proxy_files)
            results.append(self._check_maintenance_page())
            results.append(self._check_media_privacy())
            results.append(self._check_fetch_metadata())
            results.append(self._check_front_proxy())
            results.append(self._check_relay_env())
            relay_version = self._check_relay_version()
            if relay_version is not None:
                results.append(relay_version)
            registry_check = self._check_registry_login()
            if registry_check is not None:
                results.append(registry_check)
            results.append(self._check_update_policy())
            redis = self._check_redis_persistence()
            if redis is not None:
                results.append(redis)
            permissions = self._check_app_permissions(deep=bool(args.deep or args.fix))
            if permissions is not None:
                results.append(permissions)
            worker = self._check_task_worker()
            if worker is not None:
                results.append(worker)
            floor = self._check_image_floor()
            if floor is not None:
                results.append(floor)
            drift = self._check_scaffold_drift()
            if drift is not None:
                results.append(drift)
            celery = self._check_celery()
            if celery is not None:
                results.append(celery)
            states = self._check_service_states()
            if states is not None:
                results.append(states)
            corrupt = self._check_redis_aof()
            if corrupt is not None:
                results.append(corrupt)
            pending = self._check_pending_recreate()
            if pending is not None:
                results.append(pending)
            results.append(self._check_versions())
            if args.deep:
                results.append(self._run_deep(args.deep_service, args.deep_command))

        before = {result.get("name"): result.get("level") for result in results}
        if args.fix and guidance.interactive(args, resident=self._resident_apply(args)):
            self._ask_operator_questions(results)
        fixed = switched + (self._maybe_fix(args, results) if args.fix else [])
        if args.fix:
            # After the other repairs: hardening re-renders the block on its tag.
            pinned = self._fix_composer_pin(args)
            if pinned:
                fixed.extend(pinned)
                index = next((i for i, r in enumerate(results) if r["name"] == "composer-channel"), None)
                if index is not None:
                    results[index] = self._check_composer_channel()
            if self._fix_confirmed:
                fixed.extend(self._finish_fix(args, self._fix_baseline))
        if args.fix and any(entry["level"] == OK and entry["name"].startswith("fix:") for entry in fixed):
            # The fixes may have added or removed services.
            self.discover_services(silent=True)
        if args.fix:
            self._recheck_after_fix(results)
            declined = any(
                entry.get("level") == WARN and str(entry.get("message", "")).endswith("declined.") for entry in fixed
            )
            self._fix_summary = guidance.summary(
                results, fixed, self._skipped_questions, before=before, declined=declined,
            )

        return results, fixed

    def _recheck_after_fix(self, results: List[Dict[str, Any]]) -> None:
        """Report, in place, what the repairs changed as it now stands.

        A repaired FAIL must not fail the run. After a confirmed run every check a
        repair can change is read again, whatever it said before (a removed
        docker-socket-proxy, a restamped schema, a dropped service's count, an
        updated wrapper), so the report and the summary say what stands now rather
        than what was. A check that no longer reports because the run removed its
        service leaves the report, as it would from a fresh `check`.
        """
        rechecks = {
            "resident-commands": self._check_resident_commands,
            "docker-proxy": self._check_docker_proxy,
            # A successful fix:stored-files must not leave the stale SECURITY
            # FAIL behind, or `check --fix` exits 1 after fixing it.
            "stored-files": self._check_media_privacy,
            "app-permissions": lambda: self._check_app_permissions(deep=True),
            "stack-schema": self._check_stack_schema,
            "task-worker": self._check_task_worker,
            "image-floor": self._check_image_floor,
            "proxy-config": self._check_proxy_config,
            "services": self._check_service_states,
            "redis-aof-corrupt": lambda: self._check_redis_aof(report_ok=True),
            "redis-persistence": self._check_redis_persistence,
        }
        always = ("app-permissions", "redis-persistence")
        confirmed = bool(getattr(self, "_fix_confirmed", False))
        refreshed = {
            "front-proxy": self._check_front_proxy,
            "update-policy": self._check_update_policy,
            "relay-env": self._check_relay_env,
            "removed-services": self._check_removed_services,
            "proxy-routes": self._check_proxy_routes,
            "topology": self._check_topology,
            "compose-config": self._check_compose_parses,
            "celery": self._check_celery,
        } if confirmed else {}
        wrappers_now = {entry["name"]: entry for entry in self._check_wrappers()} if confirmed else {}
        # The service whose removal leaves a check with nothing to report.
        subject = {"docker-proxy": "docker-socket-proxy", "celery": "celery"}
        current = []
        for result in results:
            name, level = result.get("name"), result.get("level")
            if name in wrappers_now:
                current.append(wrappers_now[name])
                continue
            if name in rechecks and (level == FAIL or name in always or confirmed):
                check = rechecks[name]
            elif name in refreshed:
                check = refreshed[name]
            else:
                current.append(result)
                continue
            rechecked = check()
            if rechecked is not None:
                current.append(rechecked)
            elif not (confirmed and name in subject and subject[name] not in set(self.services)):
                current.append(result)
        results[:] = current
        if confirmed and self.services:
            # The lifecycle step may have replaced the containers a record named, or left new ones behind.
            pending = self._check_pending_recreate()
            results[:] = [result for result in results if result.get("name") != "pending-recreate"]
            if pending is not None:
                at = next((i for i, r in enumerate(results) if r.get("name") == "versions"), len(results))
                results.insert(at, pending)

    def run_checkup(self, args) -> int:
        results, fixed = self.collect_checkup(args)

        if args.json:
            import json

            print(json.dumps({"results": results, "fixes": fixed}, indent=2))
        else:
            self._print_checkup(results, fixed)
            if getattr(args, "fix", False):
                guidance.print_summary(getattr(self, "_fix_summary", None))

        return 1 if any(r["level"] == FAIL for r in results + fixed) else 0

    def _ask_operator_questions(self, results: List[Dict[str, Any]], *, reader=input, stream=None) -> None:
        """Ask the operator-choice findings as questions; record the answers and the skips.

        Only for a terminal run (``guidance.interactive``). Nothing is written
        here: the answers become part of the plan the confirmation lists.
        """
        from . import update_policy

        choices: Dict[str, str] = {}
        skipped: List[Dict[str, str]] = []
        self._operator_choices, self._skipped_questions = choices, skipped
        found = {result.get("name"): result.get("level") for result in results}
        values = guidance.secret_values(Path("."))
        if values is not None and found.get("front-proxy") == WARN:
            environment = self.build_compose_env()
            try:
                state = front_proxy.inspect(Path("."), self._maintenance_compose_files(), environment)
            except OSError:
                state = None
            if (
                state and not state["set"] and not state["unsupported"]
                and state["compose"] in (front_proxy.PASSED, front_proxy.ABSENT)
                and state["caddyfile"] in (front_proxy.LEGACY, front_proxy.MANAGED)
                and not front_proxy.blocked_reason(Path("."), environment)
            ):
                value = guidance.ask_front_proxy(reader=reader, stream=stream)
                if value is None:
                    skipped.append({"name": "front-proxy", "command": guidance.ASK_AGAIN})
                else:
                    choices[front_proxy.VARIABLE] = value
        readers = {"web", "worker", "composer-agent", "composer-executor"}
        if values is not None and set(self.services) & readers:
            raw = values.get(update_policy.ENV_KEY)
            policy, valid = update_policy.normalize((raw or "").strip("'\""))
            if raw is None or not valid:
                value = guidance.ask_update_policy(policy, reader=reader, stream=stream)
                if value is None:
                    skipped.append({"name": "update-policy", "command": guidance.ASK_AGAIN})
                else:
                    choices[update_policy.ENV_KEY] = value
        if found.get("redis-persistence") == WARN and found.get("redis-aof-corrupt") != FAIL:
            if not guidance.ask_redis(reader=reader, stream=stream):
                choices["redis"] = "keep"
                skipped.append({"name": "redis-persistence", "command": guidance.ASK_AGAIN})

    def _write_operator_choices(self, values: Dict[str, str]) -> Dict[str, Any]:
        """Write the answers to the secrets file, in place; the recreate that follows reads them."""
        try:
            path = guidance.set_secret_values(Path("."), values)
        except (OSError, ValueError) as exc:
            return _result(FAIL, "fix:secrets", f"Could not write {', '.join(values)} to the secrets file: {exc}")
        written = dict(getattr(self, "_written_secrets", None) or {})
        written.update(values)
        self._written_secrets = written
        try:
            shown = path.relative_to(Path(".").resolve())
        except ValueError:
            shown = path
        return _result(
            OK, "fix:secrets",
            f"Wrote {', '.join(values)} to {shown} in place (only {'that line' if len(values) == 1 else 'those lines'} "
            "changed; the file is not copied).",
        )

    def _fix_plan(self, args, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """What ``check --fix`` would do on this stack: each repair's flag, read-only.

        ``_maybe_fix`` lists, confirms and applies it; the Operations preview
        reads from it the services an apply would remove and recreate.
        """
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
        # Retire dlux-updater into Compose init containers on worker, or on celery
        # before stack schema 5 (the worker step below then moves them). Gated
        # on the dlux the IMAGE ships: on an older release that service still owns
        # the update path, and on Compose older than 5.3.0 `pre_start` is
        # silently ignored, which would leave the stack unmigrated on boot.
        from .agent_installer import init_container_host

        init_host = init_container_host(set(self.services))
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
        needs_worker, worker_blocked = self._worker_fix_plan(needs_init_containers, init_containers_blocked)
        # celery in a project image without Celery crash-loops for good once
        # worker runs DjangoLux's tasks: removed after the worker step below.
        services_now = set(self.services)
        retire_celery = bool(
            "celery" in services_now
            and ("worker" in services_now or needs_worker == "add")
            and self._celery_unusable()
        )
        if updater_migration_blocked:
            fixes.append(_result(WARN, "dlux-updater-runtime", updater_migration_blocked))
        if init_containers_blocked:
            fixes.append(_result(WARN, "dlux-init-containers", init_containers_blocked))
        if dev_override_blocked:
            fixes.append(_result(WARN, "dev-compose", dev_override_blocked))
        if worker_blocked:
            fixes.append(_result(WARN, "task-worker", worker_blocked))
        # An AHEAD wrapper is deliberately not fixable: the image is the stale
        # side there, and writing the baked copy would downgrade the project.
        stale_wrappers = [
            entry for entry in wrappers.inspect_wrappers(".") if entry["status"] in wrappers.FIXABLE
        ]
        maintenance_legacy = maintenance_migration.inspect(
            Path("."), self._maintenance_compose_files()
        )["legacy"]
        maintenance_unpublished = not maintenance_legacy and any(
            result.get("name") == "maintenance-page" and result.get("level") == WARN for result in results
        )
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
            return {"fixes": fixes, "refused": True, "pending": False}
        proxy_routes = proxy_inspection["recognized"]
        media_exposed = media_migration.inspect(Path("."))["exposed"]
        try:
            front_proxy_files = front_proxy.inspect(
                Path("."), self._maintenance_compose_files(), self.build_compose_env()
            )["files"]
        except OSError:
            front_proxy_files = []
        front_proxy_blocked = front_proxy.blocked_reason(Path("."), self.build_compose_env()) if front_proxy_files else ""
        if front_proxy_blocked:
            fixes.append(_result(WARN, "front-proxy", front_proxy_blocked))
            front_proxy_files = []
        try:
            relay_env_files = relay_env.inspect(Path("."), self._maintenance_compose_files())["files"]
        except (OSError, ValueError):
            relay_env_files = []
        from . import update_policy_env
        from .stack_cleanup import StackCleanupError

        try:
            update_policy_files = update_policy_env.preview(".", self._maintenance_compose_files())["files"]
        except (OSError, ValueError, StackCleanupError):
            update_policy_files = []
        choices = getattr(self, "_operator_choices", None) or {}
        secret_writes = {key: choices[key] for key in ("DLUX_FRONT_PROXY", "DLUX_UPDATE_POLICY") if key in choices}
        redis_files: List[str] = []
        if redis_persistence.SERVICE in set(self.services) and choices.get("redis") != "keep":
            try:
                redis_plan = redis_persistence.preview(".", self._maintenance_compose_files())
            except (OSError, ValueError, StackCleanupError) as exc:
                redis_plan = {"files": [], "note": f"Cannot plan the redis command rewrite: {exc}"}
            redis_files = redis_plan["files"]
            if not redis_files and redis_plan["note"]:
                fixes.append(_result(WARN, "redis-persistence", redis_plan["note"]))
            elif not redis_files and any(
                result.get("name") == "redis-persistence" and result.get("level") == WARN for result in results
            ):
                fixes.append(_result(
                    WARN, "redis-persistence",
                    "Composer cannot rewrite this redis service's command: it edits a block-style service whose "
                    "command is a list or a plain string, and this one is written another way (a flow mapping "
                    "such as 'redis: {...}', for example). Nothing was changed.",
                    manual="edit redis's command in the Compose file by hand: replace --appendonly yes with "
                    "--appendonly no --save 60 1 and keep its other flags, then run './start.sh'",
                ))
        redis_corrupt = any(
            result.get("name") == "redis-aof-corrupt" and result.get("level") == FAIL for result in results
        )
        grant_access = getattr(self, "_app_permissions_state", None) or {}
        if not (grant_access.get("wrong") or grant_access.get("missing")):
            grant_access = {}
        from .stack_cleanup import StackCleanupError

        try:
            stamp_repair = stack_layout.preview(".", self._maintenance_compose_files())
        except (OSError, ValueError, StackCleanupError) as exc:
            stamp_repair = {"files": [], "notes": [f"Cannot plan the stack schema stamp repair: {exc}"], "schema": None}
        if stamp_repair["notes"] and not stamp_repair["files"]:
            fixes.append(_result(WARN, "stack-schema", " ".join(stamp_repair["notes"])))
        if getattr(self, "_invalid_compose_stamp", None) and (needs_worker or stamp_repair["files"]):
            fixes.append(_result(
                WARN, "task-worker",
                "The schema 5 step and the restamp wait until DLUX_STACK_SCHEMA in the Compose file is a stack "
                "schema number (see stack-schema); nothing was added or restamped.",
            ))
            needs_worker, stamp_repair = "", dict(stamp_repair, files=[])
        plan: Dict[str, Any] = dict(
            fixes=fixes,
            refused=False,
            legacy=legacy,
            needs_hardening=needs_hardening,
            needs_agent_block_migration=needs_agent_block_migration,
            needs_updater_migration=needs_updater_migration,
            needs_install=needs_install,
            init_host=init_host,
            needs_init_containers=needs_init_containers,
            needs_dev_override_migration=needs_dev_override_migration,
            dev_override_file=dev_override_file,
            base_compose_file=base_compose_file,
            needs_post_start_migration=needs_post_start_migration,
            needs_restart_labels=needs_restart_labels,
            needs_worker=needs_worker,
            retire_celery=retire_celery,
            stale_wrappers=stale_wrappers,
            maintenance_legacy=maintenance_legacy,
            maintenance_unpublished=maintenance_unpublished,
            obsolete=obsolete,
            proxy_routes=proxy_routes,
            media_exposed=media_exposed,
            front_proxy_files=front_proxy_files,
            relay_env_files=relay_env_files,
            update_policy_files=update_policy_files,
            grant_access=grant_access,
            stamp_repair=stamp_repair,
            redis_files=redis_files,
            redis_corrupt=redis_corrupt,
            secret_writes=secret_writes,
        )
        mixed = getattr(self, "_mixed_endings", None) or []
        if mixed:
            # Only the repairs that change no stack file still run.
            held = [key for key in self._FILE_REPAIRS if plan.get(key)]
            for key in held:
                plan[key] = type(plan[key])()
            if stamp_repair["files"]:
                held.append("stamp_repair")
                plan["stamp_repair"] = dict(stamp_repair, files=[])
            if held:
                fixes.append(_result(
                    WARN, "line-endings",
                    f"No stack file was changed: {', '.join(mixed)} mix{'es' if len(mixed) == 1 else ''} line "
                    "endings (see line-endings).",
                    manual=f"convert {' '.join(mixed)} to one line-ending style by hand, then ./start.sh check --fix",
                ))
        plan["pending"] = bool(
            legacy or obsolete or proxy_routes or needs_hardening or needs_agent_block_migration
            or needs_updater_migration or needs_post_start_migration or stale_wrappers
            or maintenance_legacy or maintenance_unpublished or needs_install or needs_init_containers
            or needs_restart_labels or needs_dev_override_migration or media_exposed or needs_worker
            or front_proxy_files or relay_env_files or grant_access or update_policy_files
            or stamp_repair["files"] or retire_celery or redis_files or redis_corrupt or secret_writes
        )
        return plan

    #: Plan keys whose repair writes a stack file (everything but the grant, the
    #: page publish, the redis recovery and the secrets answers).
    _FILE_REPAIRS = (
        "legacy", "needs_hardening", "needs_agent_block_migration", "needs_updater_migration", "needs_install",
        "needs_init_containers", "needs_dev_override_migration", "needs_post_start_migration", "needs_restart_labels",
        "needs_worker", "retire_celery", "stale_wrappers", "maintenance_legacy", "obsolete", "proxy_routes",
        "media_exposed", "front_proxy_files", "relay_env_files", "update_policy_files", "redis_files",
    )

    def _read_only_stack_files(self, extra=()) -> List[Path]:
        """The Compose files (and ``extra``) this process cannot write; a repair that writes them is held."""
        from .stack_schema import read_only_files

        files = [Path(name) for name in dict.fromkeys([*self._maintenance_compose_files(), *extra]) if Path(name).is_file()]
        return read_only_files(files)

    @staticmethod
    def _read_only_stack_files_only(names) -> List[Path]:
        from .stack_schema import read_only_files

        return read_only_files([Path(name) for name in names if Path(name).is_file()])

    def _maybe_fix(self, args, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        plan = self._fix_plan(args, results)
        fixes = plan["fixes"]
        if plan["refused"] or not plan["pending"]:
            return fixes
        legacy = plan["legacy"]
        needs_hardening = plan["needs_hardening"]
        needs_agent_block_migration = plan["needs_agent_block_migration"]
        needs_updater_migration = plan["needs_updater_migration"]
        needs_install = plan["needs_install"]
        init_host = plan["init_host"]
        needs_init_containers = plan["needs_init_containers"]
        needs_dev_override_migration = plan["needs_dev_override_migration"]
        dev_override_file = plan["dev_override_file"]
        base_compose_file = plan["base_compose_file"]
        needs_post_start_migration = plan["needs_post_start_migration"]
        needs_restart_labels = plan["needs_restart_labels"]
        needs_worker = plan["needs_worker"]
        retire_celery = plan["retire_celery"]
        stale_wrappers = plan["stale_wrappers"]
        maintenance_legacy = plan["maintenance_legacy"]
        maintenance_unpublished = plan["maintenance_unpublished"]
        obsolete = plan["obsolete"]
        proxy_routes = plan["proxy_routes"]
        media_exposed = plan["media_exposed"]
        front_proxy_files = plan["front_proxy_files"]
        relay_env_files = plan["relay_env_files"]
        update_policy_files = plan["update_policy_files"]
        grant_access = plan["grant_access"]
        stamp_repair = plan["stamp_repair"]
        redis_files = plan["redis_files"]
        redis_corrupt = plan["redis_corrupt"]
        secret_writes = plan["secret_writes"]
        consequences = []
        if secret_writes:
            one = len(secret_writes) == 1
            consequences.append(
                "Write " + ", ".join(f"{key}={value}" for key, value in secret_writes.items())
                + f" to the secrets file in place: only {'that line changes' if one else 'those lines change'} "
                "(added at the end when missing), and the file is not copied. The services that read "
                f"{'it' if one else 'them'} are recreated."
            )
        if maintenance_legacy:
            consequences.append(
                "Move the maintenance page to Composer: rewrite the proxy config to serve it from "
                "the runtime volume, drop the .proxy/maintenance.html mount, keep a customised page "
                "as .proxy/maintenance/, publish it through composer-agent and recreate the proxy."
            )
        if maintenance_unpublished:
            consequences.append("Publish the maintenance page through composer-agent.")
        if media_exposed:
            consequences.append(
                "SECURITY: make stored files private (DjangoLux stack schema 4). The proxy will serve only "
                "media/public/, branding and managed images and fonts to everyone, and ask DjangoLux before "
                "serving any other /media/ file. Uploads your site shows to signed-out visitors (product "
                "photos, for example) must first be listed in DLUX_PUBLIC_MEDIA or moved under media/public/. "
                "The proxy config is validated, archived under .xclude/composer-check/ and the proxy recreated."
            )
        if front_proxy_files:
            consequences.append(
                "Let DLUX_FRONT_PROXY in the secrets file choose the front proxies Caddy trusts ("
                + ", ".join(front_proxy_files)
                + "). Left unset, Caddy keeps trusting every private network. The Compose and Caddy "
                "configuration is validated, the originals archived under .xclude/composer-check/ and a "
                "running caddy recreated."
            )
        if relay_env_files:
            consequences.append(
                "Share the SMTP relay's host, port, encryption and user with web and worker so DjangoLux's "
                "Email step can show them, and pass the relay its certificate settings (SMTP_RELAY_TLS_VERIFY, "
                "SMTP_RELAY_CA_FILE) (" + ", ".join(relay_env_files) + "). The password stays on "
                "smtp-relay; web only learns whether one is set. The Compose configuration is validated, the "
                "original archived under .xclude/composer-check/ and running web, worker and smtp-relay "
                "recreated."
            )
        if redis_corrupt:
            consequences.append(
                "Recover Redis from its damaged append-only file: stop redis, move the append-only files on its "
                "volume aside into aof-archive/<UTC time>/ on the same volume (kept, not deleted) with a one-off "
                "container that has no network, switch it to snapshots and recreate it. Users sign in once more."
            )
        if redis_files:
            consequences.append(
                "Switch Redis from an append-only file to RDB snapshots (" + ", ".join(redis_files) + "): "
                "--appendonly no --save 60 1, its other flags kept. A hard stop can leave a half-written "
                "append-only file Redis refuses to start from; a snapshot is renamed into place only when "
                "complete. Up to about a minute of cache and sessions can be lost after a crash. The original is "
                "archived under .xclude/composer-check/ and redis is recreated."
            )
        if update_policy_files:
            consequences.append(
                f"Pass DLUX_UPDATE_POLICY from the secrets file to {self._policy_readers()} ("
                + ", ".join(update_policy_files) + "), so the resident pair enforces "
                "the deployment update policy. The original is archived under .xclude/composer-check/; "
                "run './start.sh' afterwards to recreate the services."
            )
        if grant_access:
            consequences.append(
                f"Give the app (uid {grant_access['uid']}, gid {grant_access['gid']}) group access to the data "
                "its services mount: add the group read, write and search bits it needs to directories and files "
                "(the group itself changes only on entries owned by neither group 0 nor the app). Nothing is "
                "moved, renamed or rewritten, and the previous root image keeps working. Every change is listed "
                "in .xclude/composer-check/<UTC>/app-permissions/ for 'composer app-permissions revert'."
            )
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
                    "Migrate composer-updater to the hardened topology: composer-agent and "
                    "composer-executor (sole Docker authority). The docker-socket-proxy "
                    "container is removed.",
                ]
            )
        if needs_init_containers:
            consequences.append(
                "Retire the dlux-updater service: its runtime reconcile and migrations "
                f"become Compose init containers (pre_start) on {init_host}, its depends_on "
                f"edges are removed, {init_host} gains write access to the runtime volume and "
                "staticfiles, and web's org.dlux.post-start migrator hook is dropped "
                "(it would now be a second, redundant run). The dlux_runtime volume and "
                "its releases are kept."
            )
        if needs_dev_override_migration:
            consequences.append(
                "Normalize compose.dev.yml for the init-container topology: remove the "
                "development dlux-updater override, keep the dlux_runtime mount of worker "
                "and celery read-write, and disable inline updates in dev containers."
            )
        if needs_worker == "add":
            consequences.append(
                "Add DjangoLux's task worker (stack schema 5): copy celery, with its mounts and "
                "environment, to a new 'worker' service that runs 'manage.py dlux_worker', and "
                "move the init containers (dlux_reconcile, migrator) and DLUX_BOOT_GATE=apply "
                "to it. celery stays for the project's own tasks. Add worker to "
                "COMPOSER_AGENT_RESTART_SERVICES, mirror a compose.dev.yml celery override in "
                "development, and restamp schema 4 files to 5. The next deploy starts worker."
            )
        elif needs_worker:
            consequences.append(
                "Finish stack schema 5: mirror the compose.dev.yml celery override for worker "
                "in development, and bring the stack's schema stamps to 5."
            )
        if retire_celery:
            consequences.append(
                f"Remove celery: {self._celery_unusable()} worker runs DjangoLux's tasks. Its service block "
                "(and a compose.dev.yml override) leaves the Compose files and COMPOSER_AGENT_RESTART_SERVICES, "
                "its container is stopped and removed, and volumes are kept. The configuration is validated "
                "first and the originals archived under .xclude/composer-check/; restoring them brings celery back."
            )
        if stamp_repair["files"]:
            consequences.append(
                f"Restamp {', '.join(stamp_repair['files'])} to stack schema {stamp_repair['schema']}, the schema "
                "the stack's files follow: their own stamps disagree (a restamp changed only some of them), and "
                "DjangoLux reads DLUX_STACK_SCHEMA. Only the stamps change; the secrets file is rewritten in place "
                "and not copied."
            )
        if needs_install:
            consequences.append(
                "Install the Composer services this stack is missing (composer-executor, "
                "composer-agent) plus their volumes and the docker_proxy "
                "network. DjangoLux 1.8.0 hands inline updates to Composer, so without them "
                "this deployment has no update path."
            )
        if needs_hardening:
            consequences.extend(
                [
                    "Harden composer-agent into the executor topology: add composer-executor "
                    "(sole Docker authority) and remove docker-socket-proxy, its container "
                    "included.",
                ]
            )
        if needs_agent_block_migration:
            consequences.append(
                "Normalize the generated Composer resident block: use nested "
                "agent/executor run commands; give composer-executor cap_add: "
                "DAC_OVERRIDE (replacing DAC_READ_SEARCH) so it can read the project's "
                "0600 .secrets/.env to deploy; remove docker-socket-proxy (its container, "
                "service, composer-agent's DOCKER_HOST and depends_on entry), which would let "
                "composer-agent read every container's environment; pass the private-registry login "
                "(COMPOSER_REGISTRY_USERNAME, COMPOSER_REGISTRY_PASSWORD, COMPOSER_REGISTRY_HOST) "
                "to composer-agent and composer-executor."
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
        consequences.extend(self._lifecycle_consequences(plan, args))
        if not confirm(
            "composer check --fix will apply safe stack migrations",
            consequences,
            assume_yes=getattr(args, "yes", False),
        ):
            fixes.append(_result(WARN, "fix", "Changes declined."))
            return fixes
        self._fix_confirmed = True
        if getattr(self, "_capture_lifecycle", False):
            # After the answer, before the first write: what the stack runs now.
            self._fix_baseline = self._lifecycle_baseline()
        if secret_writes:
            # First: the front-proxy repair validates Caddy with the value chosen.
            fixes.append(self._write_operator_choices(secret_writes))

        # These repairs replace the file by rename, which a read-only file does
        # not stop (its mode was then lost): hold them, as the other repairs refuse.
        compose_repairs = [name for name, wanted in (
            ("obsolete-stack", obsolete), ("dlux-init-containers", needs_init_containers),
            ("dev-compose", needs_dev_override_migration), ("install-composer", needs_install),
            ("agent-enable", legacy), ("executor-enable", needs_hardening),
            ("resident-block", needs_agent_block_migration), ("dlux-updater-runtime", needs_updater_migration),
            ("post-start-label", needs_post_start_migration), ("restart-labels", needs_restart_labels),
            ("maintenance-page", maintenance_legacy),
        ) if wanted]
        proxy_repairs = [name for name, wanted in (
            ("proxy-routes", proxy_routes), ("maintenance-page", maintenance_legacy),
        ) if wanted]
        read_only = self._read_only_stack_files() if compose_repairs else []
        proxy_read_only = self._read_only_stack_files_only(
            [".proxy/Caddyfile", ".proxy/default.conf.template", ".nginx/nginx.conf"]
        ) if proxy_repairs else []
        if read_only or proxy_read_only:
            from .stack_schema import read_only_message

            held = list(dict.fromkeys((compose_repairs if read_only else []) + (proxy_repairs if proxy_read_only else [])))
            fixes.append(_result(
                FAIL, "fix:read-only-files",
                read_only_message(".", [*read_only, *proxy_read_only]) + f" Held: {', '.join(held)}.",
            ))
            if read_only:
                obsolete, needs_init_containers, needs_dev_override_migration, needs_install = [], False, False, False
                legacy = needs_hardening = needs_agent_block_migration = needs_updater_migration = False
                needs_post_start_migration = needs_restart_labels = maintenance_legacy = False
            if proxy_read_only:
                proxy_routes, maintenance_legacy = [], False

        if stale_wrappers:
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

        if (maintenance_legacy or maintenance_unpublished) and not self._agent_running():
            fixes.append(_result(
                WARN, "fix:maintenance-page",
                "composer-agent is not running, so it cannot publish the maintenance page now"
                + (" and the proxy was not moved over to it" if maintenance_legacy else "")
                + ". Run './start.sh'"
                + (", then './start.sh check --fix' to move the proxy to the published page." if maintenance_legacy
                   else ": composer-agent publishes the page when it starts."),
            ))
        elif maintenance_legacy:
            from .stack_cleanup import StackCleanupError
            from .versions import at_least

            agent_version = self._resident_agent_version()
            if not agent_version or not at_least(agent_version, self.MAINTENANCE_AGENT_FLOOR):
                fixes.append(
                    _result(
                        FAIL,
                        "fix:maintenance-page",
                        f"composer-agent is {agent_version or 'not running'}; publishing the maintenance page "
                        f"needs {self.MAINTENANCE_AGENT_FLOOR}+. Run 'composer agent update', then "
                        "'composer check --fix' again.",
                    )
                )
            else:
                try:
                    outcome = maintenance_migration.migrate(
                        ".",
                        self._maintenance_compose_files(),
                        environment=self.build_compose_env(),
                    )
                    detail = f"Migrated {', '.join(outcome['files'])}"
                    if outcome["override"]:
                        detail += f"; kept the customised page as {outcome['override']}"
                    if outcome["recreated"]:
                        detail += f"; recreated {', '.join(outcome['recreated'])}"
                        self._note_recreated(outcome["recreated"])
                    fixes.append(_result(OK, "fix:maintenance-page", f"{detail}. Backup: {outcome['backup_root']}"))
                except (OSError, StackCleanupError) as exc:
                    fixes.append(_result(FAIL, "fix:maintenance-page", str(exc)))
        elif maintenance_unpublished:
            from .stack_cleanup import StackCleanupError

            try:
                project_page = maintenance_migration.publish_through_agent(
                    Path(".").resolve(),
                    [Path(c).resolve() for c in self._maintenance_compose_files()],
                    self.build_compose_env(),
                )
                fixes.append(
                    _result(
                        OK,
                        "fix:maintenance-page",
                        f"Published the project page from {maintenance_page.OVERRIDE}/." if project_page
                        else "Published the maintenance page.",
                    )
                )
            except StackCleanupError as exc:
                fixes.append(_result(FAIL, "fix:maintenance-page", str(exc)))

        if media_exposed:
            from .stack_cleanup import StackCleanupError

            running = self._running_dlux_version()
            if running is None or tuple(running) < media_migration.MEDIA_ACCESS_FLOOR:
                fixes.append(
                    _result(
                        FAIL,
                        "fix:stored-files",
                        f"DjangoLux {'.'.join(map(str, running)) if running else 'version unknown'} has no "
                        "media access check, so switching the proxy now would refuse every private file. "
                        + self._stored_files_way_out(),
                    )
                )
            else:
                try:
                    outcome = media_migration.migrate(
                        ".", self._maintenance_compose_files(), environment=self.build_compose_env()
                    )
                    detail = f"Made stored files private: {', '.join(outcome['files'])}"
                    if outcome["recreated"]:
                        detail += f"; recreated {', '.join(outcome['recreated'])}"
                        self._note_recreated(outcome["recreated"])
                    fixes.append(_result(OK, "fix:stored-files", f"{detail}. Backup: {outcome['backup_root']}"))
                except (OSError, StackCleanupError) as exc:
                    fixes.append(_result(FAIL, "fix:stored-files", str(exc)))

        if front_proxy_files:
            from .stack_cleanup import StackCleanupError

            try:
                outcome = front_proxy.migrate(
                    ".", self._maintenance_compose_files(), environment=self._compose_env_with_secrets()
                )
                detail = f"Caddy reads its trusted front proxies from DLUX_FRONT_PROXY: {', '.join(outcome['files'])}"
                if outcome["recreated"]:
                    detail += f"; recreated {', '.join(outcome['recreated'])}"
                    self._note_recreated(outcome["recreated"])
                if outcome["notes"]:
                    detail += ". " + " ".join(outcome["notes"])
                fixes.append(_result(OK, "fix:front-proxy", f"{detail}. Backup: {outcome['backup_root']}"))
            except (OSError, StackCleanupError) as exc:
                fixes.append(_result(FAIL, "fix:front-proxy", str(exc)))

        if update_policy_files:
            from . import update_policy_env
            from .stack_cleanup import StackCleanupError

            try:
                outcome = update_policy_env.migrate(".", self._maintenance_compose_files())
                if outcome["applied"]:
                    fixes.append(_result(
                        OK, "fix:update-policy",
                        f"{self._policy_readers()} read DLUX_UPDATE_POLICY: "
                        + ", ".join(outcome["files"])
                        + ". Backup: " + outcome["backup_root"],
                    ))
            except (OSError, ValueError, StackCleanupError) as exc:
                fixes.append(_result(FAIL, "fix:update-policy", str(exc)))

        if relay_env_files:
            from .stack_cleanup import StackCleanupError

            try:
                outcome = relay_env.migrate(
                    ".", self._maintenance_compose_files(), environment=self._compose_env_with_secrets()
                )
                if outcome["applied"]:
                    detail = (
                        "web, worker and smtp-relay share the SMTP relay's settings (password excepted): "
                        + ", ".join(outcome["files"])
                    )
                    if outcome["recreated"]:
                        detail += f"; recreated {', '.join(outcome['recreated'])}"
                        self._note_recreated(outcome["recreated"])
                    fixes.append(_result(OK, "fix:relay-env", f"{detail}. Backup: {outcome['backup_root']}"))
            except (OSError, ValueError, StackCleanupError) as exc:
                fixes.append(_result(FAIL, "fix:relay-env", str(exc)))

        if redis_corrupt:
            fixes.append(self._recover_redis_aof())
        if redis_files:
            from .stack_cleanup import StackCleanupError

            try:
                outcome = redis_persistence.migrate(
                    ".", self._maintenance_compose_files(), environment=self._compose_env_with_secrets()
                )
                if outcome["applied"]:
                    fixes.append(_result(
                        OK, "fix:redis-persistence",
                        "Redis keeps RDB snapshots (--appendonly no --save 60 1, its other flags kept): "
                        + ", ".join(outcome["files"]) + f". Backup: {outcome['backup_root']}",
                    ))
            except (OSError, StackCleanupError) as exc:
                fixes.append(_result(FAIL, "fix:redis-persistence", str(exc)))

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
                    f"Retired dlux-updater into Compose init containers on {init_host}. Backup: "
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
                    "Installed composer-executor and composer-agent. Backup: "
                    + (outcome.get("backup_root") or "n/a"),
                ))
            except AgentInstallError as exc:
                fixes.append(_result(FAIL, "fix:install-composer", f"Install failed: {exc}"))
        if legacy:
            from .agent_installer import AgentInstallError, enable_agent

            try:
                outcome = enable_agent(".", compose_file=args.file or "", apply=True)
                fixes.append(
                    _result(
                        OK,
                        "fix:agent-enable",
                        "Migrated composer-updater to the hardened composer-agent + "
                        "composer-executor topology. Backup: " + (outcome.get("backup_root") or "n/a"),
                    )
                )
            except AgentInstallError as exc:
                fixes.append(_result(FAIL, "fix:agent-enable", f"Migration failed: {exc}"))
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
                        "fix:resident-block",
                        "Normalized the Composer resident block: nested 'agent run'/'executor run' "
                        "commands, DAC_OVERRIDE as composer-executor's only added capability, no "
                        "docker-socket-proxy (its container removed), and the registry login passed to "
                        "composer-agent and composer-executor. Backup: "
                        + (outcome.get("backup_root") or "n/a"),
                    )
                )
            except AgentInstallError as exc:
                fixes.append(_result(FAIL, "fix:resident-block", f"Resident block repair failed: {exc}"))
        if needs_updater_migration:
            from .agent_installer import AgentInstallError, migrate_dlux_updater

            try:
                outcome = migrate_dlux_updater(".", compose_file=args.file or "", apply=True)
                fixes.append(
                    _result(
                        OK,
                        "fix:dlux-updater-runtime",
                        "Normalized DjangoLux runtime commands and local tools mounts. Backup: "
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
                            "(one migrator run per start). Backup: "
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
        if grant_access:
            fixes.append(self._fix_app_permissions(grant_access))
        # Last: it builds on what the steps above made of an older layout.
        worker_entry = None
        if needs_worker:
            from .stack_cleanup import StackCleanupError

            try:
                outcome = worker_migration.migrate(
                    ".", self._maintenance_compose_files(), environment=self.build_compose_env()
                )
                if needs_worker == "add":
                    detail = f"Added the worker service (stack schema 5): {', '.join(outcome['files'])}."
                    if getattr(self, "dev_mode", False):
                        detail += " Build and start it with './start.sh -d -b'."
                else:
                    detail = f"Brought the stack to schema 5: {', '.join(outcome['files'])}."
                if outcome["notes"]:
                    detail += " " + " ".join(outcome["notes"])
                worker_entry = _result(OK, "fix:task-worker", f"{detail} Backup: {outcome['backup_root'] or 'n/a'}")
                fixes.append(worker_entry)
            except (OSError, StackCleanupError) as exc:
                fixes.append(_result(FAIL, "fix:task-worker", str(exc)))
        if retire_celery and not any(
            entry["name"] == "fix:task-worker" and entry["level"] == FAIL for entry in fixes
        ):
            from .stack_cleanup import StackCleanupError

            try:
                outcome = worker_migration.retire_celery(
                    ".", self._maintenance_compose_files(), environment=self.build_compose_env()
                )
                if outcome["applied"]:
                    fixes.append(_result(
                        OK, "fix:celery",
                        f"Removed celery ({', '.join(outcome['files'])}); its container is gone and worker runs "
                        f"DjangoLux's tasks. Backup: {outcome['backup_root']}",
                    ))
                    if worker_entry is not None and worker_migration.DEV_OVERRIDE in outcome["files"]:
                        # Removing celery mirrored the development override onto worker.
                        worker_entry["message"] = worker_entry["message"].replace(
                            f" {worker_migration.DEV_WAITS_NOTE}", ""
                        )
                    fixes.extend(self._finish_schema_after_celery())
            except (OSError, StackCleanupError) as exc:
                fixes.append(_result(FAIL, "fix:celery", str(exc)))
        # After every step that restamps: it re-reads the files they left.
        if stamp_repair["files"]:
            from .stack_cleanup import StackCleanupError

            try:
                outcome = stack_layout.repair(
                    ".", self._maintenance_compose_files(), environment=self.build_compose_env()
                )
                if outcome["applied"]:
                    detail = f"Restamped {', '.join(outcome['files'])} to stack schema {outcome['schema']}."
                    if outcome["notes"]:
                        detail += " " + " ".join(outcome["notes"])
                    fixes.append(_result(OK, "fix:stack-schema", f"{detail} Backup: {outcome['backup_root']}"))
                elif outcome["notes"]:
                    fixes.append(_result(WARN, "fix:stack-schema", " ".join(outcome["notes"])))
            except (OSError, StackCleanupError) as exc:
                fixes.append(_result(FAIL, "fix:stack-schema", str(exc)))
        return fixes

    def _finish_schema_after_celery(self) -> List[Dict[str, Any]]:
        """Restamp what the celery removal made ready for schema 5 (``compose.dev.yml``), in the same run.

        The schema step stamps a development override only once it overrides
        worker; removing celery moves that override to worker after the step ran,
        so without this the next ``check --fix`` did the restamp.
        """
        from .stack_cleanup import StackCleanupError

        if not worker_migration.pending(".", self._maintenance_compose_files())["stamp"]:
            return []
        try:
            outcome = worker_migration.migrate(".", self._maintenance_compose_files(), environment=self.build_compose_env())
        except (OSError, StackCleanupError) as exc:
            return [_result(FAIL, "fix:stack-schema", str(exc))]
        if not outcome.get("files"):
            return []
        return [_result(
            OK, "fix:stack-schema",
            f"Brought the stack to schema 5: {', '.join(outcome['files'])}. Backup: {outcome['backup_root'] or 'n/a'}",
        )]

    # --- Redis persistence -------------------------------------------------------

    def _redis_definition(self) -> Optional[Dict[str, Any]]:
        if redis_persistence.SERVICE not in set(self.services):
            return None
        config = self.compose_config_json() or {}
        services = config.get("services") if isinstance(config.get("services"), dict) else {}
        definition = services.get(redis_persistence.SERVICE)
        return definition if isinstance(definition, dict) else None

    def _check_redis_persistence(self) -> Optional[Dict[str, Any]]:
        """Does Redis keep RDB snapshots, which a hard stop cannot leave half-written?"""
        definition = self._redis_definition()
        if definition is None:
            return None
        name = "redis-persistence"
        state = redis_persistence.classify(definition.get("command"))
        if state == redis_persistence.AOF:
            return _result(
                WARN, name,
                "Redis keeps its data in an append-only file (--appendonly yes). A hard power-off or a killed Docker "
                "can leave that file half-written, and Redis then refuses to start: it restarts in a loop and the "
                "app loses its cache and sessions until someone repairs the file.",
                fix="Run 'composer check --fix': it switches Redis to RDB snapshots (--appendonly no --save 60 1, "
                "its other flags kept), archives the Compose file and recreates redis. A snapshot is renamed into "
                "place only when complete; after a crash up to about a minute of cache and sessions is lost, so a "
                "few users sign in again. At a terminal it asks first: answer no to keep the append-only file.",
                manual="edit redis's command in the Compose file by hand: replace --appendonly yes with "
                "--appendonly no --save 60 1 and keep its other flags, then run './start.sh'",
            )
        if state == redis_persistence.CUSTOM:
            return _result(
                OK, name,
                "Redis runs its own configuration file or command; Composer leaves its persistence alone.",
            )
        return _result(
            OK, name,
            "Redis keeps RDB snapshots, not an append-only file: a hard stop leaves the last complete snapshot.",
        )

    def _check_redis_aof(self, report_ok: bool = False) -> Optional[Dict[str, Any]]:
        """Is redis crash-looping on an append-only file it cannot read?"""
        name = "redis-aof-corrupt"
        service = redis_persistence.SERVICE
        if service not in set(self.services):
            return None
        rows = [row for row in self._container_states() if row["service"] == service]
        down = [row for row in rows if self._crash_looping(row) or row["state"] in ("exited", "dead")]
        quiet = _result(OK, name, "Redis is not restarting on a damaged append-only file.") if report_ok else None
        if not down:
            return quiet
        ok, out, err = self.run_docker_compose(["logs", "--no-color", "--tail", "60", service], timeout=20)
        text = f"{out or ''}\n{err or ''}"
        match = redis_persistence.AOF_ERROR.search(text)
        if not match:
            return quiet
        line = next((line for line in text.splitlines() if redis_persistence.AOF_ERROR.search(line)), match.group(0))
        return _result(
            FAIL, name,
            "Redis cannot start: its append-only file is damaged, most likely by a hard stop while it was "
            f"writing, so it restarts in a loop and the app cannot reach its cache or sessions. Redis says: "
            f"{line.split('|', 1)[-1].strip()[:240]}",
            fix="Run 'composer check --fix': it stops redis, moves the append-only files aside into aof-archive/ "
            "on the same volume (kept, not deleted) with a one-off container that has no network, switches Redis "
            "to RDB snapshots and recreates it. Redis starts from its last snapshot, or empty, so users sign in "
            "once more.",
        )

    def _recover_redis_aof(self) -> Dict[str, Any]:
        """Stop redis and move its append-only files aside on its own volume; redis is then recreated."""
        from datetime import datetime, timezone

        name = "fix:redis-aof-corrupt"
        service = redis_persistence.SERVICE
        forced = getattr(self, "_fix_force_recreate", None)
        if forced is None:
            forced = self._fix_force_recreate = set()
        # Switching to snapshots alone ends the loop, since Redis then ignores the
        # damaged files: redis is recreated even if they cannot be moved aside.
        forced.add(service)
        with self._deployment_environment():
            model = self.compose_config_json() or {}
            services = model.get("services") if isinstance(model.get("services"), dict) else {}
            redis = services.get(service) if isinstance(services.get(service), dict) else {}
            self.run_docker_compose(["stop", service], timeout=60)
            volume, target = redis_persistence.data_volume(model, redis.get("command"))
            image = str(redis.get("image") or "")
            if not volume or not image:
                return _result(
                    WARN, name,
                    "redis keeps no named volume at its data directory, so there are no append-only files to move "
                    "aside; it is recreated on snapshots.",
                )
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            try:
                argv = redis_persistence.move_aside_argv(volume, image, stamp, target, redis.get("command"))
            except redis_persistence.RedisPersistenceError as exc:
                return self._redis_still_down(name, str(exc), volume, image, target, redis.get("command"), stamp)
            ok, out, err = self.run_command(argv, timeout=120)
        archive = f"{redis_persistence.ARCHIVE_DIR}/{stamp}/"
        if not ok:
            detail = (str(err or out or "").strip().splitlines() or ["no reason given"])[-1][:300]
            return self._redis_still_down(
                name, f"Could not move the append-only files on {volume} aside ({detail}).",
                volume, image, target, redis.get("command"), stamp,
            )
        moved = redis_persistence.moved_names(out)
        if not moved:
            return _result(
                OK, name,
                f"No append-only file was left on {volume}; redis is recreated on snapshots.",
            )
        return _result(
            OK, name,
            f"Moved {', '.join(moved)} aside into {archive} on the {volume} volume (kept, not deleted). Redis "
            "starts from its last snapshot, or empty, so users sign in once more.",
        )

    @staticmethod
    def _redis_still_down(name, why, volume, image, target, command, stamp) -> Dict[str, Any]:
        """The append-only files stay where Redis reads them: say Redis stays down, and how to move them."""
        tokens = command if isinstance(command, list) else str(command or "").split()
        tokens = [str(token) for token in tokens]
        directory = "appendonlydir"
        for index, token in enumerate(tokens):
            if token == "--appenddirname" and index + 1 < len(tokens):
                directory = tokens[index + 1]
        data = target or "/data"
        archive = f"{data}/{redis_persistence.ARCHIVE_DIR}/{stamp}"
        move = (
            f"docker run --rm --network none -v {volume}:{data} --entrypoint sh {image} -c "
            f"'mkdir -p {archive} && mv \"{data}/{directory}\" {archive}/'"
        )
        entry = _result(
            FAIL, name,
            f"{why} Redis 7 reads the append-only manifest at start even with --appendonly no, so switching to "
            f"snapshots does not end the loop: redis stays down until the files are moved aside. With redis "
            f"stopped, run: {move}; then run './start.sh'.",
        )
        entry["manual"] = f"move the append-only directory aside by hand ({move}), then ./start.sh"
        return entry

    # --- After the repairs: the running stack follows the files ------------------

    def _resident_apply(self, args) -> bool:
        """True for an Operations panel apply, which runs inside the resident pair.

        The agent applies in-process on a stack without an executor
        (``resident_check``); the executor applies in a sibling container it starts
        with ``--resident-apply``. Neither may recreate the pair under itself.
        """
        return bool(getattr(self, "resident_check", False) or getattr(args, "resident_apply", False))

    def _note_recreated(self, services) -> None:
        """A repair recreated these itself; the lifecycle step waits for them but does not repeat it."""
        recreated = getattr(self, "_fix_recreated", None)
        if recreated is None:
            recreated = self._fix_recreated = set()
        recreated.update(services)

    @contextmanager
    def _deployment_environment(self):
        """``(ok, error)``: the deployment's secrets in this process's environment, then restored.

        Containers are built from the interpolated Compose model, so without the
        secrets a recreated one would run on Compose defaults (a placeholder
        SECRET_KEY, default database credentials). Values a question wrote to the
        secrets file in this run are added too: ``./start.sh`` handed over the
        file as it was when the run started.
        """
        saved = dict(os.environ)
        loaded = list(getattr(self, "loaded_secrets", []) or [])
        try:
            try:
                ok, error = self.resolve_secrets()
            except Exception as exc:  # an unreadable file is reported, never fatal here
                ok, error = False, str(exc)
            for key, value in (getattr(self, "_written_secrets", None) or {}).items():
                os.environ[key] = value
            yield ok, error
        finally:
            for key in set(os.environ) - set(saved):
                os.environ.pop(key, None)
            for key, value in saved.items():
                if os.environ.get(key) != value:
                    os.environ[key] = value
            self.loaded_secrets = loaded

    def _lifecycle_baseline(self) -> Optional[Dict[str, Any]]:
        """What the stack runs before the repairs write anything, or None when Compose cannot say."""
        try:
            with self._deployment_environment():
                model = self.compose_config_json()
                containers = self._container_states() if model else []
        except Exception:
            return None
        if not model:
            return None
        return {
            "definitions": stack_lifecycle.definitions(model),
            "images": stack_lifecycle.images(model),
            "project": str(model.get("name") or ""),
            "containers": containers,
        }

    def _finish_fix(self, args, baseline: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Bring the running stack to the files the repairs wrote.

        The resolved Compose model is compared with ``baseline`` one service at a
        time. A service the repairs dropped is stopped and removed (volumes kept).
        A running service whose definition changed, and a service the repairs
        added to a running stack, is recreated with its start-up steps and must
        become healthy and stay up for the settle window. composer-executor and
        composer-agent go last; an Operations panel apply runs inside them and
        leaves them to ``./start.sh``. ``--no-recreate`` changes the files only.
        """
        apply = stack_lifecycle.APPLY_COMMAND
        join = stack_lifecycle.join
        unreadable = (
            "Composer could not read the Compose configuration {when} the repairs, so it removed and recreated "
            f"nothing. Run '{apply}' to apply the changed files."
        )
        if baseline is None:
            return [_result(WARN, "fix:recreate", unreadable.format(when="before"))]
        entries: List[Dict[str, Any]] = []
        resident_services = set(stack_lifecycle.RESIDENT_SERVICES)
        with self._deployment_environment() as (secrets_ok, secrets_error):
            after = self.compose_config_json()
            if not after:
                return [_result(WARN, "fix:recreate", unreadable.format(when="after"))]
            diff = stack_lifecycle.changes(baseline["definitions"], stack_lifecycle.definitions(after))
            if diff["dropped"]:
                entries.append(self._remove_dropped(baseline.get("project") or str(after.get("name") or ""),
                                                    diff["dropped"]))
            rows = baseline.get("containers") or []
            live = {row["service"] for row in rows if row.get("state") in stack_lifecycle.LIVE_STATES}
            ids: Dict[str, str] = {}
            for row in rows:
                if row.get("state") in stack_lifecycle.LIVE_STATES and row.get("id"):
                    ids.setdefault(row["service"], row["id"])
            done = set(getattr(self, "_fix_recreated", None) or ()) & live
            forced = set(getattr(self, "_fix_force_recreate", None) or ())
            changed = {name for name in diff["changed"] if name in live}
            # A development stack builds what it adds first ('./start.sh -d -b').
            added = set(diff["added"]) if live and not getattr(self, "dev_mode", False) else set()
            targets = stack_lifecycle.ordered((changed | added | forced) - done)
            if not targets and not done:
                if (diff["changed"] or diff["added"]) and not live:
                    entries.append(_result(
                        OK, "fix:recreate",
                        f"Nothing of this stack is running, so nothing was recreated: '{apply}' starts it on the "
                        "changed files.",
                    ))
                return entries
            pending: List[str] = []
            if getattr(args, "no_recreate", False):
                left = [name for name in targets if name not in forced]
                targets = [name for name in targets if name in forced]
                if left:
                    entries.append(_result(
                        WARN, "fix:recreate",
                        f"--no-recreate: {join(left)} keep running the definition they were created from. Run "
                        f"'{apply}' when you are ready: it recreates them on the changed files.",
                    ))
                    pending += left
            held = self._image_drift(
                [name for name in targets if name not in resident_services and name not in forced], baseline, after
            )
            if held:
                entries.append(_result(
                    WARN, "fix:recreate",
                    "Not recreated, so that check --fix does not deploy another image on the side: "
                    + "; ".join(f"{name}: {why}" for name, why in sorted(held.items()))
                    + f". Run '{apply}' to deploy it on purpose.",
                ))
                pending += sorted(held)
                targets = [name for name in targets if name not in held]
            resident = [name for name in targets if name in resident_services]
            app = [name for name in targets if name not in resident_services]
            if (app or resident) and not secrets_ok:
                reason = (str(secrets_error or "").strip().splitlines() or ["they could not be read"])[0]
                entries.append(_result(
                    FAIL, "fix:recreate",
                    f"Refusing to recreate {join(app + resident)} without the deployment's secrets ({reason}): a "
                    f"container built without them would run on Compose defaults. Run '{apply}' once the secrets "
                    "file can be read.",
                ))
                pending += app + resident
                app, resident = [], []
            if app or done:
                healthy, entry = self._recreate_and_wait(app, sorted(done), "fix:recreate")
                entries.append(entry)
                if not healthy and resident:
                    pending += resident
                    resident = []
            if resident and self._resident_apply(args):
                entries.append(_result(
                    WARN, "fix:recreate-resident",
                    f"{join(resident)} changed, and an Operations panel apply runs inside them, so it does not "
                    f"recreate them. Run '{apply}' on the server: it recreates them on the changed files.",
                ))
                pending += resident
            elif resident:
                entries.append(self._recreate_and_wait(resident, [], "fix:recreate-resident")[1])
            leftover = {name: ids[name] for name in pending if name in ids}
            if leftover:
                try:
                    stack_lifecycle.record_pending(Path("."), leftover)
                except OSError:
                    pass
        return entries

    def _recreate_and_wait(self, services: List[str], already: List[str], name: str) -> Tuple[bool, Dict[str, Any]]:
        """``(healthy, result)``: recreate ``services``, then wait for them and ``already``."""
        from .health_monitor import settle_seconds

        join = stack_lifecycle.join
        waited = stack_lifecycle.ordered(set(services) | set(already))
        logs = f"./start.sh logs {' '.join(waited)}"
        if services:
            ok, out, err = self.recreate_containers(list(services))
            finish = getattr(self, "finish_progress_line", None)
            if finish is not None:
                finish()
            if not ok:
                detail = (str(err or out or "").strip().splitlines() or ["Compose gave no reason"])[-1][:500]
                return False, _result(
                    FAIL, name,
                    f"Could not recreate {join(services)}: {detail}. Read '{logs}', fix the cause, then run "
                    f"'{stack_lifecycle.APPLY_COMMAND}'.",
                )
        healthy, detail = self._wait_healthy(waited)
        if not healthy:
            reason = str(detail or "").strip().split("\n\n")[0][:1200]
            return False, _result(
                FAIL, name,
                f"Recreated {join(waited)}, but they did not stay healthy: {reason} Read '{logs}', fix the cause, "
                f"then run '{stack_lifecycle.APPLY_COMMAND}'.",
            )
        message = f"Recreated {join(services)} with their start-up steps" if services else ""
        if already:
            by_repair = f"{join(already)} recreated or restarted by {'its' if len(already) == 1 else 'their'} own repair"
            message = f"{message} ({by_repair})" if message else by_repair[0].upper() + by_repair[1:]
        return True, _result(OK, name, f"{message}; healthy and up {settle_seconds():g} s.")

    def _wait_healthy(self, services: List[str]) -> Tuple[bool, str]:
        """``monitor_health()`` scoped to ``services``, without the deploy screen."""
        from .constants import SERVICE_NOT_SEEN

        saved_services = list(getattr(self, "services", []) or [])
        saved_state = dict(getattr(self, "service_state", {}) or {})
        own_render = self.__dict__.get("render")
        self.services = list(services)
        self.service_state = {service: SERVICE_NOT_SEEN for service in services}
        self.render = lambda *args, **kwargs: None
        try:
            return self.monitor_health()
        finally:
            if own_render is None:
                self.__dict__.pop("render", None)
            else:
                self.render = own_render
            self.services = saved_services
            self.service_state = saved_state
            finish = getattr(self, "finish_progress_line", None)
            if finish is not None:
                finish()

    def _image_drift(self, services: List[str], baseline: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, str]:
        """``{service: why}`` for services whose image reference now names another image than they run.

        Recreating one would deploy that image (pulled or built, never deployed),
        which ``check --fix`` must not do on the side. A repair that changes the
        reference itself (a Composer pin) is meant to, so only an unchanged
        reference is compared. A service the repairs add (``worker``) has no
        container yet: it is compared through a running service on the same
        reference, so it is not started on an image the stack does not run.
        """
        before_images = baseline.get("images") or {}
        after_images = stack_lifecycle.images(after)
        ids = {
            row["service"]: row["id"] for row in baseline.get("containers") or []
            if row.get("id") and row.get("state") in stack_lifecycle.LIVE_STATES
        }
        unchanged = sorted(
            name for name in ids if after_images.get(name) and after_images.get(name) == before_images.get(name)
        )
        probes: Dict[str, str] = {}
        for name in services:
            reference = after_images.get(name)
            if name in ids:
                if name in unchanged:
                    probes[name] = name
            elif reference and name not in before_images:
                source = next((other for other in unchanged if after_images[other] == reference), None)
                if source:
                    probes[name] = source
        if not probes:
            return {}
        containers = sorted({ids[source] for source in probes.values()})
        ok, out, _err = self.run_command(["docker", "inspect", "--format", "{{.Id}} {{.Image}}", *containers], timeout=20)
        if not ok:
            return {}
        running = dict(line.split(None, 1) for line in out.splitlines() if len(line.split()) == 2)
        local: Dict[str, str] = {}
        held: Dict[str, str] = {}
        for name, source in probes.items():
            image = next(
                (value.strip() for cid, value in running.items()
                 if cid.startswith(ids[source]) or ids[source].startswith(cid)),
                "",
            )
            reference = after_images[name]
            if reference not in local:
                found, text, _ = self.run_command(["docker", "image", "inspect", "--format", "{{.Id}}", reference], timeout=15)
                local[reference] = text.strip() if found else ""
            if image and local[reference] and local[reference] != image:
                runs = "the one it runs" if source == name else f"the one {source} runs"
                held[name] = f"{reference} now names another image than {runs}"
        return held

    def _remove_dropped(self, project: str, services: List[str]) -> Dict[str, Any]:
        """Stop and remove the containers of services the Compose files no longer define.

        Compose cannot name them any more, so they are found by the labels it gave
        them. ``docker rm`` without ``-v``: volumes are kept.
        """
        join = stack_lifecycle.join
        if not project:
            return _result(
                WARN, "fix:remove-services",
                f"{join(services)} left the Compose files, but the project name could not be read, so their "
                "containers were not looked for.",
            )
        found: Dict[str, List[str]] = {}
        failed: List[str] = []
        for service in services:
            ok, out, err = self.run_command(
                ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}",
                 "--filter", f"label=com.docker.compose.service={service}"],
                timeout=20,
            )
            if not ok:
                failed.append(f"{service} ({(err or out or 'docker ps failed').strip()[:200]})")
            elif out.split():
                found[service] = out.split()
        removed = []
        for service, containers in found.items():
            stopped, _out, stop_err = self.run_command(["docker", "stop", *containers], timeout=120)
            deleted, _out, rm_err = self.run_command(["docker", "rm", *containers], timeout=60)
            if stopped and deleted:
                removed.append(service)
            else:
                failed.append(f"{service} ({(rm_err or stop_err or 'docker rm failed').strip()[:200]})")
        if failed:
            return _result(
                FAIL, "fix:remove-services",
                f"{join(services)} left the Compose files, but Composer could not remove the containers of "
                f"{'; '.join(failed)}. They keep running outside the Compose files until they are removed; "
                "volumes are kept.",
            )
        if removed:
            return _result(
                OK, "fix:remove-services",
                f"Stopped and removed the containers of {join(removed)}: their definitions left the Compose "
                "files. Volumes are kept.",
            )
        return _result(
            OK, "fix:remove-services",
            f"{join(services)} left the Compose files and no container of {'it' if len(services) == 1 else 'them'} "
            "is left. Volumes are kept.",
        )

    def _check_pending_recreate(self) -> Optional[Dict[str, Any]]:
        """Services ``check --fix`` left on their old definition, until their container is replaced."""
        recorded = stack_lifecycle.read_pending(Path("."))
        if not recorded:
            return None
        current: Dict[str, set] = {}
        for row in self._container_states():
            if row.get("id"):
                current.setdefault(row["service"], set()).add(row["id"])
        pending = stack_lifecycle.still_pending(recorded, current)
        if not pending:
            return None
        join = stack_lifecycle.join
        since = min(recorded[name]["since"] for name in pending)
        one = len(pending) == 1
        message = (
            f"{join(pending)} still run{'s' if one else ''} the Compose definition from before 'check --fix' "
            f"changed it ({since})"
        )
        if set(pending) & set(stack_lifecycle.RESIDENT_SERVICES):
            message += (
                "; an Operations panel apply runs inside composer-agent and composer-executor, so it cannot "
                "recreate them"
            )
        return _result(
            WARN, "pending-recreate", message + ".",
            fix=f"Run '{stack_lifecycle.APPLY_COMMAND}' on the server: it recreates "
            f"{'it' if one else 'them'} on the changed files.",
        )

    # Services each repair changes, for the confirmation and the Operations
    # preview. What is recreated goes by the Compose model, not by this list.
    _EDGE_PROXIES = ("caddy", "nginx", "proxy")
    # The x-environment mergers when the Compose model cannot say (see below).
    _SHARED_ENVIRONMENT = ("web", "worker", "celery", "smtp-relay")
    _SHARED_PROBE_KEY = "COMPOSER_SHARED_ENVIRONMENT_PROBE"

    def _shared_environment_services(self) -> set:
        """The services that merge ``x-environment``, by the same model diff ``_finish_fix`` recreates by.

        A scratch copy of the file that defines the block gains one probe line,
        and the resolved models with and without it are compared one service at a
        time: each service whose definition changes is one a repair to the block
        recreates. Older stacks also merge it into db, which a fixed list missed.
        Falls back to ``_SHARED_ENVIRONMENT`` when Compose cannot say.
        """
        import tempfile

        fallback = set(self._SHARED_ENVIRONMENT)
        files = [str(name) for name in (getattr(self, "active_compose_files", None) or [])]
        header = re.compile(r"(?m)^x-environment:[^\n]*\n")
        for index, name in enumerate(files):
            try:
                contents = Path(name).read_text(encoding="utf-8")
            except OSError:
                continue
            match = header.search(contents)
            if match:
                break
        else:
            return fallback
        end = re.compile(r"(?m)^\S").search(contents, match.end())
        entry = re.compile(r"(?m)^([ \t]+)[A-Za-z_][A-Za-z0-9_]*:").search(
            contents, match.end(), end.start() if end else len(contents))
        if not entry:
            return fallback
        probed = (contents[:entry.start()] + f'{entry.group(1)}{self._SHARED_PROBE_KEY}: "1"\n'
                  + contents[entry.start():])
        project_dir = str(Path(files[0]).resolve().parent)
        saved_files = list(files)
        paused = getattr(self, "_runtime_override_paused", False)
        try:
            with tempfile.TemporaryDirectory() as scratch, self._deployment_environment():
                probe = Path(scratch) / Path(files[index]).name
                probe.write_text(probed, encoding="utf-8")
                # Without the runtime override: rewriting it for the scratch file list would touch the project.
                self._runtime_override_paused = True
                models = []
                for active in (saved_files, saved_files[:index] + [str(probe)] + saved_files[index + 1:]):
                    self.active_compose_files = active
                    ok, out, _err = self.run_docker_compose(
                        ["--project-directory", project_dir, "config", "--format", "json"], timeout=20)
                    models.append(json.loads(out) if ok else None)
        except (OSError, ValueError):
            return fallback
        finally:
            self.active_compose_files = saved_files
            self._runtime_override_paused = paused
        if not all(isinstance(model, dict) for model in models):
            return fallback
        before, after = (stack_lifecycle.definitions(model) for model in models)
        changed = set(stack_lifecycle.changes(before, after)["changed"])
        return changed or fallback

    def _lifecycle_expectation(self, plan: Dict[str, Any]) -> Dict[str, List[str]]:
        """``{"remove", "recreate", "resident"}``: the services a planned run removes and recreates."""
        get = plan.get
        services = set(getattr(self, "services", []) or [])
        choices = getattr(self, "_operator_choices", None) or {}
        resident_pair = {"composer-agent", "composer-executor"}
        remove = set(get("obsolete") or [])
        if get("retire_celery"):
            remove.add("celery")
        if get("legacy"):
            remove.add("composer-updater")
        if get("legacy") or get("needs_hardening") or get("needs_agent_block_migration"):
            remove.add("docker-socket-proxy")
        if get("needs_init_containers"):
            remove.add("dlux-updater")
        remove &= services
        recreate: set = set()
        if any((
            get("maintenance_legacy"), get("media_exposed"), get("relay_env_files"),
            (get("stamp_repair") or {}).get("files"), get("update_policy_files"),
            "DLUX_UPDATE_POLICY" in choices, get("needs_worker"),
        )):
            # Services that merge x-environment, where the schema stamp and shared settings live.
            shared = self._shared_environment_services()
        else:
            shared = set()
        if get("maintenance_legacy") or get("media_exposed"):
            recreate |= set(self._EDGE_PROXIES) | shared
        if get("front_proxy_files") or "DLUX_FRONT_PROXY" in choices:
            recreate.add("caddy")
        if get("relay_env_files") or (get("stamp_repair") or {}).get("files"):
            recreate |= shared
        if get("update_policy_files") or "DLUX_UPDATE_POLICY" in choices:
            recreate |= shared | resident_pair
        if get("needs_init_containers"):
            recreate |= {get("init_host") or "celery", "web"}
        if get("needs_dev_override_migration"):
            recreate |= {"celery", "worker"}
        if get("needs_worker"):
            recreate |= shared
        if get("needs_worker") == "add" or get("retire_celery"):
            # Their restart list (COMPOSER_AGENT_RESTART_SERVICES) gains worker or loses celery.
            recreate |= resident_pair
        if get("needs_updater_migration"):
            recreate |= {"dlux-updater", "smtp-relay", "web", "celery", "worker"}
        if get("needs_post_start_migration"):
            recreate.add("web")
        if get("needs_restart_labels"):
            recreate |= services
        if get("legacy") or get("needs_install") or get("needs_hardening") or get("needs_agent_block_migration"):
            recreate |= resident_pair
        if get("redis_files") or get("redis_corrupt"):
            recreate.add("redis")
        known = set(services)
        if get("needs_worker") == "add":
            known.add("worker")
        if get("legacy") or get("needs_install"):
            known |= resident_pair
        recreate = (recreate & known) - remove
        return {
            "remove": sorted(remove),
            "recreate": sorted(recreate - resident_pair),
            "resident": [name for name in stack_lifecycle.RESIDENT_SERVICES if name in recreate],
        }

    def _lifecycle_consequences(self, plan: Dict[str, Any], args) -> List[str]:
        """The confirmation's lines for what the run does to the running stack."""
        from .health_monitor import settle_seconds

        join = stack_lifecycle.join
        apply = stack_lifecycle.APPLY_COMMAND
        life = self._lifecycle_expectation(plan)
        lines = []
        if life["remove"]:
            lines.append(
                f"Stop and remove the containers of {join(life['remove'])}: their definitions leave the Compose "
                "files. Volumes are kept."
            )
        if getattr(args, "no_recreate", False):
            lines.append(
                f"--no-recreate: running services keep the definition they were created from until you run "
                f"'{apply}', which recreates the changed ones."
            )
            return lines
        if not (life["recreate"] or life["resident"]):
            return lines
        expected = f" (expected: {join(life['recreate'])})" if life["recreate"] else ""
        lines.append(
            f"Recreate each running service whose Compose definition this run changes{expected}, with its start-up "
            f"steps, and wait until it is healthy and has stayed up {settle_seconds():g} s. A service the repairs "
            "add to a running stack is started, unless its image is not the one the stack runs: the next deploy "
            "starts it."
        )
        if life["resident"]:
            if self._resident_apply(args):
                lines.append(
                    "composer-agent and composer-executor change too; this apply runs inside them, so they are left "
                    f"for '{apply}' on the server."
                )
            else:
                lines.append("Recreate composer-executor and then composer-agent last.")
        lines.append(
            "Restart window: each recreated service is down for the seconds Docker needs to replace its container; "
            "while web is down the proxy shows the maintenance page."
        )
        return lines

    def planned_lifecycle(self, args, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """The Operations preview's entries for what an apply does to the running stack."""
        join = stack_lifecycle.join
        try:
            plan = self._fix_plan(args, results)
        except Exception as exc:  # a planning step that refuses is not a crash
            return [{"name": "recreate-services", "files": [], "diff": "",
                     "note": f"Could not plan which services an apply removes or recreates: {exc}"}]
        if plan.get("refused") or not plan.get("pending"):
            return []
        life = self._lifecycle_expectation(plan)
        entries = []
        if life["remove"]:
            entries.append({
                "name": "remove-services", "files": [], "diff": "",
                "note": f"Stops and removes the containers of {join(life['remove'])}: their definitions leave the "
                "Compose files. Volumes are kept.",
            })
        if not (life["recreate"] or life["resident"]):
            return entries
        expected = f" (expected: {join(life['recreate'])})" if life["recreate"] else ""
        note = (
            f"Recreates each running service whose Compose definition the apply changes{expected}, with its "
            "start-up steps, and waits until it is healthy; each is down for a few seconds meanwhile."
        )
        if life["resident"]:
            note += (
                " composer-agent and composer-executor change too; the apply runs inside them, so run "
                f"'{stack_lifecycle.APPLY_COMMAND}' on the server afterwards to recreate them."
            )
        entries.append({"name": "recreate-services", "files": [], "diff": "", "note": note})
        return entries

    def _configured_dlux_version(self) -> Tuple[str, Optional[Tuple[int, int, int]]]:
        """``(image, (major, minor, patch) or None)``: the DjangoLux baked into the app
        image the Compose files name, read only when Docker already has that image.

        Its baked-version label, else ``python -m dlux --version`` in a throwaway
        container without network. Nothing is pulled.
        """
        from .agent_installer import parse_dlux_version
        from .app_permissions import app_image
        from .downgrade_guard import label_versions

        image = app_image(self.compose_config_json() or {})
        if not image:
            return "", None
        labels = self.image_labels(image)
        if labels is None:
            return image, None
        baked = label_versions(labels).get("dlux")
        if baked:
            return image, parse_dlux_version(baked)
        ok, out, _err = self.run_command(
            ["docker", "run", "--rm", "--pull", "never", "--network", "none", "--entrypoint", "python",
             image, "-m", "dlux", "--version"],
            timeout=60,
        )
        return image, (parse_dlux_version(out) if ok else None)

    def _worker_gate(self) -> Tuple[bool, str]:
        """``(ready, why not)``: may ``worker`` be added to this stack now?

        Only once the stack's DjangoLux has ``dlux_worker``, or ``worker`` would
        crash-loop: the image the Compose files name when Docker has it (pulled
        or built, not deployed yet), the running release, or in development
        (``-d``, built from the project source) the version the project requires.
        """
        if getattr(self, "_worker_gate_cache", None) is not None:
            return self._worker_gate_cache
        floor = worker_migration.WORKER_FLOOR
        minimum = ".".join(map(str, floor))
        running = self._running_dlux_version()
        self._worker_running = running
        self._worker_image, configured = self._configured_dlux_version()
        self._worker_configured = configured
        declared = worker_migration.declared_dlux(Path(".")) if getattr(self, "dev_mode", False) else None
        if any(version is not None and tuple(version) >= floor for version in (running, configured, declared)):
            gate = (True, "")
        else:
            got = ".".join(map(str, running)) if running else "an unknown DjangoLux"
            gate = (False, (
                f"The stack runs {'DjangoLux ' + got if running else got}, which has no dlux_worker: "
                f"'worker' is added once the project image is on DjangoLux {minimum} or later and present "
                "here ('./start.sh pull', or a rebuild), or in development once the project requires it."
            ))
        self._worker_gate_cache = gate
        return gate

    def _worker_fix_plan(self, needs_init_containers: bool, init_containers_blocked: str) -> Tuple[str, str]:
        """``(step, blocked)`` for ``check --fix``: step is ``"add"`` (worker),
        ``"finish"`` (development override or stamps) or ``""``. Quiet on a stack
        whose DjangoLux has no worker yet: nothing is wrong with it."""
        pending = worker_migration.pending(".", self._maintenance_compose_files())
        services = set(self.services)
        if not pending["compose"]:
            # The base file has worker: what is left is the development override
            # or the stamps, neither of which needs the version gate.
            finish = "worker" in services and (pending["dev"] or pending["stamp"])
            return ("finish" if finish else ""), ""
        ready, _why = self._worker_gate()
        if not ready:
            return "", ""
        if "dlux-updater" in services:
            if needs_init_containers:
                return "add", ""
            return "", (
                "'worker' is added once 'dlux-updater' is retired into init containers"
                + (f": {init_containers_blocked}" if init_containers_blocked else ".")
            )
        try:
            preview = worker_migration.preview(".", self._maintenance_compose_files())
        except Exception as exc:
            return "", f"Cannot add the worker service: {exc}"
        return ("add" if preview["files"] else ""), ""

    def _check_task_worker(self) -> Optional[Dict[str, Any]]:
        """Does DjangoLux have its ``worker`` service (stack schema 5)?"""
        services = set(self.services)
        if "worker" in services:
            running = self._running_dlux_version()
            if running is not None and tuple(running) < worker_migration.WORKER_FLOOR:
                # image-floor reports it: this DjangoLux has no dlux_worker.
                return None
            return _result(OK, "task-worker", "DjangoLux's tasks run in the worker service (stack schema 5).")
        if "celery" not in services:
            return None
        ready, why = self._worker_gate()
        running = self._worker_running
        if ready and running is not None and tuple(running) >= worker_migration.WORKER_FLOOR:
            return _result(
                FAIL,
                "task-worker",
                f"DjangoLux {'.'.join(map(str, running))} runs its tasks in a 'worker' service, which this "
                "stack does not have, so they are queued and never run: scheduled backups and jobs stop.",
                fix="Run 'composer check --fix' to add it (stack schema 5), then deploy.",
            )
        configured = getattr(self, "_worker_configured", None)
        if ready and configured is not None and tuple(configured) >= worker_migration.WORKER_FLOOR:
            return _result(
                OK,
                "task-worker",
                f"No 'worker' service yet; {self._worker_image}, the image this stack is set to run, has "
                f"DjangoLux {'.'.join(map(str, configured))}, which runs its tasks there: run "
                "'composer check --fix' (stack schema 5) before deploying it.",
            )
        if ready:
            return _result(
                OK,
                "task-worker",
                "No 'worker' service yet; the project requires a DjangoLux that runs its tasks there, so "
                "'composer check --fix' adds it (stack schema 5). Rebuild with './start.sh -d -b' afterwards.",
            )
        return _result(OK, "task-worker", f"celery runs DjangoLux's tasks (stack schema 4 layout). {why}")

    # A container restarted this often, and started this recently, is crash-looping
    # even when `ps` catches it between two restarts.
    CRASH_LOOP_RESTARTS = 3
    CRASH_LOOP_WINDOW_SECONDS = 120

    def _container_states(self) -> List[Dict[str, Any]]:
        """``[{service, name, state, health, restarts, started_at}]`` for the stack's containers.

        Empty when Docker cannot say: the check then reports nothing rather than guessing.
        """
        ok, out, _err = self.run_docker_compose(["ps", "--all", "--format", "json"], timeout=20)
        if not ok or not out.strip():
            return []
        try:
            parsed = json.loads(out)
            rows = parsed if isinstance(parsed, list) else [parsed]
        except ValueError:
            rows = []
            for line in out.splitlines():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        containers = [
            {
                "service": str(row.get("Service") or ""),
                "name": str(row.get("Name") or row.get("ID") or ""),
                "id": str(row.get("ID") or ""),
                "state": str(row.get("State") or "").lower(),
                "health": str(row.get("Health") or "").lower(),
                "exit_code": row.get("ExitCode"),
                "restarts": 0,
                "started_at": "",
            }
            for row in rows if isinstance(row, dict) and row.get("Service")
        ]
        ids = [entry["id"] or entry["name"] for entry in containers]
        if ids:
            ok, out, _err = self.run_command(
                ["docker", "inspect", "--format",
                 "{{.Id}}|{{.Name}}|{{.RestartCount}}|{{.State.Restarting}}|{{.State.StartedAt}}", *ids],
                timeout=20,
            )
            details = {}
            for line in (out.splitlines() if ok else []):
                parts = line.split("|")
                if len(parts) == 5:
                    details[parts[0]] = parts
                    details[parts[1].lstrip("/")] = parts
            for entry in containers:
                found = next(
                    (details[key] for key in details if entry["id"] and key.startswith(entry["id"])),
                    details.get(entry["name"]),
                )
                if not found:
                    continue
                try:
                    entry["restarts"] = int(found[2])
                except ValueError:
                    pass
                if found[3] == "true":
                    entry["state"] = "restarting"
                entry["started_at"] = found[4]
        return containers

    @classmethod
    def _crash_looping(cls, entry: Dict[str, Any], now=None) -> bool:
        if entry["state"] == "restarting":
            return True
        if entry["restarts"] < cls.CRASH_LOOP_RESTARTS or entry["state"] != "running":
            return False
        from datetime import datetime, timezone

        try:
            started = datetime.fromisoformat(entry["started_at"].replace("Z", "+00:00")[:32])
        except ValueError:
            return False
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        return (now - started).total_seconds() < cls.CRASH_LOOP_WINDOW_SECONDS

    _ADDRESS = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?(?![\w.])")

    def _last_error_line(self, service: str) -> str:
        """The last error line of the service's log, container addresses replaced by "<address>"."""
        ok, out, err = self.run_docker_compose(["logs", "--no-color", "--tail", "30", service], timeout=20)
        lines = [
            self._ADDRESS.sub("<address>", line.split("|", 1)[-1].strip())
            for line in (out or err or "").splitlines() if line.strip()
        ]
        for line in reversed(lines):
            if "error" in line.lower() or "exception" in line.lower():
                return line[:200]
        return lines[-1][:200] if lines else ""

    def _check_service_states(self) -> Optional[Dict[str, Any]]:
        """Are any of the stack's containers crash-looping, restarting or unhealthy?"""
        containers = self._container_states()
        if not containers:
            return None
        looping = sorted({entry["service"] for entry in containers if self._crash_looping(entry)})
        unhealthy = sorted({
            entry["service"] for entry in containers
            if entry["health"] == "unhealthy" and entry["service"] not in looping
        })
        stopped, residents = self._stopped_services(containers, looping)
        if not looping and not unhealthy and not stopped and not residents:
            running = sum(1 for entry in containers if entry["state"] == "running")
            return _result(OK, "services", f"No service is crash-looping or unhealthy ({running} running).")
        parts = []
        for service in looping:
            restarts = max(entry["restarts"] for entry in containers if entry["service"] == service)
            error = self._last_error_line(service)
            parts.append(
                f"{service} keeps restarting ({restarts} restart(s))"
                + (f"; last error: {error}" if error else "")
            )
        parts += [f"{service} is unhealthy" for service in unhealthy]
        parts += stopped + residents
        celery = self._celery_unusable() if "celery" in looping else ""
        if celery:
            fix = f"{celery} Run 'composer check --fix' to remove celery (worker runs DjangoLux's tasks)."
        elif looping or unhealthy or stopped:
            fix = "Read the service's log ('./start.sh logs <service>'), fix the cause, then redeploy."
        else:
            fix = "Run './start.sh' to start them again."
        return _result(FAIL if looping or stopped else WARN, "services", "; ".join(parts) + ".", fix=fix)

    #: App services the site and DjangoLux's tasks need while the stack runs.
    _REQUIRED_RUNNING = {
        "web": "the site does not answer",
        "worker": "DjangoLux's queued tasks and schedules (backups) never run",
    }
    _RESIDENT_ROLES = {
        "composer-executor": "updates, rollbacks and the Operations panel's apply cannot run",
        "composer-agent": "no update checks, maintenance page or Operations panel",
    }

    def _stopped_services(self, containers: List[Dict[str, Any]], looping) -> Tuple[List[str], List[str]]:
        """``(app, resident)`` findings for services that do not run while the rest of the stack does."""
        if not any(entry["state"] == "running" for entry in containers):
            return [], []
        by_service: Dict[str, List[Dict[str, Any]]] = {}
        for entry in containers:
            by_service.setdefault(entry["service"], []).append(entry)

        def describe(service: str, short: bool = False) -> str:
            rows = by_service.get(service) or []
            if not rows:
                return "no container" if short else "has no container"
            if all(row["state"] == "created" for row in rows):
                return "created, never started" if short else "never started"
            code = next((row.get("exit_code") for row in rows if row.get("exit_code") not in (None, "")), None)
            if short:
                return "stopped" + (f", exit {code}" if code is not None else "")
            return "has stopped" + (f" (exit {code})" if code is not None else "")

        def down(service: str) -> bool:
            rows = by_service.get(service) or []
            return service not in looping and not any(row["state"] in ("running", "restarting") for row in rows)

        services = set(self.services)
        app = [
            f"{service} {describe(service)}, so {why}"
            for service, why in self._REQUIRED_RUNNING.items() if service in services and down(service)
        ]
        resident = [
            f"{service} is not running ({describe(service, short=True)}): {why}"
            for service, why in self._RESIDENT_ROLES.items() if service in services and down(service)
        ]
        return app, resident

    # Does the image celery runs have Celery and the app celery's command names?
    # Only module lookups: nothing of the project is imported but the package the
    # app lives in.
    CELERY_PROBE = """import importlib.util,json,sys
app=sys.argv[1] if len(sys.argv)>1 else ''
out={'celery':importlib.util.find_spec('celery') is not None,'app':None}
if out['celery'] and app:
 try: out['app']=importlib.util.find_spec(app.split(':')[0]) is not None
 except Exception: out['app']=False
print(json.dumps(out))
"""

    @staticmethod
    def _celery_app(command) -> str:
        tokens = command if isinstance(command, list) else str(command or "").split()
        tokens = [str(token) for token in tokens]
        for index, token in enumerate(tokens):
            if token in ("-A", "--app") and index + 1 < len(tokens):
                return tokens[index + 1]
            if token.startswith("--app="):
                return token.split("=", 1)[1]
            if token.startswith("-A") and len(token) > 2:
                return token[2:]
        return ""

    def _celery_unusable(self) -> str:
        """Why celery cannot run in its image (no Celery, or no such app), or ``""``.

        Asked once per run, and only of a schema 5 stack (worker and celery), or
        one ``check --fix`` is bringing to schema 5. Runs celery's image alone, so
        an image Docker has not pulled is simply not probed.
        """
        cached = getattr(self, "_celery_probe_cache", None)
        if cached is not None:
            return cached
        reason = ""
        config = self.compose_config_json() or {}
        services = config.get("services") if isinstance(config, dict) else None
        celery = services.get("celery") if isinstance(services, dict) else None
        image = celery.get("image") if isinstance(celery, dict) else None
        if isinstance(image, str) and image:
            app = self._celery_app(celery.get("command"))
            # The bare image, not `compose run`: no init containers, mounts,
            # environment or network, so the probe cannot change anything.
            ok, out, _err = self.run_command(
                ["docker", "run", "--rm", "--pull", "never", "--network", "none", "--entrypoint", "python", image,
                 "-c", self.CELERY_PROBE, *([app] if app else [])],
                timeout=90,
            )
            try:
                probe = json.loads(out.strip().splitlines()[-1]) if ok and out.strip() else {}
            except ValueError:
                probe = {}
            if probe.get("celery") is False:
                reason = "The project image has no Celery, so celery cannot start."
            elif probe.get("app") is False:
                reason = f"The project image has no Celery app '{app}', so celery cannot start."
        self._celery_probe_cache = reason
        return reason

    def _check_celery(self) -> Optional[Dict[str, Any]]:
        """On a schema 5 stack, can celery still run the project's own tasks?"""
        if not {"worker", "celery"} <= set(self.services):
            return None
        reason = self._celery_unusable()
        if not reason:
            return None
        return _result(
            WARN,
            "celery",
            f"{reason} DjangoLux runs its tasks in worker; celery only ran the project's own, and this "
            "image has none, so the container restarts for good.",
            fix="Run 'composer check --fix' to remove celery from the Compose files (archived first).",
        )

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
