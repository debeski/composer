"""Version contract for the `start.sh` / `start.ps1` launcher wrappers.

Composer owns both wrappers. Every line in them is composer's own invocation
contract — the self image, `-i`/`-t`, the `--env-file` secrets handoff, the
`update-self` route — and composer is the only component that keeps running in
a project after creation: the DLUX scaffold writes them once and then refuses
to touch them (`_write_rendered` will not overwrite). The copies under DLUX's
`scaffold_templates/project/` are mirrors of these files, not the source; they
carry no `{{ }}` placeholders, so the comparison here is byte-exact.

The wrapper version is a plain integer bumped only when the wrapper bytes
change, deliberately decoupled from composer's own release version: composer
ships far more often than the wrapper does, and a marker tracking it would
report every project stale after every release until nobody read the warning.

`wrappers-history.json` records the sha256 of every published version, which is
what separates "old but pristine" (safe to replace) from "locally edited" (the
operator has to see it first).
"""

import hashlib
import json
import os
import re
import shutil
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional

WRAPPER_NAMES = ("start.sh", "start.ps1")

# Both bash and PowerShell take `#` comments, so one marker form covers both.
MARKER_PATTERN = re.compile(r"^#\s*composer-wrapper:\s*(\d+)\s*$", re.MULTILINE)

# Baked into the image next to the code that checks them, so a deployment
# verifies its wrapper against the composer it is actually running — no
# registry call, which matters on an air-gapped server.
DEFAULT_BAKED_ROOT = Path("/app/wrappers")
HISTORY_NAME = "wrappers-history.json"

CURRENT = "current"
STALE = "stale"
MODIFIED = "modified"
AHEAD = "ahead"
UNVERSIONED = "unversioned"
MISSING = "missing"

# Statuses `check --fix` may resolve by writing the baked copy.
FIXABLE = (STALE, UNVERSIONED, MISSING, MODIFIED)


def read_marker(text: str) -> Optional[int]:
    match = MARKER_PATTERN.search(text)
    return int(match.group(1)) if match else None


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def baked_root(override: Optional[str] = None) -> Optional[Path]:
    """Directory holding the reference wrappers, or None when unavailable.

    Absent when composer runs from a source checkout rather than the image; the
    check degrades to a skip instead of inventing a reference.
    """
    candidate = Path(override or os.environ.get("COMPOSER_WRAPPERS_DIR") or DEFAULT_BAKED_ROOT)
    return candidate if candidate.is_dir() else None


def load_history(root: Path) -> Dict[str, Dict[str, str]]:
    try:
        document = json.loads((root / HISTORY_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    history = document.get("history")
    return history if isinstance(history, dict) else {}


def inspect_wrapper(
    project_root: Path,
    name: str,
    root: Path,
    history: Dict[str, Dict[str, str]],
) -> Optional[Dict[str, Any]]:
    """Compare one project wrapper against the baked reference.

    Returns None when the reference itself is absent — nothing to compare
    against is not the project's problem.
    """
    reference = root / name
    if not reference.is_file():
        return None

    reference_bytes = reference.read_bytes()
    baked_version = read_marker(reference_bytes.decode("utf-8", "replace"))
    entry: Dict[str, Any] = {"name": name, "baked_version": baked_version, "version": None}

    target = project_root / name
    if not target.is_file():
        entry["status"] = MISSING
        return entry

    payload = target.read_bytes()
    if payload == reference_bytes:
        entry["status"] = CURRENT
        entry["version"] = baked_version
        return entry

    version = read_marker(payload.decode("utf-8", "replace"))
    entry["version"] = version
    if version is None:
        entry["status"] = UNVERSIONED
    elif baked_version is not None and version > baked_version:
        entry["status"] = AHEAD
    elif version == baked_version:
        # Same declared version, different bytes: someone edited it locally.
        entry["status"] = MODIFIED
    else:
        # Older marker, but only pristine if it hashes to what that version
        # actually shipped; otherwise it is an edited copy of an old version.
        known = (history.get(name) or {}).get(str(version))
        entry["status"] = STALE if known == sha256_bytes(payload) else MODIFIED
    return entry


def inspect_wrappers(project_root: str = ".", override: Optional[str] = None) -> List[Dict[str, Any]]:
    root = baked_root(override)
    if root is None:
        return []
    base = Path(project_root)
    # A directory with no wrapper at all is not a composer-launched project
    # (running from a source checkout, for one), so stay quiet rather than
    # demand two files nobody asked for.
    if not any((base / name).is_file() for name in WRAPPER_NAMES):
        return []
    history = load_history(root)
    found = [inspect_wrapper(base, name, root, history) for name in WRAPPER_NAMES]
    return [entry for entry in found if entry is not None]


def install_wrapper(
    project_root: Path,
    name: str,
    root: Path,
    archive_dir: Optional[Path] = None,
) -> None:
    """Replace one wrapper with the baked copy, atomically.

    `os.replace` swaps the inode, so a `start.sh` that is *currently executing*
    this very check keeps reading the file it was launched from — truncating in
    place would corrupt the running shell, which reads the script by offset.
    The previous file is archived first; nothing is deleted.
    """
    destination = project_root / name
    original = destination.stat() if destination.is_file() else None
    if original is not None and archive_dir is not None:
        archive_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(destination, archive_dir / name)

    staged = destination.with_name(f"{name}.composer-new")
    shutil.copyfile(root / name, staged)
    if original is not None:
        mode = stat.S_IMODE(original.st_mode)
        if name.endswith(".sh"):
            # Preserving the old mode must not preserve a *broken* one: a
            # non-executable start.sh cannot be run as `./start.sh` at all.
            mode |= 0o111
        os.chmod(staged, mode)
        # Written by root inside the container onto a bind mount; without this
        # the host owner loses write access to their own wrapper.
        try:
            os.chown(staged, original.st_uid, original.st_gid)
        except (OSError, AttributeError):
            pass
    elif name.endswith(".sh"):
        os.chmod(staged, 0o755)
    os.replace(staged, destination)
