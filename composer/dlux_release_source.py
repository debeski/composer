"""Fetching a DjangoLux release from PyPI, for Composer to stage.

This is the half of the inline updater that talks to the network. It is mirrored
from ``dlux/updater/manifest.py`` on purpose: the trust decisions — which hosts
may serve a download, which digest must match, which repository must have signed
the artifact — are the ones DjangoLux already makes. Moving the code must not
quietly relax them.

Every failure path is closed. If the attestation verifier is unavailable, the
download is refused rather than accepted unverified; an updater that degrades to
"unsigned is fine" when a dependency is missing is worse than no updater.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlparse

# Mirrored from dlux/updater/manifest.py — keep in step.
PYPI_SIMPLE_URL = "https://pypi.org/simple/django-lux/"
PYPI_PROJECT_REPOSITORY = "https://github.com/debeski/django-lux"
ALLOWED_DOWNLOAD_HOSTS = frozenset({"pypi.org", "files.pythonhosted.org"})
MAX_INDEX_BYTES = 4 * 1024 * 1024
MAX_WHEEL_BYTES = 64 * 1024 * 1024
MANIFEST_PATH = "dlux/release-manifest.json"

_HREF_RE = re.compile(r'<a\s[^>]*href="([^"]+)"[^>]*>([^<]+)</a>', re.IGNORECASE)
_WHEEL_RE = re.compile(r"^django_lux-([0-9][^-]*)-py3-none-any\.whl$", re.IGNORECASE)
_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)*(?:[.-]?(?:a|b|rc|post|dev)[0-9]+)?$")


class ReleaseSourceError(RuntimeError):
    """The release could not be fetched or could not be trusted."""


@dataclass(frozen=True)
class ReleaseCandidate:
    version: str
    filename: str
    url: str
    sha256: str

    def as_dict(self) -> dict:
        return asdict(self)


def _validated_https_url(url: str) -> str:
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_DOWNLOAD_HOSTS:
        raise ReleaseSourceError("The update source returned an unapproved download URL.")
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ReleaseSourceError("The update source returned an unsafe download URL.")
    return parsed.geturl()


def _read_bounded(response, limit: int) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared:
        try:
            if int(declared) > limit:
                raise ReleaseSourceError("The update source returned an oversized response.")
        except ValueError:
            pass
    payload = response.read(limit + 1)
    if len(payload) > limit:
        raise ReleaseSourceError("The update source returned an oversized response.")
    return payload


def fetch_index(*, opener=urllib.request.urlopen) -> list:
    """Every published wheel, newest last. Digest comes from the index fragment."""
    request = urllib.request.Request(
        PYPI_SIMPLE_URL, headers={"User-Agent": "composer-dlux-updater/1"}
    )
    try:
        with opener(request, timeout=30) as response:
            _validated_https_url(response.geturl())
            payload = _read_bounded(response, MAX_INDEX_BYTES).decode("utf-8", "replace")
    except ReleaseSourceError:
        raise
    except Exception as exc:
        raise ReleaseSourceError("Could not read the DjangoLux release index.") from exc

    candidates = []
    for href, text in _HREF_RE.findall(payload):
        filename = text.strip()
        match = _WHEEL_RE.match(filename)
        if not match:
            continue
        version = match.group(1)
        if not _VERSION_RE.fullmatch(version):
            continue
        url, _, fragment = href.partition("#")
        digest = ""
        if fragment.startswith("sha256="):
            digest = fragment[len("sha256="):].strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            # No digest means nothing to verify the download against.
            continue
        try:
            url = _validated_https_url(url)
        except ReleaseSourceError:
            continue
        candidates.append(ReleaseCandidate(version=version, filename=filename, url=url, sha256=digest))
    return candidates


def select_candidate(candidates, target_version="") -> ReleaseCandidate:
    """Pin to ``target_version`` when given, otherwise take the newest release."""
    if not candidates:
        raise ReleaseSourceError("No DjangoLux release is available to install.")
    if target_version:
        for candidate in candidates:
            if candidate.version == target_version:
                return candidate
        raise ReleaseSourceError(f"DjangoLux {target_version} is not published.")

    def sort_key(candidate):
        parts = re.split(r"[._-]", candidate.version)
        return tuple(int(p) if p.isdigit() else -1 for p in parts)

    stable = [c for c in candidates if _VERSION_RE.fullmatch(c.version) and not re.search(
        r"(a|b|rc|dev)[0-9]+$", c.version)]
    pool = stable or list(candidates)
    return sorted(pool, key=sort_key)[-1]


def verify_attestation(candidate: ReleaseCandidate, *, runner=subprocess.run) -> None:
    """Refuse the artifact unless PyPI's Trusted Publisher attestation checks out.

    Fails closed: a missing verifier is a refusal, never a pass. Mirrors
    ``dlux.updater.manifest.verify_pypi_attestation``.
    """
    if importlib.util.find_spec("pypi_attestations") is None:
        raise ReleaseSourceError(
            "PyPI attestation verification is unavailable in this Composer image; "
            "refusing to install an unverified DjangoLux wheel."
        )
    try:
        completed = runner(
            [
                sys.executable, "-m", "pypi_attestations", "verify", "pypi",
                "--repository", PYPI_PROJECT_REPOSITORY, candidate.url,
            ],
            check=False, capture_output=True, text=True, timeout=120,
        )
    except Exception as exc:
        raise ReleaseSourceError("Could not verify the wheel's PyPI attestation.") from exc
    if completed.returncode != 0:
        raise ReleaseSourceError("The wheel's PyPI Trusted Publisher attestation is invalid.")


def download_wheel(candidate: ReleaseCandidate, destination, *, opener=urllib.request.urlopen) -> Path:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        candidate.url, headers={"User-Agent": "composer-dlux-updater/1"}
    )
    try:
        with opener(request, timeout=60) as response:
            _validated_https_url(response.geturl())
            payload = _read_bounded(response, MAX_WHEEL_BYTES)
    except ReleaseSourceError:
        raise
    except Exception as exc:
        raise ReleaseSourceError("Could not download the DjangoLux update wheel.") from exc
    actual = hashlib.sha256(payload).hexdigest()
    if actual != candidate.sha256:
        raise ReleaseSourceError("The downloaded wheel does not match PyPI's SHA-256 digest.")
    temporary = destination.with_name(f".{destination.name}.part")
    temporary.write_bytes(payload)
    temporary.replace(destination)
    return destination


def read_wheel_manifest(wheel_path) -> dict:
    """The release manifest carried inside the wheel."""
    try:
        with zipfile.ZipFile(wheel_path) as archive:
            raw = archive.read(MANIFEST_PATH)
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise ReleaseSourceError("The wheel does not carry a DjangoLux release manifest.") from exc
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ReleaseSourceError("The wheel's release manifest is not valid JSON.") from exc
    if not isinstance(manifest, dict):
        raise ReleaseSourceError("The wheel's release manifest is not an object.")
    return manifest


def assess(candidate: ReleaseCandidate, wheel_path) -> dict:
    """Decide whether this wheel may be applied inline.

    `inline_safe` is the release's own declaration; Composer honours it rather
    than second-guessing it. A release that requires an image rebuild is refused
    here, exactly as DjangoLux refuses it today.
    """
    manifest = read_wheel_manifest(wheel_path)
    declared = str(manifest.get("version") or "").strip()
    if declared != candidate.version:
        raise ReleaseSourceError(
            f"The wheel for {candidate.version} declares version {declared or 'unknown'}."
        )
    if not isinstance(manifest.get("inline_safe"), bool):
        raise ReleaseSourceError("The release manifest does not declare inline_safe.")
    if not manifest["inline_safe"]:
        raise ReleaseSourceError(
            f"DjangoLux {candidate.version} requires a project image rebuild "
            "(inline_safe is false); it cannot be applied to the runtime volume."
        )
    return manifest


def describe(target_version="", *, workdir=None, opener=urllib.request.urlopen,
             runner=subprocess.run) -> dict:
    """Resolve and inspect the newest (or pinned) release without installing it.

    Downloads and verifies the wheel — the manifest inside it is the only
    authority on `inline_safe`, and a published version number alone cannot say
    whether a release may be applied to the runtime volume. Nothing is unpacked
    and nothing is activated.
    """
    candidate = select_candidate(fetch_index(opener=opener), target_version)
    workdir = Path(workdir or tempfile.mkdtemp(prefix="composer-dlux-check-"))
    workdir.mkdir(parents=True, exist_ok=True)
    verify_attestation(candidate, runner=runner)
    wheel = download_wheel(candidate, workdir / candidate.filename, opener=opener)
    manifest = read_wheel_manifest(wheel)
    declared = str(manifest.get("version") or "").strip()
    if declared != candidate.version:
        raise ReleaseSourceError(
            f"The wheel for {candidate.version} declares version {declared or 'unknown'}."
        )
    inline_safe = bool(manifest.get("inline_safe"))
    return {
        "version": candidate.version,
        "filename": candidate.filename,
        "sha256": candidate.sha256,
        "inline_safe": inline_safe,
        "reason": "" if inline_safe else (
            f"DjangoLux {candidate.version} requires a project image rebuild."
        ),
        "manifest": manifest,
    }


def unpack(wheel_path, destination) -> Path:
    """Extract a wheel into a fresh directory, refusing any path that escapes it."""
    destination = Path(destination)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    root = destination.resolve()
    with zipfile.ZipFile(wheel_path) as archive:
        for member in archive.namelist():
            target = (root / member).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise ReleaseSourceError(f"The wheel contains an unsafe path: {member}") from exc
        archive.extractall(root)
    if not (root / "dlux").is_dir():
        raise ReleaseSourceError("The wheel does not contain a 'dlux' package.")
    return destination


def obtain(target_version="", *, workdir=None, opener=urllib.request.urlopen,
           runner=subprocess.run) -> tuple:
    """Resolve, verify and unpack a release. Returns ``(candidate, unpacked_dir)``.

    Order matters: attestation and digest are checked before the archive is
    opened, and `inline_safe` before anything is unpacked into place.
    """
    candidate = select_candidate(fetch_index(opener=opener), target_version)
    workdir = Path(workdir or tempfile.mkdtemp(prefix="composer-dlux-"))
    workdir.mkdir(parents=True, exist_ok=True)
    verify_attestation(candidate, runner=runner)
    wheel = download_wheel(candidate, workdir / candidate.filename, opener=opener)
    assess(candidate, wheel)
    unpacked = unpack(wheel, workdir / "unpacked")
    return candidate, unpacked
