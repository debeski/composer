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

from . import dlux_channel
from .version import read_composer_version
from .versions import at_least, try_parse

# Mirrored from dlux/updater/manifest.py — keep in step.
PYPI_SIMPLE_URL = "https://pypi.org/simple/django-lux/"
PYPI_PROJECT_REPOSITORY = "https://github.com/debeski/django-lux"
ALLOWED_DOWNLOAD_HOSTS = frozenset({"pypi.org", "files.pythonhosted.org"})
MAX_INDEX_BYTES = 4 * 1024 * 1024
MAX_WHEEL_BYTES = 64 * 1024 * 1024
MANIFEST_PATH = "dlux/release-manifest.json"
SAFE_INLINE_EFFECTS = frozenset({"none", "state_only", "additive"})
# Fails closed: a requirement this Composer cannot evaluate refuses the release
# rather than ignoring the constraint. That is why every key DjangoLux may
# publish has to be listed here — `migration_baseline` was added by DjangoLux
# 1.8.14, and until it appeared here every Composer rejected that manifest
# outright. `migration_baseline` is informational to Composer (DjangoLux uses it
# to warn that an update spans migrations an intermediate release introduced);
# recognising it is what makes the release installable at all.
KNOWN_REQUIREMENT_KEYS = frozenset({
    "baked_image", "updater_schema", "services", "migration_baseline",
})

_HREF_RE = re.compile(r'<a\s[^>]*href="([^"]+)"[^>]*>([^<]+)</a>', re.IGNORECASE)
_WHEEL_RE = re.compile(r"^django_lux-([0-9][^-]*)-py3-none-any\.whl$", re.IGNORECASE)


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
        if try_parse(version) is None:
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


def select_candidate(candidates, target_version="", *, channel=dlux_channel.STABLE) -> ReleaseCandidate:
    """Pin to ``target_version`` when given, otherwise the newest eligible release.

    ``channel`` decides only whether prereleases are eligible. On stable there is
    deliberately no "fall back to a prerelease when no stable exists": a
    deployment that never opted in must get "nothing available", not a beta.

    An explicit ``target_version`` is honoured on either channel. That is not a
    hole — pinning is how a rollback and a `--version` install work, both of
    which are already deliberate operator acts naming an exact release.
    """
    if not candidates:
        raise ReleaseSourceError("No DjangoLux release is available to install.")
    if target_version:
        wanted = try_parse(target_version)
        for candidate in candidates:
            if candidate.version == target_version:
                return candidate
            parsed = try_parse(candidate.version)
            if wanted is not None and parsed is not None and parsed == wanted:
                return candidate
        raise ReleaseSourceError(f"DjangoLux {target_version} is not published.")

    allow_prereleases = dlux_channel.prereleases_allowed(channel)
    pool = []
    for candidate in candidates:
        parsed = try_parse(candidate.version)
        if parsed is None or parsed.is_devrelease:
            continue
        if parsed.is_prerelease and not allow_prereleases:
            continue
        pool.append((parsed, candidate))
    if not pool:
        raise ReleaseSourceError(
            "No DjangoLux release is available to install on the "
            f"{dlux_channel.normalize_channel(channel)} channel."
        )
    return max(pool, key=lambda item: item[0])[1]


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


def _version_at_least(current, requirement) -> bool:
    """PEP 440 comparison against a ``">=X.Y.Z"`` floor.

    The regex this replaced read the leading release segment, so a Composer
    running ``1.3.14b1`` satisfied ``>=1.3.14`` — it would have installed a
    DjangoLux release that needs a fix its own beta predates.
    """
    return at_least(current, requirement)


def normalize_manifest(manifest, expected_version) -> dict:
    if not isinstance(manifest, dict):
        raise ReleaseSourceError("The release manifest is not an object.")
    if str(manifest.get("version") or "").strip() != expected_version:
        raise ReleaseSourceError(
            f"The wheel for {expected_version} declares version "
            f"{manifest.get('version') or 'unknown'}."
        )

    schema = manifest.get("schema_version")
    if schema == 1:
        if not isinstance(manifest.get("inline_safe"), bool):
            raise ReleaseSourceError("The release manifest does not declare inline_safe.")
        return dict(manifest)
    if schema != 2:
        raise ReleaseSourceError("The release manifest schema is not supported.")

    requires = manifest.get("requires")
    if not isinstance(requires, dict):
        raise ReleaseSourceError("The release manifest has invalid requirements.")
    unknown = set(requires) - KNOWN_REQUIREMENT_KEYS
    if unknown:
        raise ReleaseSourceError(
            "The release manifest declares unsupported requirements: "
            + ", ".join(sorted(unknown))
            + "."
        )
    services = requires.get("services") or {}
    if not isinstance(services, dict) or any(
        not isinstance(name, str) or not isinstance(spec, str)
        for name, spec in services.items()
    ):
        raise ReleaseSourceError("The release manifest has invalid service requirements.")
    unsupported_services = set(services) - {"composer"}
    if unsupported_services:
        raise ReleaseSourceError(
            "Composer cannot verify required services: "
            + ", ".join(sorted(unsupported_services))
            + "."
        )
    composer_requirement = services.get("composer")
    composer_version = read_composer_version()
    if composer_requirement and not _version_at_least(composer_version, composer_requirement):
        raise ReleaseSourceError(
            f"DjangoLux {expected_version} requires Composer {composer_requirement}; "
            f"this deployment is running {composer_version}."
        )

    migrations = manifest.get("migrations")
    install = manifest.get("install")
    if not isinstance(migrations, dict) or not isinstance(install, dict):
        raise ReleaseSourceError("The release manifest has invalid install policy.")
    effect = migrations.get("effect")
    rollback_compatible = migrations.get("rollback_compatible")
    inline = install.get("inline")
    if effect not in {"none", "state_only", "additive", "altering", "destructive"}:
        raise ReleaseSourceError("The release manifest has an invalid migration effect.")
    if not isinstance(rollback_compatible, bool):
        raise ReleaseSourceError("The release manifest must state rollback compatibility.")
    if inline not in {"allowed", "forbidden"}:
        raise ReleaseSourceError("The release manifest has an invalid inline install policy.")

    normalized = dict(manifest)
    normalized["inline_safe"] = bool(
        inline == "allowed"
        and effect in SAFE_INLINE_EFFECTS
        and rollback_compatible
    )
    normalized["required_services"] = dict(services)
    return normalized


def assess(candidate: ReleaseCandidate, wheel_path) -> dict:
    """Decide whether this wheel may be applied inline.

    `inline_safe` is the release's own declaration; Composer honours it rather
    than second-guessing it. A release that requires an image rebuild is refused
    here, exactly as DjangoLux refuses it today.
    """
    manifest = normalize_manifest(read_wheel_manifest(wheel_path), candidate.version)
    if not manifest["inline_safe"]:
        raise ReleaseSourceError(
            f"DjangoLux {candidate.version} requires a project image rebuild "
            "(inline_safe is false); it cannot be applied to the runtime volume."
        )
    return manifest


def describe(target_version="", *, channel=dlux_channel.STABLE, workdir=None,
             opener=urllib.request.urlopen, runner=subprocess.run) -> dict:
    """Resolve and inspect the newest (or pinned) release without installing it.

    Downloads and verifies the wheel — the manifest inside it is the only
    authority on `inline_safe`, and a published version number alone cannot say
    whether a release may be applied to the runtime volume. Nothing is unpacked
    and nothing is activated.
    """
    candidate = select_candidate(fetch_index(opener=opener), target_version, channel=channel)
    workdir = Path(workdir or tempfile.mkdtemp(prefix="composer-dlux-check-"))
    workdir.mkdir(parents=True, exist_ok=True)
    verify_attestation(candidate, runner=runner)
    wheel = download_wheel(candidate, workdir / candidate.filename, opener=opener)
    manifest = normalize_manifest(read_wheel_manifest(wheel), candidate.version)
    inline_safe = manifest["inline_safe"]
    return {
        "version": candidate.version,
        "filename": candidate.filename,
        "sha256": candidate.sha256,
        "channel": dlux_channel.normalize_channel(channel),
        "prerelease": bool(try_parse(candidate.version) and try_parse(candidate.version).is_prerelease),
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


def obtain(target_version="", *, channel=dlux_channel.STABLE, workdir=None,
           opener=urllib.request.urlopen, runner=subprocess.run) -> tuple:
    """Resolve, verify and unpack a release. Returns ``(candidate, unpacked_dir)``.

    Order matters: attestation and digest are checked before the archive is
    opened, and `inline_safe` before anything is unpacked into place.
    """
    candidate = select_candidate(fetch_index(opener=opener), target_version, channel=channel)
    workdir = Path(workdir or tempfile.mkdtemp(prefix="composer-dlux-"))
    workdir.mkdir(parents=True, exist_ok=True)
    verify_attestation(candidate, runner=runner)
    wheel = download_wheel(candidate, workdir / candidate.filename, opener=opener)
    assess(candidate, wheel)
    unpacked = unpack(wheel, workdir / "unpacked")
    return candidate, unpacked
