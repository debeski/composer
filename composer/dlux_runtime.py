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
generation semantics are DjangoLux's runtime contract (``dlux/contracts/runtime.json``,
printed by ``manage.py dlux_runtime_contract``; the reader is
``dlux/updater/runtime.py``), and ``tests/test_dlux_runtime.py`` checks this module
against it. Do not "improve" them here — a change has to land on both sides, gated
by the contract's ``schema_version`` (``CONTRACT_SCHEMA_VERSION``).
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import safe_files
from .app_permissions import SHARED_FILE_MODE
from .versions import try_parse

CONTRACT_SCHEMA_VERSION = 1

# Mirrors dlux/updater/runtime.py::_VERSION_DIR_RE. A version becomes a
# directory name, so it is validated rather than trusted.
_VERSION_DIR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
VALID_SOURCES = frozenset({"image", "volume"})
# Under state/: held by `flock` for the whole of an update or rollback. The
# deployer CLI (in its sibling container) and the resident executor mount the
# same volume, so an in-process lock would not see the other one.
PACKAGE_LOCK_FILENAME = "package-operation.lock"
QUARANTINE_REASON_FILENAME = "quarantine-reason.txt"


def version_sort_key(version) -> tuple:
    """PEP 440 ordering for a release version, total over unparseable input.

    The tuple's first element separates "could not be parsed" from "parsed", so
    an unreadable directory name sorts below every real release instead of
    raising in the middle of a rollback.

    This used to split on punctuation and score every non-numeric part as -1,
    which collapsed ``1.9.0b1``, ``1.9.0b2`` and ``1.9.0`` onto one key. That is
    harmless while only stable releases are staged and wrong the moment a beta
    is: the rollback target search looks for a release *strictly below* the
    active one, and a tie is not below, so a rollback from 1.9.0 would step past
    its own beta to the image copy. The prune has the same tie.
    """
    parsed = try_parse(version)
    return (0,) if parsed is None else (1, parsed)


class DluxRuntimeError(RuntimeError):
    """The runtime volume is not in a state Composer can safely act on."""


class PackageOperationBusy(DluxRuntimeError):
    """Another update or rollback holds the runtime volume's operation lock."""


#: What an operator runs to recover from an unusable ``active.json``.
POINTER_RECOVERY_HINT = (
    "Run './start.sh dlux rollback' to set it aside and return to the DjangoLux "
    "release baked into the image; DjangoLux then rebuilds the pointer."
)


class UnusablePointer(DluxRuntimeError):
    """``state/active.json`` exists but cannot be used; ``set_aside_pointer`` recovers."""

    def __init__(self, problem: str):
        self.problem = problem
        super().__init__(f"{problem} {POINTER_RECOVERY_HINT}")


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
        os.fchmod(handle, SHARED_FILE_MODE)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def normalize_version(value) -> str:
    """Validate a version that is about to become a directory name.

    Two independent gates, both required: it must be a real PEP 440 version, and
    it must be safe as a path segment. The string is returned unchanged rather
    than canonicalised — the directory on the volume was created under the name
    the release carries, and rewriting ``1.9.0b1`` to some other spelling here
    would look for a path that does not exist.
    """
    raw = str(value or "").strip()
    if not raw or try_parse(raw) is None or not _VERSION_DIR_RE.fullmatch(raw):
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
            return max(0, int(safe_files.read_bounded(self.generation_file, 64).decode("ascii").strip()))
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
        if not (self.active_file.exists() or self.active_file.is_symlink()):
            return {}
        try:
            payload = json.loads(safe_files.read_bounded(self.active_file).decode("utf-8"))
        except (OSError, ValueError, RecursionError) as exc:
            raise UnusablePointer("state/active.json is unreadable or not JSON.") from exc
        if not isinstance(payload, dict):
            raise UnusablePointer("state/active.json is not a JSON object.")
        source = payload.get("source")
        if source not in VALID_SOURCES:
            raise UnusablePointer(f"state/active.json has an invalid source: {str(source)[:40]!r}.")
        try:
            version = normalize_version(payload.get("version"))
        except DluxRuntimeError as exc:
            raise UnusablePointer(f"state/active.json names {exc}.") from exc
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
                raise UnusablePointer(f"state/active.json points at a missing release: {version}.")
            result["path"] = str(release)
        return result

    def set_aside_pointer(self) -> Optional[Path]:
        """Move an unusable ``active.json`` aside, keep it, and restart on the image.

        The file is renamed (never deleted) to ``active.json.unusable-<UTC time>``
        and the generation bumped, so every supervisor serves the baked package and
        DjangoLux's reconcile rebuilds the pointer from the release it recorded.
        """
        if not (self.active_file.exists() or self.active_file.is_symlink()):
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = self.state_dir / f"{self.active_file.name}.unusable-{stamp}"
        suffix = 1
        while target.exists() or target.is_symlink():
            suffix += 1
            target = self.state_dir / f"{self.active_file.name}.unusable-{stamp}-{suffix}"
        os.replace(self.active_file, target)
        self.bump_generation()
        return target

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
                normalized.get("stack_schema_reason")
                or f"Release {version} may not be applied inline; refusing to activate it."
            )
        return normalized

    def quarantine(self, version, reason="") -> Optional[Path]:
        """Move a release out of ``releases/`` so it cannot be activated again.

        An earlier record of the same version (a retry that failed again) is set
        aside for audit, never deleted.
        """
        version = normalize_version(version)
        release = self.release_path(version)
        if not release.is_dir():
            return None
        self.failed.mkdir(parents=True, exist_ok=True)
        target = self.failed / version
        self.set_aside_quarantine(version)
        shutil.move(str(release), str(target))
        if reason:
            _atomic_write(target / QUARANTINE_REASON_FILENAME, reason.strip() + "\n")
        return target

    def quarantined_reason(self, version) -> Optional[str]:
        """Why ``version`` sits quarantined under ``failed/``; None when it does not.

        ``''`` is a quarantine without a recorded reason. DjangoLux archives its
        own leftovers in ``failed/`` too, under names that are not versions.
        """
        try:
            record = self.failed / normalize_version(version)
        except DluxRuntimeError:
            return None
        if not record.is_dir():
            return None
        try:
            return (record / QUARANTINE_REASON_FILENAME).read_text(
                encoding="utf-8", errors="replace"
            ).strip()
        except OSError:
            return ""

    def quarantined_versions(self) -> list:
        """Releases quarantined after a failed update, oldest first."""
        if not self.failed.is_dir():
            return []
        found = [
            entry.name for entry in self.failed.iterdir()
            if entry.is_dir() and _VERSION_DIR_RE.fullmatch(entry.name)
            and try_parse(entry.name) is not None
        ]
        return sorted(found, key=version_sort_key)

    def set_aside_quarantine(self, version, label="retried") -> Optional[Path]:
        """Move ``failed/<version>`` to ``failed/<version>.<label>-<UTC time>``.

        That name is not a version, so the release stops counting as
        quarantined while its record stays on the volume.
        """
        record = self.failed / normalize_version(version)
        if not (record.exists() or record.is_symlink()):
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = self.failed / f"{record.name}.{label}-{stamp}"
        suffix = 1
        while target.exists() or target.is_symlink():
            suffix += 1
            target = self.failed / f"{record.name}.{label}-{stamp}-{suffix}"
        record.rename(target)
        return target

    # -- exclusion ------------------------------------------------------

    @contextmanager
    def package_operation_lock(self):
        """Hold the update/rollback lock, or raise `PackageOperationBusy` at once.

        Never waits: a second operation started while one runs is refused
        before it touches anything. The lock goes with the file descriptor, so
        it is released on every exit, including a crash.
        """
        import fcntl

        path = self.state_dir / PACKAGE_LOCK_FILENAME
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            # flock needs no write access; O_NOFOLLOW refuses a symlink planted
            # by another writer of this volume.
            fd = os.open(path, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, SHARED_FILE_MODE)
        except OSError as exc:
            raise DluxRuntimeError(
                f"Could not open the DjangoLux update lock {path}: "
                f"{exc.strerror or exc}; nothing was changed."
            ) from exc
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    raise PackageOperationBusy(
                        "Another DjangoLux update or rollback is already running; "
                        "nothing was changed."
                    ) from exc
                raise DluxRuntimeError(
                    f"Could not take the DjangoLux update lock {path}: "
                    f"{exc.strerror or exc}; nothing was changed."
                ) from exc
            try:
                yield path
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

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
