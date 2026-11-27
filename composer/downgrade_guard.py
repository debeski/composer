"""Refuse to deploy an app image older than the release the database was migrated by.

Migrations only run forwards. Once a newer image's migrator has changed the
schema, the older image's code may fail on it or write rows the newer schema
reads wrongly, and nothing undoes that. The break-it run (2026-11-25, finding
#11) set ``WEB_IMAGE`` back from Decrees 2.2.1 (DjangoLux 1.16.3) to 2.1.26
(DjangoLux 1.15.1) and a plain ``./start.sh`` deployed it over the migrated
database.

The guard runs before every ``up`` and compares the target app image with the
newest image this deployment has deployed:

- the images of the app containers that have started, which are what last ran
  the migrator (even when they failed their health check afterwards); one that
  was only created ran nothing;
- the record Composer keeps after each ``up``, which survives a ``down`` that
  removed those containers. It is written twice: to ``RECORD`` in the deploy
  root, and to ``RUNTIME_RECORD`` on the DjangoLux runtime volume, so a deploy
  root Composer cannot write still keeps one. The newer of the two counts.
  Each names the stack's Postgres data volume and its creation time; a record
  whose volume is gone or was recreated (``down -v``, a restore onto a new
  volume) describes another database, so it does not refuse, and the next ``up``
  replaces it. A record without that field counts as before.

Versions come from image labels DjangoLux project images already carry: the
baked DjangoLux version (``org.<project>.dlux_baked_version``) and the app
version in the project release manifest (``org.dlux.project.release-manifest``,
falling back to ``org.opencontainers.image.version``). So the record's ``dlux``
is the newest *baked* DjangoLux deployed, not the release that runs: an inline
update activates a newer release on the runtime volume without a deploy, and the
record keeps the image's version. That is enough. DjangoLux's supervisor serves
a volume release only while it is newer than the baked package, so an image
baked older than the active release still runs the active release, and the
floor check below refuses an image that release cannot run on. A dimension either side
does not label is not compared; a target that labels neither while the
recorded release did is refused, since nothing shows it is not older.

The baked DjangoLux version is also checked against the image floor of the
release the runtime volume has active (``requires.baked_image`` in
``releases/<active>/dlux/release-manifest.json``, legacy ``image_baseline``). A
DjangoLux 1.17+ image below that floor serves its baked package instead; an older
image runs its own supervisor, which never checks, so this guard is the only
defence there. It is read in a throwaway Composer container with the volume
mounted read-only, and only when the volume already exists.

``--force`` deploys anyway and makes the forced release the new record.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from . import versions
from .service_selection import scoped_service_list

RECORD = Path(".xclude") / "composer-deploy" / "deployed.json"
MANIFEST_LABEL = "org.dlux.project.release-manifest"
OCI_VERSION_LABEL = "org.opencontainers.image.version"
BAKED_LABEL_SUFFIX = ".dlux_baked_version"
DIMENSIONS = (("app", "app"), ("dlux", "DjangoLux"))
#: What was deployed, beside which release it was (``write_record``).
IDENTITY_KEYS = ("image", "repo_digest", "image_id")
RUNTIME_ROOT = "/opt/dlux-runtime"
RUNTIME_RECORD = "state/composer-deployed.json"
#: Where the database the record describes lives: ``{"volume", "created_at"}``.
DATABASE_KEY = "database"
#: Mount targets of a Postgres data directory (the official image's PGDATA, and its parent from 18).
POSTGRES_DATA_TARGETS = ("/var/lib/postgresql/data", "/var/lib/postgresql")

# Runs in a throwaway Composer container. Prints {"active", "floor"} for the
# active volume release (none while the image release is in force) and
# {"record"} for the downgrade record kept on the volume.
RUNTIME_STATE_SCRIPT = """
import json, pathlib, sys
root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/dlux-runtime")
out = {}
try:
    record = json.loads((root / "state" / "composer-deployed.json").read_text(encoding="utf-8"))
    if isinstance(record, dict):
        out["record"] = {key: str(record[key]) for key in ("app", "dlux") if record.get(key)}
        if isinstance(record.get("database"), dict):
            out["record_database"] = {key: str(value) for key, value in record["database"].items()}
except Exception:
    pass
try:
    active = json.loads((root / "state" / "active.json").read_text(encoding="utf-8"))
    version = str(active.get("version") or "").strip()
    if active.get("source") == "volume" and version and "/" not in version and version not in (".", ".."):
        out["active"] = version
        manifest = json.loads((root / "releases" / version / "dlux" / "release-manifest.json").read_text(encoding="utf-8"))
        requires = manifest.get("requires") or {}
        floor = str(requires.get("baked_image") or manifest.get("image_baseline") or "")
        out["floor"] = floor.strip().lstrip(">=").strip()
except Exception:
    pass
print(json.dumps(out))
"""

# Runs in a throwaway Composer container: writes argv[2] (JSON) as the volume's
# downgrade record, atomically and group-writable like every runtime writer.
RUNTIME_WRITE_SCRIPT = """
import os, pathlib, sys
path = pathlib.Path(sys.argv[1]) / "state" / "composer-deployed.json"
path.parent.mkdir(parents=True, exist_ok=True)
temporary = path.with_name("." + path.name + ".tmp")
temporary.write_text(sys.argv[2] + "\\n", encoding="utf-8")
os.chmod(temporary, 0o664)
os.replace(temporary, path)
"""

_GO_NO_VALUE = "<no value>"
#: Docker's ``State.StartedAt`` of a container that never started (Go's zero time).
NEVER_STARTED = "0001-01-01"


def label_versions(labels: Optional[Mapping[str, str]]) -> Dict[str, str]:
    """``{"app": ..., "dlux": ...}`` from image labels; absent keys are unknown."""
    from .watcher import _release_manifest_from_label

    labels = labels or {}
    found: Dict[str, str] = {}
    override = os.environ.get("COMPOSER_VERSION_LABEL", "").strip()
    baked_keys = sorted(key for key in labels if str(key).endswith(BAKED_LABEL_SUFFIX))
    if override.endswith(BAKED_LABEL_SUFFIX) and override in labels:
        baked_keys.insert(0, override)
    for key in baked_keys:
        value = str(labels.get(key) or "").strip()
        if versions.try_parse(value) is not None:
            found["dlux"] = value
            break
    manifest = _release_manifest_from_label(labels.get(MANIFEST_LABEL)) or {}
    for value in (manifest.get("version"), labels.get(OCI_VERSION_LABEL)):
        value = str(value or "").strip()
        if versions.try_parse(value) is not None:
            found["app"] = value
            break
    return found


def newest(*candidates: Mapping[str, str]) -> Dict[str, str]:
    """Per dimension, the highest version any candidate names."""
    result: Dict[str, str] = {}
    for candidate in candidates:
        for key, _ in DIMENSIONS:
            value = str((candidate or {}).get(key) or "").strip()
            parsed = versions.try_parse(value)
            if parsed is None:
                continue
            current = versions.try_parse(result.get(key))
            if current is None or parsed > current:
                result[key] = value
    return result


def downgrades(target: Mapping[str, str], floor: Mapping[str, str]) -> List[Tuple[str, str, str]]:
    """``(label, target, floor)`` for each dimension the target is older in."""
    found = []
    for key, label in DIMENSIONS:
        wanted, recorded = versions.try_parse(target.get(key)), versions.try_parse(floor.get(key))
        if wanted is not None and recorded is not None and wanted < recorded:
            found.append((label, str(target[key]), str(floor[key])))
    return found


def read_record(root: Path = Path(".")) -> Dict[str, str]:
    try:
        payload = json.loads((Path(root) / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {key: str(payload[key]) for key, _ in DIMENSIONS if payload.get(key)}


def record_database(payload) -> Dict[str, str]:
    """``{"volume", "created_at"}`` a record names for its database, or {} (an older record)."""
    value = payload.get(DATABASE_KEY) if isinstance(payload, dict) else None
    if not isinstance(value, dict) or not (value.get("volume") and value.get("created_at")):
        return {}
    return {"volume": str(value["volume"]), "created_at": str(value["created_at"])}


def read_record_database(root: Path = Path(".")) -> Dict[str, str]:
    try:
        payload = json.loads((Path(root) / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return record_database(payload)


def database_volume(config: Mapping) -> str:
    """Docker's name for the named volume holding the stack's Postgres data, or ""."""
    from .dlux_runtime_access import find_runtime_volume, qualified_volume_name

    for target in POSTGRES_DATA_TARGETS:
        short_name = find_runtime_volume(dict(config), target)
        if short_name:
            return qualified_volume_name(dict(config), short_name)
    return ""


def stale_database(recorded: Mapping[str, str], current: Optional[Mapping[str, str]]) -> str:
    """Why a record describes another database than the stack has now, or "".

    ``current`` is None when the stack's database cannot be identified (no named
    Postgres volume, Docker did not answer): the record then counts, as one
    without a database field does.
    """
    if not recorded or current is None:
        return ""
    where = f"the database volume {recorded['volume']} created {recorded['created_at']}"
    if not current:
        return f"{where} no longer exists"
    if dict(current) == dict(recorded):
        return ""
    return f"it names {where}, and this stack's is {current['volume']} created {current['created_at']}"


def read_deployed_image(root: Path = Path(".")) -> Dict[str, str]:
    """``image``, ``repo_digest`` and ``image_id`` of the last deploy, where recorded."""
    try:
        payload = json.loads((Path(root) / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {key: str(payload[key]) for key in IDENTITY_KEYS if payload.get(key)}


def repo_digest_for(image: str, repo_digests) -> str:
    """The ``repo@sha256:…`` entry of ``RepoDigests`` that belongs to ``image``'s repository.

    Docker records Hub images as ``docker.io/[library/]name``; a local build has
    none. An entry for another repository is never taken, so the rollback target
    is the repository the stack names, or nothing.
    """
    def canonical(repository: str) -> str:
        for prefix in ("docker.io/library/", "docker.io/", "index.docker.io/"):
            if repository.startswith(prefix):
                repository = repository[len(prefix):]
                break
        return repository[len("library/"):] if repository.startswith("library/") else repository

    name = str(image or "").split("@", 1)[0]
    last = name.rsplit("/", 1)[-1]
    repository = name.rsplit(":", 1)[0] if ":" in last else name
    for entry in repo_digests or []:
        entry = str(entry)
        if "@sha256:" in entry and canonical(entry.split("@", 1)[0]) == canonical(repository):
            return entry
    return ""


def write_record(
    target: Mapping[str, str], image: str, *, replace: bool, root: Path = Path("."),
    identity: Optional[Mapping[str, str]] = None, database: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """Remember the newest release deployed here; ``replace`` after a forced deploy.

    ``identity`` is what was actually deployed (``repo_digest``, ``image_id``):
    an exact rollback target and audit trail even after the tag moves. It records,
    it does not protect: whoever pushes the tag chooses what the next pull gets.
    ``database`` is the Postgres volume that release migrated.

    Returns why the record could not be written, or None.
    """
    identity = {key: str(value) for key, value in (identity or {}).items() if key in IDENTITY_KEYS and value}
    values = dict(target) if replace else newest(read_record(root), target)
    if not values and not replace and not identity:
        return None
    payload = record_payload(values, image, identity, database)
    path = Path(root) / RECORD
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        return exc.strerror or str(exc)
    return None


def record_payload(
    values: Mapping[str, str], image: str, identity: Optional[Mapping[str, str]] = None,
    database: Optional[Mapping[str, str]] = None,
) -> Dict[str, object]:
    payload: Dict[str, object] = {key: values[key] for key, _ in DIMENSIONS if key in values}
    payload["image"] = image
    payload.update({key: str(value) for key, value in (identity or {}).items() if key in IDENTITY_KEYS and value})
    database = record_database({DATABASE_KEY: dict(database or {})})
    if database:
        payload[DATABASE_KEY] = database
    payload["recorded_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return payload


def record_warning(reason: str, runtime_reason: str = "") -> str:
    where = f"Could not write {RECORD} ({reason})"
    if runtime_reason:
        where += f" or its copy on the DjangoLux runtime volume ({runtime_reason})"
    return (
        f"{where}. The downgrade guard now relies on the app containers' image labels "
        "alone, and a `down` removes those, so an older image deployed after it would "
        "not be refused. Give the deploying user write access to the deploy root's "
        ".xclude/ directory."
    )


def below_runtime_floor(target: Mapping[str, str], runtime: Mapping[str, str]) -> Optional[str]:
    """Why the target's baked DjangoLux is below the active release's image floor."""
    baked, floor = versions.try_parse(target.get("dlux")), versions.try_parse(runtime.get("floor"))
    if baked is None or floor is None or baked >= floor:
        return None
    return (
        f"DjangoLux {target['dlux']} is below the image floor {runtime['floor']} of the "
        f"active runtime release {runtime.get('active') or '(unknown)'}"
    )


def refusal(
    image: str, found: List[Tuple[str, str, str]], unlabeled: bool, extra: Optional[List[str]] = None
) -> str:
    if unlabeled:
        reasons = [
            "it carries no app or DjangoLux version label, so nothing shows it is not "
            "older than the release this deployment already ran"
        ]
    else:
        reasons = [f"{label} {wanted} is older than {recorded}" for label, wanted, recorded in found]
    reasons += list(extra or [])
    lines = [f"Refusing to deploy {image}: " + "; ".join(reasons) + "."]
    lines.append(
        "The database was migrated by the newer release. Migrations do not run "
        "backwards, and older code on a newer schema can fail or write data the "
        "newer release then reads wrongly."
    )
    lines.append(
        "Deploy a release at least as new. To go back on purpose, restore a database "
        "backup taken before the newer release first, then deploy with --force."
    )
    return "\n".join(lines)


class DowngradeGuardMixin:
    """Pre-``up`` refusal of an app image older than the deployed release."""

    def app_image_target(self) -> Tuple[str, List[str]]:
        """``(image, services)`` for the app image this run would (re)create."""
        from .app_permissions import app_image, app_services

        config = self.compose_config_json() or {}
        image = app_image(config)
        services = app_services(config)
        scoped = scoped_service_list(getattr(self, "up_service", None))
        if scoped:
            services = [service for service in services if service in scoped]
        excluded = set(getattr(self, "exclude_services", []) or [])
        services = [service for service in services if service not in excluded]
        return (image, services) if image and services else ("", [])

    def image_labels(self, image: str) -> Optional[Dict[str, str]]:
        """The local image's labels, or None when it is not present."""
        ok, out, _ = self.run_command(
            ["docker", "image", "inspect", image, "--format", "{{json .Config.Labels}}"],
            timeout=15,
        )
        if not ok:
            return None
        try:
            labels = json.loads((out or "").strip() or "null")
        except ValueError:
            return {}
        return labels if isinstance(labels, dict) else {}

    def deployed_image_identity(self, image: str) -> Dict[str, str]:
        """``repo_digest`` and ``image_id`` of the local ``image`` that ``up`` just used."""
        ok, out, _ = self.run_command(
            ["docker", "image", "inspect", image, "--format", "{{json .Id}} {{json .RepoDigests}}"],
            timeout=15,
        )
        if not ok:
            return {}
        try:
            image_id, _, digests = (out or "").strip().partition(" ")
            image_id, digests = json.loads(image_id or "null"), json.loads(digests or "null")
        except ValueError:
            return {}
        identity = {"image_id": str(image_id or "")}
        identity["repo_digest"] = repo_digest_for(image, digests if isinstance(digests, list) else [])
        return {key: value for key, value in identity.items() if value}

    def deployed_app_versions(self, services: List[str]) -> Dict[str, str]:
        """Newest versions labelled on the containers of ``services`` that have run.

        A container that was created but never started (a raw ``docker compose
        create``) ran no migrator, so it says nothing about the database.
        """
        ok, entries, _ = self.get_compose_ps_entries(include_all=True)
        if not ok:
            return {}
        ids = [
            str(entry.get("ID") or "").strip()
            for entry in entries
            if entry.get("Service") in services and str(entry.get("ID") or "").strip()
        ]
        found: Dict[str, str] = {}
        for container_id in ids:
            ok, out, _ = self.run_command(
                ["docker", "container", "inspect", container_id, "--format",
                 "{{json .State.StartedAt}} {{json .Config.Labels}}"],
                timeout=15,
            )
            if not ok:
                continue
            started, _, labels = (out or "").strip().partition(" ")
            try:
                started, labels = json.loads(started or "null"), json.loads(labels or "null")
            except ValueError:
                continue
            if not str(started or "").strip() or str(started).startswith(NEVER_STARTED):
                continue
            if isinstance(labels, dict):
                found = newest(found, label_versions(labels))
        return found

    def target_app_versions(self, image: str, services: List[str]) -> Optional[Dict[str, str]]:
        labels = self.image_labels(image)
        if labels is None and services:
            # Not pulled yet: `up` would fetch it, so fetch it first to read it.
            self.run_docker_compose(["pull", "--quiet", services[0]], timeout=900)
            labels = self.image_labels(image)
        if labels is None:
            return None
        return label_versions(labels)

    def database_identity(self) -> Optional[Dict[str, str]]:
        """``{"volume", "created_at"}`` of the stack's Postgres data volume.

        {} when that volume does not exist (yet), None when it cannot be said.
        """
        volume = database_volume(self.compose_config_json() or {})
        if not volume:
            return None
        ok, out, err = self.run_command(
            ["docker", "volume", "inspect", volume, "--format", "{{json .CreatedAt}}"], timeout=20
        )
        if not ok:
            return {} if "no such volume" in f"{out}\n{err}".lower() else None
        try:
            created = json.loads((out or "").strip() or "null")
        except ValueError:
            return None
        return {"volume": volume, "created_at": str(created)} if created else None

    def runtime_volume(self) -> str:
        """The stack's existing DjangoLux runtime volume, or "" (never created here)."""
        from .dlux_runtime_access import find_runtime_volume, qualified_volume_name

        config = self.compose_config_json() or {}
        short_name = find_runtime_volume(config, RUNTIME_ROOT)
        if not short_name:
            return ""
        volume = qualified_volume_name(config, short_name)
        # `docker run -v` would create a missing volume: use only one that exists.
        ok, _, _ = self.run_command(["docker", "volume", "inspect", volume], timeout=20)
        return volume if ok else ""

    def run_in_runtime_volume(self, volume: str, script: str, *args: str, write: bool = False):
        """Run ``script`` in a throwaway Composer container with the volume mounted."""
        from .dlux_runtime_access import self_image

        mount = f"{volume}:{RUNTIME_ROOT}" + ("" if write else ":ro")
        return self.run_command(
            [
                "docker", "run", "--rm", "--pull", "never", "--network", "none",
                "-v", mount, "--entrypoint", "python",
                self_image(self.run_command), "-c", script, RUNTIME_ROOT, *args,
            ],
            timeout=60,
        )

    def runtime_state(self, volume: str) -> Dict[str, object]:
        """``{"active", "floor", "record"}`` read from the runtime volume, or {}."""
        if not volume:
            return {}
        ok, out, _ = self.run_in_runtime_volume(volume, RUNTIME_STATE_SCRIPT)
        if not ok:
            return {}
        lines = (out or "").strip().splitlines()
        try:
            payload = json.loads(lines[-1]) if lines else {}
        except ValueError:
            return {}
        if not isinstance(payload, dict):
            return {}
        state: Dict[str, object] = {
            key: str(payload[key]) for key in ("active", "floor") if payload.get(key)
        }
        record = payload.get("record")
        if isinstance(record, dict):
            state["record"] = {key: str(record[key]) for key, _ in DIMENSIONS if record.get(key)}
            state["record_database"] = record_database({DATABASE_KEY: payload.get("record_database")})
        return state

    def write_runtime_record(self, volume: str, payload: Mapping[str, str]) -> Optional[str]:
        """Write the downgrade record to the runtime volume; why it failed, or None."""
        ok, out, err = self.run_in_runtime_volume(
            volume, RUNTIME_WRITE_SCRIPT, json.dumps(dict(payload), indent=2), write=True
        )
        if ok:
            return None
        detail = (err or out or "").strip().splitlines()
        return detail[-1][:200] if detail else "the write container failed"

    def downgrade_guard(self) -> Tuple[bool, str]:
        """``(ok, message)``. Remembers the target for ``record_deployed_release``."""
        self.guard_target = None
        if getattr(self, "dev_mode", False) or getattr(self, "build_images", False):
            return True, ""
        image, services = self.app_image_target()
        if not image:
            return True, ""
        target = self.target_app_versions(image, services)
        if target is None:
            return True, ""
        self.guard_volume = self.runtime_volume()
        runtime = self.runtime_state(self.guard_volume)
        database = self.database_identity()
        records = []
        for where, recorded, recorded_database in (
            (str(RECORD), read_record(), read_record_database()),
            ("its copy on the DjangoLux runtime volume", runtime.get("record") or {},
             runtime.get("record_database") or {}),
        ):
            why = stale_database(recorded_database, database) if recorded else ""
            if why:
                self.note_operator(
                    f"The deploy record {where} is not compared: {why}, so the release it names did not "
                    "migrate this database. This deploy's `up` replaces it."
                )
                continue
            records.append(recorded)
        self.guard_records = newest(*records)
        self.guard_target = (image, target)
        floor = newest(self.guard_records, self.deployed_app_versions(services))
        runtime_reason = below_runtime_floor(target, runtime) if target.get("dlux") else None
        found = downgrades(target, floor)
        unlabeled = not target and bool(floor)
        if not found and not unlabeled and not runtime_reason:
            return True, ""
        message = refusal(image, found, unlabeled, [runtime_reason] if runtime_reason else None)
        if getattr(self, "force", False):
            return True, "--force: " + message.splitlines()[0].replace("Refusing to deploy", "Deploying", 1)
        return False, message

    def record_deployed_release(self) -> None:
        """After ``up`` ran the migrator: raise the record to the deployed release."""
        target = getattr(self, "guard_target", None)
        if not target:
            return
        image, values = target
        force = bool(getattr(self, "force", False))
        identity = self.deployed_image_identity(image)
        if not force:
            values = newest(getattr(self, "guard_records", None) or {}, values)
        if not values and not force and not identity:
            return
        database = self.database_identity()
        reason = write_record(values, image, replace=True, identity=identity, database=database)
        volume = getattr(self, "guard_volume", "") or ""
        runtime_reason = (
            self.write_runtime_record(volume, record_payload(values, image, identity, database)) if volume else None
        )
        if reason and (runtime_reason or not volume):
            self.warn_operator(record_warning(reason, runtime_reason or ""))
        elif reason:
            self.append_console(
                f"Downgrade record kept on the runtime volume only: {RECORD} ({reason})."
            )

    def note_operator(self, message: str) -> None:
        """A note for the terminal and the console log; not a deploy warning."""
        print(f"\nℹ {message}", flush=True)
        self.append_console(message)

    def warn_operator(self, message: str) -> None:
        """A warning the operator must see: terminal, console log and deploy status."""
        warnings = getattr(self, "deploy_warnings", None)
        if warnings is None:
            warnings = self.deploy_warnings = []
        if message in warnings:
            return
        warnings.append(message)
        print(f"\n⚠ {message}", flush=True)
        self.append_console(f"WARNING: {message}")
