"""Group access to app data for DjangoLux images that run as a non-root uid.

DjangoLux images run as uid 10001 with primary group 0 (root): the
``permissions`` block of DjangoLux's runtime contract, and its
``dlux.mount_access`` gate, which this module mirrors. Data written while the
stack ran as root is ``root:root`` with directories at 0755, so that app could
read it but not create, delete or rename anything in it.

Composer grants what the app needs through group 0 and changes nothing else:

- Only modes change. The group changes only on an entry whose group is neither
  0 nor the app's, such as a file copied in from a workstation.
- Nothing is moved, renamed, deleted or rewritten; contents and mtimes stay.
- Symlinks are never followed, other filesystems are not entered, and a regular
  file with more than one hard link is reported, never changed.
- Every change is recorded in a manifest under
  ``.xclude/composer-check/<UTC>/app-permissions/``;
  ``composer app-permissions revert <manifest>`` restores the previous modes.

Root ignores these bits, so a granted tree still works for the previous root
image, and the grant can run while the stack serves.

What the app needs per entry, on a mount any app service writes: directories
``rwx``; files ``r``; files on a ``logs`` mount ``rw`` (a log handler appends
and rotates). On the runtime volume the app writes only into the top-level
directories and the ``state/`` tree (``rwx`` directories); it replaces a file
there by renaming a new one over it, never by writing into it, so every file
needs only ``r``, and installed releases are only read. On a mount every app
service reads only: directories ``r-x``, files ``r``. DjangoLux's
``dlux.mount_access`` applies the same rule.

The walk runs in a short-lived container from Composer's own image with only
the capabilities it needs (none for a scan beyond reading) and no network: the
CLI and the executor reach named volumes only through Docker.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

APP_UID = 10001
APP_GID = 0
RUNTIME_ROOT = "/opt/dlux-runtime"
SIBLING_ROOT = "/composer-app-data"
SIBLING_MANIFEST = "/composer-manifest"
MANIFEST_NAME = "app-permissions.tsv.gz"
QUICK_DEPTH = 2
SAMPLE_LIMIT = 20
WALK_TIMEOUT = 6 * 3600

READ, WRITE, SEARCH = 4, 2, 1
DOCKER_SOCKET = "/var/run/docker.sock"

# The runtime contract's 0775 directories and 0664 files: composer-agent (uid 0,
# no capabilities) and a non-root app reach each other's entries through group 0.
SHARED_UMASK = 0o002
SHARED_FILE_MODE = 0o664


def share_runtime_dirs(*paths) -> None:
    """Give group 0 write on runtime-volume directories this process owns.

    A fresh volume's root is created by Docker at 0755, owned by root; the
    resident agent owns it and may open it to the app. Anything it does not own
    is left to ``check --fix``.
    """
    for path in paths:
        try:
            st = os.stat(path)
            if st.st_uid == os.geteuid() and stat.S_ISDIR(st.st_mode) and stat.S_IMODE(st.st_mode) & 0o070 != 0o070:
                os.chmod(path, stat.S_IMODE(st.st_mode) | 0o070)
        except OSError:
            continue


@dataclass
class Target:
    label: str
    kind: str  # "bind" | "volume"
    source: str  # host path, or the Docker-level volume name
    role: str  # "data" | "logs" | "runtime"
    read_only: bool


# ── identity ────────────────────────────────────────────────────────────────


def parse_user(value: str) -> Tuple[str, Optional[int], Optional[int]]:
    """Read an image's ``Config.User``: ``("root"|"user"|"named", uid, gid)``.

    An empty or ``0`` user is root. ``uid:gid`` and a bare ``uid`` (Docker then
    uses group 0 for a uid without a passwd entry, which is what DjangoLux
    images use) are numeric. A name cannot be resolved without the image's
    passwd file, so it is reported rather than guessed.
    """
    raw = str(value or "").strip()
    if not raw:
        return "root", 0, 0
    user, _, group = raw.partition(":")
    if user in {"0", "root"}:
        return "root", 0, 0
    if not user.isdigit() or (group and not group.isdigit()):
        return "named", None, None
    return "user", int(user), int(group) if group else 0


# ── discovery ───────────────────────────────────────────────────────────────


def _mounts(spec: dict) -> Iterator[Tuple[str, str, str, bool]]:
    for entry in spec.get("volumes") or []:
        if isinstance(entry, dict):
            yield (
                str(entry.get("type") or "volume"),
                str(entry.get("source") or ""),
                str(entry.get("target") or ""),
                bool(entry.get("read_only")),
            )
            continue
        parts = str(entry).split(":")
        if len(parts) < 2:
            continue
        source, target = parts[0], parts[1]
        kind = "bind" if source.startswith((".", "/", "~")) else "volume"
        yield kind, source, target, len(parts) > 2 and "ro" in parts[2].split(",")


_APP_ROLES = ("web", "worker")


def service_image(config: dict, name: str) -> str:
    """The image ``up`` runs for service ``name``.

    A service with only a ``build`` section (development mode resets ``image:``
    and builds) has no ``image`` in the resolved config; Compose tags what it
    builds ``<project>-<service>``, so that is the image to read.
    """
    spec = (config.get("services") or {}).get(name)
    if not isinstance(spec, dict):
        return ""
    if spec.get("image"):
        return str(spec["image"])
    if spec.get("build") and config.get("name"):
        return f"{config['name']}-{name}".lower()
    return ""


def _image_key(spec) -> Optional[Tuple[str, str]]:
    """What makes two services run the same app: one image, or one build."""
    if not isinstance(spec, dict):
        return None
    if spec.get("image"):
        return ("image", str(spec["image"]))
    build = spec.get("build")
    if isinstance(build, str):
        build = {"context": build}
    if isinstance(build, dict) and build:
        return ("build", json.dumps(build, sort_keys=True, default=str))
    return None


def _app_role(config: dict) -> str:
    for name in _APP_ROLES:
        if service_image(config, name):
            return name
    return ""


def app_image(config: dict) -> str:
    role = _app_role(config)
    return service_image(config, role) if role else ""


def app_services(config: dict) -> List[str]:
    """Every service running the app: the same image as web/worker, or the same build.

    Built services get one image name each (``<project>-web``,
    ``<project>-worker``), so matching on the image name alone would leave the
    worker's mounts out in development mode.
    """
    role = _app_role(config)
    if not role:
        return []
    services = config.get("services") or {}
    key = _image_key(services.get(role))
    return sorted(
        name for name, spec in services.items()
        if key is not None and _image_key(spec) == key
    )


def _role(container_target: str) -> str:
    target = container_target.rstrip("/")
    if target == RUNTIME_ROOT:
        return "runtime"
    if os.path.basename(target) == "logs":
        return "logs"
    return "data"


def discover_targets(config: dict, project_dir: str) -> List[Target]:
    """Every bind source and named volume an app service mounts, once each."""
    from .dlux_runtime_access import qualified_volume_name

    project = os.path.abspath(project_dir)
    found: Dict[Tuple[str, str], Target] = {}
    for name in app_services(config):
        spec = config["services"][name]
        for kind, source, target, read_only in _mounts(spec):
            if kind not in {"bind", "volume"} or not source or not target:
                continue
            if kind == "bind":
                source = os.path.abspath(source)
                if source in {DOCKER_SOCKET, project} or project.startswith(source.rstrip("/") + "/"):
                    continue
                relative = os.path.relpath(source, project)
                label = f"./{relative}" if not relative.startswith("..") else source
            else:
                short = source
                source = qualified_volume_name(config, short)
                label = f"volume {short}"
            key = (kind, source)
            role = _role(target)
            if key in found:
                existing = found[key]
                existing.read_only = existing.read_only and read_only
                if existing.role == "data":
                    existing.role = role
                continue
            found[key] = Target(label=label, kind=kind, source=source, role=role, read_only=read_only)
    return [found[key] for key in sorted(found)]


# ── the rule ────────────────────────────────────────────────────────────────


def allowed(st, uid: int, gid: int, want: int) -> bool:
    if uid == 0:
        return True
    if st.st_uid == uid:
        bits = (st.st_mode >> 6) & 7
    elif st.st_gid == gid:
        bits = (st.st_mode >> 3) & 7
    else:
        bits = st.st_mode & 7
    return bits & want == want


def needs(st, role: str, read_only: bool, parts: Tuple[str, ...] = ()) -> int:
    """The bits the app needs on one entry; ``parts`` is its path below the mount root."""
    is_dir = stat.S_ISDIR(st.st_mode)
    if read_only:
        return READ | SEARCH if is_dir else READ
    if role == "runtime":
        if is_dir:
            writes = len(parts) <= 1 or parts[:1] == ("state",)
            return READ | WRITE | SEARCH if writes else READ | SEARCH
        return READ
    if is_dir:
        return READ | WRITE | SEARCH
    return READ | WRITE if role == "logs" else READ


def planned(st, uid: int, gid: int, want: int) -> Optional[Tuple[Optional[int], int]]:
    """``(new_gid or None, new_mode)`` that grants ``want``, or None when nothing is needed."""
    if allowed(st, uid, gid, want):
        return None
    mode = stat.S_IMODE(st.st_mode)
    if st.st_uid == uid:
        return None, mode | (want << 6)
    return (None if st.st_gid == gid else gid), mode | (want << 3)


def _describe(want: int) -> str:
    return "".join(letter for bit, letter in ((READ, "r"), (WRITE, "w"), (SEARCH, "x")) if want & bit)


def walk(root: str, max_depth: Optional[int] = None) -> Iterator[Tuple[str, os.stat_result]]:
    """``root`` and what is below it, never following links or leaving its filesystem."""
    try:
        top = os.lstat(root)
    except OSError:
        return
    yield root, top
    stack = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        if max_depth is not None and depth >= max_depth:
            continue
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if st.st_dev != top.st_dev:
                continue
            yield entry.path, st
            if stat.S_ISDIR(st.st_mode):
                stack.append((entry.path, depth + 1))


# ── inside the sibling container ────────────────────────────────────────────


def _relative(root: str, path: str) -> str:
    relative = os.path.relpath(path, root)
    return "." if relative == "." else relative


def process_target(spec: dict, *, apply: bool, uid: int, gid: int, depth: Optional[int], manifest=None) -> dict:
    root = spec["path"]
    summary = {
        "label": spec["label"], "entries": 0, "wrong": 0, "changed": 0,
        "hardlinks": 0, "errors": 0, "sample": [],
    }
    if not os.path.lexists(root):
        summary["missing"] = True
        return summary
    for path, st in walk(root, depth):
        summary["entries"] += 1
        if stat.S_ISLNK(st.st_mode):
            continue
        relative = _relative(root, path)
        parts = () if relative == "." else tuple(relative.split(os.sep))
        want = needs(st, spec["role"], spec["read_only"], parts)
        change = planned(st, uid, gid, want)
        if change is None:
            continue
        summary["wrong"] += 1
        new_gid, new_mode = change
        if len(summary["sample"]) < SAMPLE_LIMIT:
            summary["sample"].append({
                "path": relative,
                "kind": "dir" if stat.S_ISDIR(st.st_mode) else "file",
                "owner": f"{st.st_uid}:{st.st_gid}",
                "mode": f"{stat.S_IMODE(st.st_mode):04o}",
                "needs": _describe(want),
            })
        if not apply:
            continue
        if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
            summary["hardlinks"] += 1
            continue
        try:
            if new_gid is not None:
                os.lchown(path, -1, new_gid)
            os.chmod(path, new_mode)
        except OSError:
            summary["errors"] += 1
            continue
        summary["changed"] += 1
        if manifest is not None:
            manifest.write(
                "\t".join([
                    spec["label"], relative, f"{st.st_uid}:{st.st_gid}", f"{stat.S_IMODE(st.st_mode):04o}",
                    str(gid if new_gid is not None else st.st_gid), f"{new_mode:04o}",
                ]) + "\n"
            )
    return summary


def revert_target(spec: dict, rows: List[List[str]]) -> dict:
    """Put back the recorded mode and group, only where the entry still has the granted ones."""
    root = spec["path"]
    summary = {"label": spec["label"], "reverted": 0, "skipped": 0, "errors": 0}
    for row in rows:
        _label, relative, old_owner, old_mode, new_gid, new_mode = row
        path = os.path.normpath(os.path.join(root, relative))
        if path != root and not path.startswith(root.rstrip("/") + "/"):
            summary["skipped"] += 1
            continue
        try:
            st = os.lstat(path)
        except OSError:
            summary["skipped"] += 1
            continue
        if stat.S_ISLNK(st.st_mode) or stat.S_IMODE(st.st_mode) != int(new_mode, 8) or st.st_gid != int(new_gid):
            summary["skipped"] += 1
            continue
        old_gid = int(old_owner.partition(":")[2])
        try:
            if old_gid != st.st_gid:
                os.lchown(path, -1, old_gid)
            os.chmod(path, int(old_mode, 8))
        except OSError:
            summary["errors"] += 1
            continue
        summary["reverted"] += 1
    return summary


class _LazyManifest:
    """Opened on the first change, so a grant that changes nothing leaves no file."""

    def __init__(self, path: str):
        self.path = path
        self.stream = None

    def write(self, line: str) -> None:
        if self.stream is None:
            self.stream = gzip.open(self.path, "wt", encoding="utf-8")
        self.stream.write(line)

    def close(self) -> None:
        if self.stream is not None:
            self.stream.close()


def run_sibling(argv: List[str]) -> int:
    """``composer app-permissions scan|apply|revert --spec JSON``, inside the sibling."""
    parser = argparse.ArgumentParser(prog="composer app-permissions")
    parser.add_argument("action", choices=["scan", "apply", "revert"])
    parser.add_argument("--spec", required=True)
    args = parser.parse_args(argv)
    spec = json.loads(args.spec)
    uid, gid, depth = int(spec["uid"]), int(spec["gid"]), spec.get("depth")
    results = []
    if args.action == "revert":
        rows: Dict[str, List[List[str]]] = {}
        with gzip.open(os.path.join(SIBLING_MANIFEST, spec["manifest"]), "rt", encoding="utf-8") as stream:
            for line in stream:
                fields = line.rstrip("\n").split("\t")
                if len(fields) == 6:
                    rows.setdefault(fields[0], []).append(fields)
        for target in spec["targets"]:
            results.append(revert_target(target, rows.get(target["label"], [])))
    else:
        apply = args.action == "apply"
        manifest = _LazyManifest(os.path.join(SIBLING_MANIFEST, spec["manifest"])) if apply and spec.get("manifest") else None
        try:
            for target in spec["targets"]:
                results.append(process_target(target, apply=apply, uid=uid, gid=gid, depth=depth, manifest=manifest))
        finally:
            if manifest is not None:
                manifest.close()
    print(json.dumps({"targets": results}, sort_keys=True))
    return 1 if any(item.get("errors") for item in results) else 0


# ── on the host side (CLI or executor) ──────────────────────────────────────


class PermissionsError(RuntimeError):
    pass


def image_identity(runner, image: str) -> Tuple[str, Optional[int], Optional[int]]:
    """``parse_user`` of the local image, or ``("unknown", None, None)`` when it is not here."""
    ok, out, _err = runner(["docker", "image", "inspect", image, "--format", "{{.Config.User}}"], timeout=20)
    if not ok:
        return "unknown", None, None
    return parse_user(out.strip())


def _volume_exists(runner, name: str) -> bool:
    ok, _out, _err = runner(["docker", "volume", "inspect", name], timeout=20)
    return ok


def sibling_command(
    targets: List[Tuple[Target, bool]],
    *,
    image: str,
    action: str,
    uid: int,
    gid: int,
    depth: Optional[int],
    manifest_dir: Optional[str] = None,
    manifest_name: Optional[str] = None,
) -> List[str]:
    """``docker run`` for the walk. ``targets`` pairs each target with whether it may be created."""
    writes = action in {"apply", "revert"}
    command = [
        "docker", "run", "--rm", "--network", "none", "--user", "0:0",
        "--cap-drop", "ALL", "--cap-add", "DAC_READ_SEARCH",
        "--security-opt", "no-new-privileges:true",
    ]
    if writes:
        command += ["--cap-add", "FOWNER", "--cap-add", "CHOWN"]
    specs = []
    for index, (target, create) in enumerate(targets):
        path = f"{SIBLING_ROOT}/{index}"
        if target.kind == "bind" and create:
            # `-v` creates a missing host directory, as `compose up` would; the
            # walk then grants the app group access to it.
            command += ["-v", f"{target.source}:{path}:rw"]
        else:
            mount = f"type={target.kind},source={target.source},target={path}"
            command += ["--mount", mount if writes else mount + ",readonly"]
        specs.append({"path": path, "label": target.label, "role": target.role, "read_only": target.read_only})
    if manifest_dir:
        command += ["-v", f"{manifest_dir}:{SIBLING_MANIFEST}:rw"]
    spec = {"uid": uid, "gid": gid, "depth": depth, "targets": specs}
    if manifest_name:
        spec["manifest"] = manifest_name
    command += [image, "app-permissions", action, "--spec", json.dumps(spec, sort_keys=True)]
    return command


def run(
    launcher,
    action: str,
    *,
    uid: int = APP_UID,
    gid: int = APP_GID,
    depth: Optional[int] = None,
    project_dir: Optional[str] = None,
    revert_manifest: Optional[str] = None,
) -> dict:
    """Scan, grant or revert app-data access for this stack; returns the summary.

    Missing named volumes are left alone (Docker creates them on ``up``, from
    the image). A missing bind directory is reported by a scan and created by a
    grant, which then makes it usable to the app.
    """
    from .dlux_runtime_access import self_image

    project = os.path.abspath(project_dir or os.getcwd())
    config = launcher.compose_config_json()
    if not config:
        raise PermissionsError(
            "Could not read the compose configuration."
            + (f"\n{launcher.last_runtime_diagnostic}" if getattr(launcher, "last_runtime_diagnostic", "") else "")
        )
    chosen: List[Tuple[Target, bool]] = []
    missing: List[str] = []
    for target in discover_targets(config, project):
        if target.kind == "volume":
            if not _volume_exists(launcher.run_command, target.source):
                continue
            chosen.append((target, False))
            continue
        visible = project == target.source or target.source.startswith(project + "/")
        if visible and not os.path.exists(target.source):
            if action == "apply":
                chosen.append((target, True))
            else:
                missing.append(target.label)
            continue
        chosen.append((target, False))
    summary = {"action": action, "uid": uid, "gid": gid, "targets": [], "missing": missing, "manifest": ""}
    if not chosen:
        return summary

    manifest_dir = manifest_name = None
    if action == "apply":
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        manifest_dir = os.path.join(project, ".xclude", "composer-check", stamp, "app-permissions")
        manifest_name = MANIFEST_NAME
    elif action == "revert":
        manifest_path = os.path.abspath(str(revert_manifest or ""))
        manifest_dir, manifest_name = os.path.dirname(manifest_path), os.path.basename(manifest_path)

    command = sibling_command(
        chosen, image=self_image(launcher.run_command), action=action, uid=uid, gid=gid,
        depth=depth, manifest_dir=manifest_dir, manifest_name=manifest_name,
    )
    ok, out, err = launcher.run_command(command, timeout=WALK_TIMEOUT)
    lines = [line for line in out.splitlines() if line.strip().startswith("{")]
    if not lines:
        raise PermissionsError(
            "The permissions walk did not report back."
            + (f"\n{(err or out).strip()[-1500:]}" if (err or out).strip() else "")
        )
    summary["targets"] = json.loads(lines[-1]).get("targets", [])
    if action == "apply" and any(item.get("changed") for item in summary["targets"]):
        summary["manifest"] = os.path.join(manifest_dir, manifest_name)
    summary["ok"] = ok
    return summary


def totals(summary: dict) -> Dict[str, int]:
    keys = ("entries", "wrong", "changed", "hardlinks", "errors", "reverted", "skipped")
    return {key: sum(int(item.get(key) or 0) for item in summary.get("targets", [])) for key in keys}


def examples(summary: dict, limit: int = 3) -> str:
    shown = []
    for item in summary.get("targets", []):
        for entry in item.get("sample", []):
            path = item["label"] if entry["path"] == "." else f"{item['label'].rstrip('/')}/{entry['path']}"
            shown.append(f"{path} ({entry['kind']} {entry['owner']} {entry['mode']}, needs {entry['needs']})")
            if len(shown) >= limit:
                return "; ".join(shown)
    return "; ".join(shown)


def _compose(launcher, args: List[str]) -> Tuple[bool, str, str]:
    """A compose pull or build through the launcher, with the same env (and progress) ``up`` gets."""
    stream = getattr(launcher, "run_docker_compose_streaming", None)
    emit = getattr(launcher, "emit_pull_progress", None)
    if stream is None or emit is None:
        return launcher.run_docker_compose(args)
    progress = getattr(launcher, "pull_progress", None)
    if progress is not None:
        progress.reset()
    label = "Pull" if args[0] == "pull" else "Build"
    return stream(args, progress_callback=lambda line: emit(label, line))


def fetch_app_image(launcher, config: dict, image: str) -> Tuple[str, Optional[int], Optional[int], str]:
    """``image_identity`` of the image ``up`` is about to run, fetching it first when needed.

    ``up`` pulls an image that is not on the host and builds one with ``--build``,
    then recreates at once, so reading only what is local would grant for the
    wrong user, or for none. The app services are pulled (or built, for those
    with a ``build`` section) here instead, the way ``up`` would get them. The
    last item is the pull or build output when the image still cannot be read.
    """
    services = app_services(config)
    specs = config.get("services") or {}
    buildable = [name for name in services if isinstance(specs.get(name), dict) and specs[name].get("build")]
    if buildable and getattr(launcher, "build_images", False):
        ok, out, err = _compose(launcher, ["build", *buildable])
        if not ok:
            return "unknown", None, None, (err or out).strip()
    kind, uid, gid = image_identity(launcher.run_command, image)
    if kind != "unknown" or not services:
        return kind, uid, gid, ""
    pull = ["pull", "--ignore-pull-failures"] if buildable else ["pull"]
    ok, out, err = _compose(launcher, [*pull, *services])
    detail = (err or out).strip()
    kind, uid, gid = image_identity(launcher.run_command, image)
    if kind == "unknown" and buildable:
        ok, out, err = _compose(launcher, ["build", *buildable])
        detail = (err or out).strip()
        kind, uid, gid = image_identity(launcher.run_command, image)
    return kind, uid, gid, detail if kind == "unknown" else ""


def ensure_for_image(launcher, image: str, config: Optional[dict] = None) -> Tuple[bool, str]:
    """Before ``up``: grant the image's non-root uid what it needs, or refuse.

    A root image needs nothing. An image that is not on the host yet is pulled
    (or built) first, so the uid is read from the image ``up`` will run; when it
    still cannot be read, nothing is recreated.
    """
    if config is None:
        config = launcher.compose_config_json() or {}
    kind, uid, gid, detail = fetch_app_image(launcher, config, image)
    if kind == "unknown":
        return False, (
            f"Could not read which user {image} runs as: it is not on this host and could not be "
            "pulled or built, so nothing was recreated."
            + (f"\n{detail[-1500:]}" if detail else "")
        )
    if kind == "root":
        return True, ""
    if kind == "named":
        return False, (
            f"{image} runs as a named user, so Composer cannot tell which uid needs access to "
            "the app's data. Use a numeric USER uid:gid in the Dockerfile (DjangoLux images use 10001:0)."
        )
    try:
        scan = run(launcher, "scan", uid=uid, gid=gid)
        if not totals(scan)["wrong"] and not scan["missing"]:
            return True, ""
        summary = run(launcher, "apply", uid=uid, gid=gid)
    except PermissionsError as exc:
        return False, f"Could not prepare the app's data for uid {uid} (gid {gid}): {exc}"
    counts = totals(summary)
    if counts["errors"] or not summary.get("ok", True):
        return False, (
            f"Could not give uid {uid} (gid {gid}) access to {counts['errors']} entr"
            f"{'y' if counts['errors'] == 1 else 'ies'} of the app's data; nothing was recreated."
        )
    note = f"App data ready for uid {uid} (gid {gid})"
    if counts["changed"]:
        note += f": granted group access to {counts['changed']} entries (manifest {summary['manifest']})"
    if counts["hardlinks"]:
        note += f"; {counts['hardlinks']} multiply-linked file(s) left unchanged"
    return True, note + "."


def run_cli(argv: List[str]) -> int:
    """``composer app-permissions revert <manifest>``, and the sibling actions."""
    if "--spec" in argv:
        return run_sibling(argv)
    parser = argparse.ArgumentParser(
        prog="composer app-permissions",
        description="Undo a group-access grant from its manifest. To grant access, run 'composer check --fix'.",
    )
    parser.add_argument("action", choices=["revert"])
    parser.add_argument("manifest", help="app-permissions.tsv.gz written by a grant")
    parser.add_argument("-f", "--file", help="Compose file")
    parser.add_argument("-d", "--dev", action="store_true", help="Development compose files")
    args = parser.parse_args(argv)

    from .launcher import DockerComposeLauncher

    launcher = DockerComposeLauncher()
    launcher.compose_file = args.file
    launcher.dev_mode = args.dev
    launcher.resolve_active_compose_files()
    launcher.resolve_secrets()
    try:
        summary = run(launcher, "revert", revert_manifest=args.manifest)
    except PermissionsError as exc:
        print(f"✖ {exc}", file=sys.stderr)
        return 1
    counts = totals(summary)
    print(f"✔ Restored {counts['reverted']} entries; {counts['skipped']} changed since the grant and were left alone.")
    return 1 if counts["errors"] else 0


