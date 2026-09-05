"""Composer's writer for the DjangoLux runtime volume.

DjangoLux inline updates work by staging a release under
``/opt/dlux-runtime/releases/<version>/`` and pointing ``state/active.json`` at
it; the supervisor baked into the project image puts that directory on
``PYTHONPATH`` at container start. Historically DjangoLux staged and activated
its own releases. This module lets Composer do it instead, so the framework
needs no outbound network access and so the swap is supervised from *outside*
the container being swapped.

`composer-executor` already mounts ``dlux_runtime:/opt/dlux-runtime:rw``, so no
Compose change is required.

**This is a cross-repo contract.** The layout, the ``active.json`` schema and the
generation semantics are mirrored from ``dlux/updater/runtime.py``. Do not
"improve" them here — a change has to land on both sides, gated by
``schema_version`` (see docs/updater-consolidation.md).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Optional

CONTRACT_SCHEMA_VERSION = 1

# Mirrors dlux/updater/runtime.py::_VERSION_DIR_RE. A version becomes a
# directory name, so it is validated rather than trusted.
_VERSION_DIR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_SIMPLE_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)*(?:[.-]?(?:a|b|rc|post|dev)[0-9]+)?$")

VALID_SOURCES = frozenset({"image", "volume"})


def version_sort_key(version) -> tuple:
    """Numeric ordering for a release version. Mirrors dlux_release_source's
    candidate sort; a non-numeric part sorts below any number (1.9.0rc1 < 1.9.0).
    """
    return tuple(
        int(part) if part.isdigit() else -1
        for part in re.split(r"[._-]", str(version or ""))
    )


class DluxRuntimeError(RuntimeError):
    """The runtime volume is not in a state Composer can safely act on."""


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, then rename.

    The supervisor may read these files at any moment, including while a swap is
    in flight. A partial ``active.json`` would send the next container start to a
    release that does not exist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def normalize_version(value) -> str:
    raw = str(value or "").strip()
    if not raw or not _SIMPLE_VERSION_RE.fullmatch(raw) or not _VERSION_DIR_RE.fullmatch(raw):
        raise DluxRuntimeError(f"Invalid DjangoLux version: {raw!r}")
    return raw


class DluxRuntime:
    """Read/write access to one project's DjangoLux runtime volume."""

    def __init__(self, root="/opt/dlux-runtime"):
        self.root = Path(root).expanduser().resolve()
        self.releases = self.root / "releases"
        self.staging = self.root / "staging"
        self.downloads = self.root / "downloads"
        self.failed = self.root / "failed"
        self.state_dir = self.root / "state"
        self.active_file = self.state_dir / "active.json"
        self.generation_file = self.state_dir / "generation"

    # -- layout ---------------------------------------------------------

    def exists(self) -> bool:
        return self.root.is_dir()

    def release_path(self, version) -> Path:
        return self.releases / normalize_version(version)

    def staged_versions(self) -> list:
        """Staged releases, oldest first — by version, not by string.

        `sorted()` alone puts "1.8.10" *below* "1.8.9", which is wrong the first
        time a two-digit patch is staged: the rollback would pick the wrong
        target and the prune would drop the wrong release.
        """
        if not self.releases.is_dir():
            return []
        found = []
        for entry in self.releases.iterdir():
            if entry.is_dir() and _VERSION_DIR_RE.fullmatch(entry.name):
                found.append(entry.name)
        return sorted(found, key=version_sort_key)

    # -- generation -----------------------------------------------------

    def read_generation(self) -> int:
        try:
            return max(0, int(self.generation_file.read_text(encoding="utf-8").strip()))
        except (OSError, ValueError):
            return 0

    def bump_generation(self) -> int:
        generation = self.read_generation() + 1
        _atomic_write(self.generation_file, f"{generation}\n")
        return generation

    # -- active pointer -------------------------------------------------

    def read_active(self) -> dict:
        """Current activation, or ``{}`` when the image release is in force.

        Never raises for a *missing* file — an unmanaged volume legitimately has
        none. It does raise for a file that exists but is unusable, because
        silently treating that as "no release" would mask a broken swap.
        """
        if not self.active_file.exists():
            return {}
        try:
            payload = json.loads(self.active_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DluxRuntimeError("active.json is unreadable or not JSON.") from exc
        if not isinstance(payload, dict):
            raise DluxRuntimeError("active.json is not a JSON object.")
        source = payload.get("source")
        if source not in VALID_SOURCES:
            raise DluxRuntimeError(f"active.json has an invalid source: {source!r}")
        version = normalize_version(payload.get("version"))
        result = {
            "version": version,
            "source": source,
            "path": str(payload.get("path") or ""),
            "generation": int(payload.get("generation") or self.read_generation()),
        }
        if source == "volume":
            release = self.release_path(version).resolve()
            try:
                release.relative_to(self.releases.resolve())
            except ValueError as exc:
                raise DluxRuntimeError("active.json points outside releases/.") from exc
            if not release.is_dir():
                raise DluxRuntimeError(f"active.json points at a missing release: {version}")
            result["path"] = str(release)
        return result

    def write_active(self, version, *, source="volume", generation=None) -> dict:
        version = normalize_version(version)
        if source not in VALID_SOURCES:
            raise DluxRuntimeError(f"Invalid runtime source: {source!r}")
        path = ""
        if source == "volume":
            release = self.release_path(version)
            if not release.is_dir():
                raise DluxRuntimeError(f"Release {version} is not staged; refusing to activate it.")
            path = str(release)
        payload = {
            "version": version,
            "source": source,
            "path": path,
            "generation": self.read_generation() if generation is None else int(generation),
        }
        _atomic_write(self.active_file, json.dumps(payload, sort_keys=True) + "\n")
        return payload

    # -- staging --------------------------------------------------------

    def stage_release(self, version, source_dir, *, overwrite=False) -> Path:
        """Move an unpacked release into ``releases/<version>/``.

        Staging is deliberately separate from activation: a release is put in
        place and verified *before* anything points at it, so a failure here
        never affects the running deployment.
        """
        version = normalize_version(version)
        source = Path(source_dir)
        if not (source / "dlux").is_dir():
            raise DluxRuntimeError("Staged directory does not contain a 'dlux' package.")
        target = self.release_path(version)
        if target.exists():
            if not overwrite:
                return target
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        return target

    def verify_release(self, version) -> dict:
        """Check a staged release before it is allowed to become active.

        Reads the release manifest the wheel carries and confirms it describes
        the version the directory claims. A mismatch means the artifact is not
        what was asked for, which must fail *before* activation, not after a
        restart.
        """
        version = normalize_version(version)
        release = self.release_path(version)
        if not release.is_dir():
            raise DluxRuntimeError(f"Release {version} is not staged.")
        manifest_path = release / "dlux" / "release-manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DluxRuntimeError(f"Release {version} has no readable release manifest.") from exc
        declared = str(manifest.get("version") or "").strip()
        if declared != version:
            raise DluxRuntimeError(
                f"Release directory {version} contains DjangoLux {declared or 'unknown'}."
            )
        # Inline safety is schema-dependent: schema 1 declares `inline_safe`
        # outright, schema 2 derives it from install/migrations and never carries
        # the key. Reading it directly refused every schema-2 release here, after
        # the wheel had already been fetched, verified and staged. Defer to the
        # one normalizer both halves of the update path already use.
        from .dlux_release_source import ReleaseSourceError, normalize_manifest

        try:
            normalized = normalize_manifest(manifest, version)
        except ReleaseSourceError as exc:
            raise DluxRuntimeError(str(exc)) from exc
        if not normalized.get("inline_safe"):
            raise DluxRuntimeError(
                f"Release {version} may not be applied inline; refusing to activate it."
            )
        return normalized

    def quarantine(self, version, reason="") -> Optional[Path]:
        """Move a release out of ``releases/`` so it cannot be activated again."""
        version = normalize_version(version)
        release = self.release_path(version)
        if not release.is_dir():
            return None
        self.failed.mkdir(parents=True, exist_ok=True)
        target = self.failed / version
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(release), str(target))
        if reason:
            _atomic_write(target / "quarantine-reason.txt", reason.strip() + "\n")
        return target

    # -- the swap -------------------------------------------------------

    def activate(self, version) -> dict:
        """Verify, then point the runtime at ``version`` and bump the generation.

        Returns the previous activation so a caller can roll back to it.
        """
        self.verify_release(version)
        previous = self.read_active()
        generation = self.bump_generation()
        self.write_active(version, source="volume", generation=generation)
        return previous

    def restore(self, previous: dict) -> dict:
        """Undo an activation, given the dict ``activate()`` returned.

        An empty dict means "there was no volume release" — the image copy was in
        force — so the pointer is removed rather than rewritten.
        """
        generation = self.bump_generation()
        if not previous:
            if self.active_file.exists():
                self.active_file.unlink()
            return {}
        return self.write_active(
            previous["version"],
            source=previous.get("source") or "volume",
            generation=generation,
        )
