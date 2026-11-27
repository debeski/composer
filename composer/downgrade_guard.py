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

The records can describe another database than the one the stack has (a host
move, a disaster-recovery restore into a new volume, a fresh clone), so the
database is also asked directly: its ``django_migrations`` rows are read in the
stack's own Postgres service (``exec``, no new network path), and the migrations
the target image ships are listed in a throwaway container of that image
(``--network none``, ``--pull never``, read-only, nothing imported). A database
holding a migration of an app the target ships but does not know was migrated by
newer code, whatever the records say.

Older DjangoLux code on a newer schema follows the rule DjangoLux's own rollback
follows (``migrations.rollback_compatible`` in the DjangoLux release manifest):
it is allowed, with a warning, only when the release being left declares it, and
so does every release between it and the target that Composer can read (the
releases on the runtime volume, the images it deployed). The release being left
must be readable, and, when the database has migrations the target does not
know, it must know every one of them. A project app's own migrations, and an
older app version, are never covered by that rule.

``--force`` deploys anyway and makes the forced release the new record.
"""
import json
import os
import time
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
        out["record_image"] = {key: str(record[key]) for key in ("image", "image_id") if record.get(key)}
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
manifests = {}
try:
    for release in sorted((root / "releases").iterdir()):
        name = release.name
        if "/" in name or name.startswith("."):
            continue
        try:
            manifest = json.loads((release / "dlux" / "release-manifest.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(manifest, dict):
            compatible = (manifest.get("migrations") or {}).get("rollback_compatible")
            manifests[name] = {
                "version": str(manifest.get("version") or name),
                "rollback_compatible": compatible if isinstance(compatible, bool) else None,
            }
except Exception:
    pass
if manifests:
    out["manifests"] = manifests
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

# Runs in a throwaway container of an app image (``--network none``, read-only):
# lists the Django migrations on disk, without importing Django or the project.
# Each root is one group: the image's working directory and PYTHONPATH (the
# project), its site-packages, and every extra argument (a DjangoLux release on
# the runtime volume). ``--only`` scans the arguments alone. A group's ``known``
# maps an app label (the directory holding ``migrations/``) to the migration
# names it ships, the ones a squashed migration ``replaces`` included; its
# ``manifest`` is the DjangoLux release manifest found in it.
MIGRATIONS_SCRIPT = r"""
import ast, json, os, site, sys, sysconfig
SKIP = {"__pycache__", "node_modules", ".git", "static", "staticfiles", "media", "locale", "templates"}
args = sys.argv[1:]
only = bool(args) and args[0] == "--only"
extra = args[1:] if only else args
packages = []
for path in [sysconfig.get_paths().get("purelib"), sysconfig.get_paths().get("platlib")] + list(getattr(site, "getsitepackages", lambda: [])()):
    if path and os.path.isdir(path) and os.path.realpath(path) not in packages:
        packages.append(os.path.realpath(path))
roots = []
if not only:
    for path in [os.getcwd()] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]:
        if os.path.isdir(path):
            roots.append((os.path.realpath(path), "project"))
    roots += [(path, "package") for path in packages]
roots += [(os.path.realpath(path), "package") for path in extra if os.path.isdir(path)]
def replaced(path):
    try:
        if os.path.getsize(path) > 512 * 1024:
            return []
        text = open(path, encoding="utf-8", errors="replace").read()
        if "replaces" not in text:
            return []
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "replaces" for t in node.targets):
                return [tuple(map(str, pair)) for pair in ast.literal_eval(node.value) if len(pair) == 2]
    except Exception:
        return []
    return []
def manifest_in(root):
    for candidate in (os.path.join(root, "dlux", "release-manifest.json"),):
        try:
            data = json.load(open(candidate, encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict):
            compatible = (data.get("migrations") or {}).get("rollback_compatible")
            return {"version": str(data.get("version") or ""),
                    "rollback_compatible": compatible if isinstance(compatible, bool) else None}
    return None
groups, seen = [], set()
for root, kind in roots:
    if root in seen:
        continue
    seen.add(root)
    nested = [other for other, _ in roots if other != root and other.startswith(root + os.sep)]
    known = {}
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP and not d.startswith(".") and os.path.join(directory, d) not in nested]
        if os.path.basename(directory) != "migrations" or "__init__.py" not in files:
            continue
        label = os.path.basename(os.path.dirname(directory))
        names = known.setdefault(label, set())
        for name in files:
            if name.endswith(".py") and not name.startswith(("_", "~")):
                names.add(name[:-3])
                for app, old in replaced(os.path.join(directory, name)):
                    known.setdefault(app, set()).add(old)
    groups.append({"root": root, "kind": kind, "known": {k: sorted(v) for k, v in known.items()},
                   "manifest": manifest_in(root)})
print(json.dumps({"groups": groups}))
"""

# Runs in the stack's Postgres service: one ``app name`` line per applied migration.
APPLIED_SQL = "SELECT app || ' ' || name FROM django_migrations"
PSQL_COMMAND = (
    'psql -X -A -t -q -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-postgres}" '
    '-d "${POSTGRES_DB:-${POSTGRES_USER:-postgres}}" -c "$0"'
)
#: Errors that mean Postgres is still starting: the read is retried.
_PG_STARTING = ("the database system is starting up", "could not connect", "connection refused",
                "no such file or directory", "is not running")

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


def database_service(config: Mapping) -> str:
    """The service that keeps the stack's Postgres data (mounts it at a PGDATA target), or ""."""
    from .dlux_runtime_access import _mounts

    for name, spec in (config.get("services") or {}).items():
        if not isinstance(spec, dict):
            continue
        for _kind, _source, target in _mounts(spec):
            if target.rstrip("/") in POSTGRES_DATA_TARGETS:
                return str(name)
    return ""


def stale_note(where: str, why: str, compared: bool) -> str:
    """What the run says about a record that names another database."""
    tail = (
        "The database's own migrations were compared with the target image instead."
        if compared else
        "This database's own migrations could not be read either, so nothing older is refused on its account."
    )
    return f"The deploy record {where} is not compared: {why}. {tail} This deploy's `up` replaces the record."


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


def parse_applied(output: str) -> set:
    """``{(app, name)}`` from the ``app name`` lines ``APPLIED_SQL`` prints."""
    found = set()
    for line in (output or "").splitlines():
        app, _, name = line.strip().partition(" ")
        if app and name:
            found.add((app, name.strip()))
    return found


def parse_scan(output: str) -> List[Dict[str, object]]:
    """The groups ``MIGRATIONS_SCRIPT`` printed, or [] when its output is not that."""
    lines = (output or "").strip().splitlines()
    try:
        payload = json.loads(lines[-1]) if lines else {}
    except ValueError:
        return []
    groups = payload.get("groups") if isinstance(payload, dict) else None
    result = []
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, dict) or not isinstance(group.get("known"), dict):
            continue
        result.append({
            "root": str(group.get("root") or ""),
            "kind": "project" if group.get("kind") == "project" else "package",
            "known": {str(label): set(map(str, names)) for label, names in group["known"].items()
                      if isinstance(names, list)},
            "manifest": group.get("manifest") if isinstance(group.get("manifest"), dict) else None,
        })
    return result


def merged_known(groups) -> Tuple[Dict[str, set], set]:
    """``(known, project_labels)``: every migration the groups ship, and the labels of the project's own apps."""
    known: Dict[str, set] = {}
    project = set()
    for group in groups:
        for label, names in group["known"].items():
            known.setdefault(label, set()).update(names)
            if group["kind"] == "project":
                project.add(label)
    return known, project


def unknown_migrations(applied, known: Mapping[str, set]) -> List[Tuple[str, str]]:
    """Applied migrations of an app the code ships whose name it does not know.

    An app the code does not ship at all (removed since, or one a newer release
    added) is not compared: its rows prove nothing about the apps that run.
    """
    return sorted((app, name) for app, name in applied if app in known and name not in known[app])


def group_manifest(groups) -> Dict[str, object]:
    """The DjangoLux release manifest summary among ``groups``, or {}."""
    for group in groups:
        manifest = group.get("manifest")
        if isinstance(manifest, dict) and versions.try_parse(manifest.get("version")) is not None:
            return dict(manifest)
    return {}


def _covers(known: Mapping[str, set], migrations) -> bool:
    return all(name in known.get(app, set()) for app, name in migrations)


def rollback_verdict(
    target_dlux: str, left_dlux: str, unknown: List[Tuple[str, str]], releases: List[Mapping[str, object]],
) -> Tuple[bool, str]:
    """``(allowed, why)`` for DjangoLux ``target_dlux`` on a schema newer code migrated.

    ``left_dlux`` is the newest DjangoLux the records and containers name for this
    database ("" when none does), ``unknown`` the database's migrations the target
    does not know, and ``releases`` every DjangoLux release Composer could read:
    ``{"version", "rollback_compatible", "known"}`` (``known`` is None when its
    migrations were not listed). The release being left is ``left_dlux``, or the
    oldest readable release that knows every ``unknown`` migration, whichever is
    newer. It and every readable release after the target up to it must declare
    ``rollback_compatible: true`` (see DjangoLux ``RELEASING.md``).
    """
    target = versions.try_parse(target_dlux)
    if target is None:
        return False, "the target image labels no DjangoLux version"
    readable = {}
    for release in releases:
        parsed = versions.try_parse(release.get("version"))
        if parsed is not None and parsed > target:
            readable.setdefault(str(release["version"]), release)
    left = versions.try_parse(left_dlux)
    if unknown:
        covering = [release for release in readable.values()
                    if release.get("known") is not None and _covers(release["known"], unknown)]
        if not covering:
            return False, "no DjangoLux release Composer can read knows those migrations"
        oldest = min(covering, key=lambda release: versions.parse(release["version"]))
        if left is None or versions.parse(oldest["version"]) > left:
            left_dlux, left = str(oldest["version"]), versions.parse(oldest["version"])
    if left is None or left <= target:
        return False, "the release that migrated this database cannot be named"
    if left_dlux not in readable and not any(versions.parse(v) == left for v in readable):
        return False, f"DjangoLux {left_dlux}'s release manifest cannot be read here"
    crossed = sorted(
        (release for release in readable.values() if versions.parse(release["version"]) <= left),
        key=lambda release: versions.parse(release["version"]),
    )
    refusing = [str(release["version"]) for release in crossed if release.get("rollback_compatible") is not True]
    if refusing:
        return False, (
            f"DjangoLux {', '.join(refusing)} {'does' if len(refusing) == 1 else 'do'} not declare "
            "migrations.rollback_compatible"
        )
    return True, left_dlux


def rollback_warning(target_dlux: str, left_dlux: str) -> str:
    """The warning DjangoLux's own rollback shows for the same move."""
    return (
        f"DjangoLux {target_dlux} now runs on a schema with DjangoLux {left_dlux}'s migrations, which "
        "that release declares compatible with the code before it (migrations.rollback_compatible). "
        "This deploy never reverses database migrations: the database keeps the newer release's "
        "migrations, so a backup taken before that release no longer matches it. To restore that backup "
        "later, update back to its version first, or restore it with “Ignore database version mismatch”."
    )


def migration_refusal(image: str, unknown: List[Tuple[str, str]], why: str = "") -> str:
    """The refusal for a database holding migrations ``image`` does not know."""
    shown = ", ".join(f"{app}.{name}" for app, name in unknown[:6])
    more = f" and {len(unknown) - 6} more" if len(unknown) > 6 else ""
    lines = [
        f"Refusing to deploy {image}: this database has {len(unknown)} migration(s) the image does not "
        f"know ({shown}{more}), so newer code migrated it" + (f", and {why}" if why else "") + "."
    ]
    lines.append(
        "Migrations do not run backwards, and older code on a newer schema can fail or write data the "
        "newer release then reads wrongly. That holds whatever the deploy records say: a restored or "
        "moved database carries its schema with it."
    )
    lines.append(
        "Deploy a release at least as new as the one that migrated it. To go back on purpose, restore a "
        "database backup taken before the newer release first, then deploy with --force."
    )
    return "\n".join(lines)


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
            if isinstance(payload.get("record_image"), dict):
                state["record_image"] = {key: str(value) for key, value in payload["record_image"].items()}
        manifests = payload.get("manifests")
        if isinstance(manifests, dict):
            state["manifests"] = {
                str(name): {"version": str(entry.get("version") or name),
                            "rollback_compatible": entry.get("rollback_compatible")}
                for name, entry in manifests.items() if isinstance(entry, dict)
            }
        return state

    def service_running(self, service: str) -> bool:
        ok, entries, _ = self.get_compose_ps_entries()
        return ok and any(
            entry.get("Service") == service and str(entry.get("State") or "").lower() == "running"
            for entry in entries
        )

    def applied_migrations(self, database: Optional[Mapping[str, str]]) -> Tuple[Optional[set], str]:
        """``(applied, why)``: the database's applied migrations, or None and why they cannot be read.

        Read in the stack's own Postgres service. One that is not running is
        started first, as the `up` that follows would; ``guard_started_db``
        remembers it so a refusal stops it again. No data volume means nothing
        was migrated. A stack with no Postgres service is not read ("" reason).
        """
        service = database_service(self.compose_config_json() or {})
        if not service:
            return None, ""
        if not self.service_running(service):
            if database == {}:
                return set(), ""
            ok, out, err = self.run_docker_compose(
                ["up", "-d", "--no-deps", "--wait", "--wait-timeout", "120", service], timeout=180
            )
            if not ok:
                detail = (err or out or "").strip().splitlines()
                return None, f"its {service} service did not start ({detail[-1][:200] if detail else 'no output'})"
            self.guard_started_db = service
        last = ""
        for attempt in range(30):
            ok, out, err = self.run_docker_compose(
                ["exec", "-T", service, "sh", "-c", PSQL_COMMAND, APPLIED_SQL], timeout=60
            )
            if ok:
                return parse_applied(out), ""
            text = f"{out}\n{err}"
            lowered = text.lower()
            if "django_migrations" in lowered and "does not exist" in lowered:
                return set(), ""
            lines = text.strip().splitlines()
            last = lines[-1][:200] if lines else "no output"
            if not any(marker in lowered for marker in _PG_STARTING):
                break
            time.sleep(1)
        return None, f"{service} answered: {last}"

    def stop_started_database(self) -> None:
        service = getattr(self, "guard_started_db", "") or ""
        if service:
            self.run_docker_compose(["stop", service], timeout=120)
            self.guard_started_db = ""

    def scan_migrations(self, image: str, extra: Tuple[str, ...] = (), volume: str = "",
                        only: bool = False) -> Optional[List[Dict[str, object]]]:
        """The migration groups ``image`` ships (``MIGRATIONS_SCRIPT``), or None when it cannot be listed."""
        argv = [
            "docker", "run", "--rm", "--pull", "never", "--network", "none", "--read-only",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
        ]
        if volume:
            argv += ["-v", f"{volume}:{RUNTIME_ROOT}:ro"]
        argv += ["--entrypoint", "python", image, "-I", "-c", MIGRATIONS_SCRIPT]
        argv += (["--only"] if only else []) + list(extra)
        ok, out, _ = self.run_command(argv, timeout=120)
        if not ok:
            return None
        groups = parse_scan(out)
        return groups if groups or (out or "").strip().endswith("}") else None

    def target_migrations(self, image: str, runtime: Mapping[str, object]):
        """What the target runs knows: its image, plus the active release on the runtime volume."""
        active = str(runtime.get("active") or "")
        volume = getattr(self, "guard_volume", "") or ""
        if active and volume:
            return self.scan_migrations(image, (f"{RUNTIME_ROOT}/releases/{active}",), volume)
        return self.scan_migrations(image)

    def deployed_app_images(self, services: List[str]) -> List[str]:
        """Image IDs of the app containers of ``services`` (any state)."""
        ok, entries, _ = self.get_compose_ps_entries(include_all=True)
        images: List[str] = []
        for entry in entries if ok else []:
            container = str(entry.get("ID") or "").strip()
            if entry.get("Service") not in services or not container:
                continue
            found, out, _ = self.run_command(["docker", "inspect", "--format", "{{.Image}}", container], timeout=15)
            image = (out or "").strip() if found else ""
            if image and image not in images:
                images.append(image)
        return images

    def readable_releases(self, image: str, services: List[str], runtime: Mapping[str, object],
                          unknown: List[Tuple[str, str]]) -> List[Dict[str, object]]:
        """Every DjangoLux release Composer can read for the rollback rule.

        The releases on the runtime volume (their manifests; their migrations
        too when ``unknown`` needs them), and the images this deployment ran:
        the records' images and the app containers' images, when they are local.
        """
        releases: List[Dict[str, object]] = []
        manifests = runtime.get("manifests") or {}
        volume = getattr(self, "guard_volume", "") or ""
        listed: Dict[str, Dict[str, set]] = {}
        if unknown and manifests and volume:
            from .dlux_runtime_access import self_image

            roots = tuple(f"{RUNTIME_ROOT}/releases/{name}" for name in manifests)
            for group in self.scan_migrations(self_image(self.run_command), roots, volume, only=True) or []:
                listed[group["root"].rsplit("/", 1)[-1]] = group["known"]
        for name, entry in manifests.items():
            releases.append({**entry, "known": listed.get(name) if unknown else None})
        candidates: List[str] = []
        for recorded in (read_deployed_image(), runtime.get("record_image") or {}):
            for key in ("image_id", "image"):
                if recorded.get(key) and recorded[key] not in candidates:
                    candidates.append(recorded[key])
        candidates += [ref for ref in self.deployed_app_images(services) if ref not in candidates]
        target_id = self._image_id(image)
        for ref in candidates[:6]:
            if target_id and self._image_id(ref) == target_id:
                continue
            if self.image_labels(ref) is None:
                continue
            groups = self.scan_migrations(ref)
            manifest = group_manifest(groups or [])
            if manifest:
                releases.append({**manifest, "known": merged_known(groups)[0]})
        return releases

    def _image_id(self, image: str) -> str:
        ok, out, _ = self.run_command(["docker", "image", "inspect", "--format", "{{.Id}}", image], timeout=15)
        return (out or "").strip() if ok else ""

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
        self.guard_started_db = ""
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
        applied, applied_reason = self.applied_migrations(database)
        known: Dict[str, set] = {}
        project: set = set()
        if applied:
            groups = self.target_migrations(image, runtime)
            if groups is None:
                applied, applied_reason = None, f"the migrations {image} ships could not be listed"
            else:
                known, project = merged_known(groups)
        if applied is None and applied_reason:
            self.warn_operator(
                f"The downgrade guard could not compare this database's migrations with {image}: "
                f"{applied_reason}. It compared the deploy records and the app containers only."
            )
        records = []
        for where, recorded, recorded_database in (
            (str(RECORD), read_record(), read_record_database()),
            ("its copy on the DjangoLux runtime volume", runtime.get("record") or {},
             runtime.get("record_database") or {}),
        ):
            why = stale_database(recorded_database, database) if recorded else ""
            if why:
                self.note_operator(stale_note(where, why, applied is not None))
                continue
            records.append(recorded)
        self.guard_records = newest(*records)
        self.guard_target = (image, target)
        floor = newest(self.guard_records, self.deployed_app_versions(services))
        runtime_reason = below_runtime_floor(target, runtime) if target.get("dlux") else None
        found = downgrades(target, floor)
        unlabeled = not target and bool(floor)
        unknown = unknown_migrations(applied or set(), known)
        if not found and not unlabeled and not runtime_reason and not unknown:
            return True, ""
        own = sorted({app for app, _ in unknown} & project)
        why = ""
        if own:
            why = f"the project's own app{'s' if len(own) > 1 else ''} {', '.join(own)} {'are' if len(own) > 1 else 'is'} among them"
        elif not unlabeled and not runtime_reason and all(label == "DjangoLux" for label, _, _ in found):
            allowed, verdict = rollback_verdict(
                target.get("dlux", ""), floor.get("dlux", ""), unknown,
                self.readable_releases(image, services, runtime, unknown),
            )
            if allowed:
                self.warn_operator(rollback_warning(target["dlux"], verdict))
                return True, ""
            why = verdict
        if found or unlabeled or runtime_reason:
            extra = [runtime_reason] if runtime_reason else []
            if unknown:
                extra.append(f"the database has {len(unknown)} migration(s) it does not know")
            if why and (unknown or found):
                extra.append(why)
            message = refusal(image, found, unlabeled, extra)
        else:
            message = migration_refusal(image, unknown, why)
        if getattr(self, "force", False):
            return True, "--force: " + message.splitlines()[0].replace("Refusing to deploy", "Deploying", 1)
        self.stop_started_database()
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
