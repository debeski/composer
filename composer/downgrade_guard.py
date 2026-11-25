"""Refuse to deploy an app image older than the release the database was migrated by.

Migrations only run forwards. Once a newer image's migrator has changed the
schema, the older image's code may fail on it or write rows the newer schema
reads wrongly, and nothing undoes that. The break-it run (2026-11-25, finding
#11) set ``WEB_IMAGE`` back from Decrees 2.2.1 (DjangoLux 1.16.3) to 2.1.26
(DjangoLux 1.15.1) and a plain ``./start.sh`` deployed it over the migrated
database.

The guard runs before every ``up`` and compares the target app image with the
newest release this deployment has run:

- the images of the app containers that exist now, which are what last ran the
  migrator (even when they failed their health check afterwards);
- the record Composer keeps at ``RECORD`` after each ``up``, which survives a
  ``down`` that removed those containers.

Versions come from image labels DjangoLux project images already carry: the
baked DjangoLux version (``org.<project>.dlux_baked_version``) and the app
version in the project release manifest (``org.dlux.project.release-manifest``,
falling back to ``org.opencontainers.image.version``). A dimension either side
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
RUNTIME_ROOT = "/opt/dlux-runtime"

# Runs in a throwaway Composer container: prints {"active": ..., "floor": ...}
# for the active volume release, {} when the image release is in force.
RUNTIME_FLOOR_SCRIPT = """
import json, pathlib, sys
root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/dlux-runtime")
out = {}
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

_GO_NO_VALUE = "<no value>"


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


def write_record(
    target: Mapping[str, str], image: str, *, replace: bool, root: Path = Path(".")
) -> Optional[str]:
    """Remember the newest release deployed here; ``replace`` after a forced deploy.

    Returns why the record could not be written, or None.
    """
    values = dict(target) if replace else newest(read_record(root), target)
    if not values and not replace:
        return None
    payload = {key: values[key] for key, _ in DIMENSIONS if key in values}
    payload["image"] = image
    payload["recorded_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path = Path(root) / RECORD
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        return exc.strerror or str(exc)
    return None


def record_warning(reason: str) -> str:
    return (
        f"Could not write {RECORD} ({reason}). The downgrade guard now relies on the "
        "app containers' image labels alone, and a `down` removes those, so an older "
        "image deployed after it would not be refused. Give the deploying user write "
        "access to the deploy root's .xclude/ directory."
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

    def deployed_app_versions(self, services: List[str]) -> Dict[str, str]:
        """Newest versions labelled on the existing containers of ``services``."""
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
                ["docker", "container", "inspect", container_id, "--format", "{{json .Config.Labels}}"],
                timeout=15,
            )
            if not ok:
                continue
            try:
                labels = json.loads((out or "").strip() or "null")
            except ValueError:
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

    def runtime_release_floor(self) -> Dict[str, str]:
        """``{"active", "floor"}`` of the runtime volume's active release, or {}."""
        from .dlux_runtime_access import find_runtime_volume, qualified_volume_name, self_image

        config = self.compose_config_json() or {}
        short_name = find_runtime_volume(config, RUNTIME_ROOT)
        if not short_name:
            return {}
        volume = qualified_volume_name(config, short_name)
        # `docker run -v` would create a missing volume: read only one that exists.
        ok, _, _ = self.run_command(["docker", "volume", "inspect", volume], timeout=20)
        if not ok:
            return {}
        ok, out, _ = self.run_command(
            [
                "docker", "run", "--rm", "--pull", "never", "--network", "none",
                "-v", f"{volume}:{RUNTIME_ROOT}:ro", "--entrypoint", "python",
                self_image(self.run_command), "-c", RUNTIME_FLOOR_SCRIPT,
            ],
            timeout=60,
        )
        if not ok:
            return {}
        lines = (out or "").strip().splitlines()
        try:
            payload = json.loads(lines[-1]) if lines else {}
        except ValueError:
            return {}
        if not isinstance(payload, dict):
            return {}
        return {key: str(payload[key]) for key in ("active", "floor") if payload.get(key)}

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
        self.guard_target = (image, target)
        floor = newest(read_record(), self.deployed_app_versions(services))
        runtime_reason = below_runtime_floor(target, self.runtime_release_floor()) if target.get("dlux") else None
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
        reason = write_record(values, image, replace=bool(getattr(self, "force", False)))
        if reason:
            self.warn_operator(record_warning(reason))

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
