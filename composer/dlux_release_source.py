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
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlparse

from . import dlux_channel, safe_files, stack_schema
from .version import read_composer_version
from .versions import at_least, try_parse

# Mirrored from dlux/updater/manifest.py — keep in step.
PYPI_SIMPLE_URL = "https://pypi.org/simple/django-lux/"
# One release's JSON. PyPI's CDN caches it apart from the project index, so it
# is fresh the moment a release is published while the index can lag for
# minutes (seen 2026-10-09 and 2026-11-25).
PYPI_RELEASE_URL = "https://pypi.org/pypi/django-lux/{version}/json"
PYPI_PROJECT_REPOSITORY = "https://github.com/debeski/django-lux"
ALLOWED_DOWNLOAD_HOSTS = frozenset({"pypi.org", "files.pythonhosted.org"})
MAX_INDEX_BYTES = 4 * 1024 * 1024
# Seconds between re-reads while PyPI's CDN serves a stale view of a new release.
INDEX_RETRY_DELAYS = (3, 6, 12)
PROBE_TIMEOUT = 10
MAX_WHEEL_BYTES = 64 * 1024 * 1024
MANIFEST_PATH = "dlux/release-manifest.json"
SAFE_INLINE_EFFECTS = frozenset({"none", "state_only", "additive"})
# Fails closed: a requirement this Composer cannot evaluate refuses the release
# rather than ignoring the constraint. That is why every key DjangoLux may
# publish has to be listed here — `migration_baseline` was added by DjangoLux
# 1.8.14, and until it appeared here every Composer rejected that manifest
# outright. `migration_baseline` is informational to Composer (DjangoLux uses it
# to warn that an update spans migrations an intermediate release introduced);
# recognising it is what makes the release installable at all. `stack_schema`
# (DjangoLux 1.16) is the stack schema stamp the release needs: below it the
# release is not installed inline, because `check --fix` migrates a stack and an
# inline update cannot.
KNOWN_REQUIREMENT_KEYS = frozenset({
    "baked_image", "updater_schema", "services", "migration_baseline", "stack_schema",
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


def fetch_release(version, *, opener=urllib.request.urlopen) -> Optional[ReleaseCandidate]:
    """The wheel of one exact release from its own JSON page, or None if unpublished.

    The same trust as the index: an approved HTTPS host, a SHA-256 digest to
    verify the download against, and (later) the attestation. A yanked or
    malformed release is treated as absent.
    """
    parsed = try_parse(version)
    if parsed is None:
        return None
    url = PYPI_RELEASE_URL.format(version=quote(str(version), safe=""))
    request = urllib.request.Request(url, headers={"User-Agent": "composer-dlux-updater/1"})
    try:
        with opener(request, timeout=PROBE_TIMEOUT) as response:
            _validated_https_url(response.geturl())
            payload = json.loads(_read_bounded(response, MAX_INDEX_BYTES).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise ReleaseSourceError(f"Could not read the DjangoLux {version} release page.") from exc
    except ReleaseSourceError:
        raise
    except Exception as exc:
        raise ReleaseSourceError(f"Could not read the DjangoLux {version} release page.") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("urls"), list):
        return None
    for entry in payload["urls"]:
        if not isinstance(entry, dict) or entry.get("yanked"):
            continue
        filename = str(entry.get("filename") or "")
        match = _WHEEL_RE.match(filename)
        if not match or try_parse(match.group(1)) != parsed:
            continue
        digest = str((entry.get("digests") or {}).get("sha256") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            continue
        try:
            wheel_url = _validated_https_url(entry.get("url"))
        except ReleaseSourceError:
            continue
        return ReleaseCandidate(version=match.group(1), filename=filename, url=wheel_url, sha256=digest)
    return None


def likely_successors(candidates, *, prereleases=False) -> list:
    """Versions that may have been published since this (possibly stale) index.

    The next patch, minor and major after the newest release and the final of a
    newest prerelease; with ``prereleases``, also the next beta of each. A
    short, fixed list: each is one cheap request that is either fresh or a 404.
    """
    parsed = [v for v in (try_parse(c.version) for c in candidates) if v is not None]
    if not parsed:
        return []
    newest = max(parsed)
    major, minor, patch = (tuple(newest.release) + (0, 0, 0))[:3]
    finals = [f"{major}.{minor}.{patch + 1}", f"{major}.{minor + 1}.0", f"{major + 1}.0.0"]
    found = []
    if newest.pre:
        found.append(f"{major}.{minor}.{patch}")
        if prereleases:
            phase, number = newest.pre
            found.append(f"{major}.{minor}.{patch}{phase}{number + 1}")
    found += finals
    if prereleases:
        found += [f"{final}b1" for final in finals[:2]]
    known = set(parsed)
    return [v for v in dict.fromkeys(found) if try_parse(v) not in known]


def _merge(candidates, extra) -> list:
    known = {try_parse(c.version) for c in candidates}
    return list(candidates) + [c for c in extra if c is not None and try_parse(c.version) not in known]


def _probe(versions, opener) -> list:
    """Best effort: a probe that fails is simply not evidence of a release."""
    found = []
    for version in versions:
        try:
            found.append(fetch_release(version, opener=opener))
        except ReleaseSourceError:
            continue
    return [c for c in found if c is not None]


def published_candidates(*, opener=urllib.request.urlopen, target_version="", expected="",
                         channel=dlux_channel.STABLE) -> list:
    """The index, topped up from per-release pages the CDN cannot have cached stale.

    A pin reads its own release page when the index lacks it; an unpinned read
    probes the likely successors of the newest release and the ``expected``
    release a recent check saw. Adding a candidate is not choosing it: channel
    and quarantine filtering still happen in `select_candidate`.
    """
    candidates = fetch_index(opener=opener)
    versions = {try_parse(c.version) for c in candidates}
    if target_version:
        if try_parse(target_version) in versions:
            return candidates
        return _merge(candidates, _probe([target_version], opener))
    probes = likely_successors(candidates, prereleases=dlux_channel.prereleases_allowed(channel))
    if expected and try_parse(expected) is not None and try_parse(expected) not in versions:
        probes = [expected] + [v for v in probes if try_parse(v) != try_parse(expected)]
    return _merge(candidates, _probe(probes, opener))


def resolve_candidate(target_version="", *, channel=dlux_channel.STABLE, exclude=(), expected="",
                      opener=urllib.request.urlopen, sleep=time.sleep) -> ReleaseCandidate:
    """`select_candidate` over `published_candidates`, re-reading while it lags.

    A pin that is not published yet, or an unpinned resolve that lands below
    ``expected``, re-reads a few times before giving its answer. It never pins
    to ``expected``: the channel may have changed.
    """
    wanted = try_parse(expected) if expected and not target_version else None
    for delay in INDEX_RETRY_DELAYS + (None,):
        candidates = published_candidates(
            opener=opener, target_version=target_version, expected=expected if wanted else "",
            channel=channel,
        )
        try:
            candidate = select_candidate(candidates, target_version, channel=channel, exclude=exclude)
        except ReleaseSourceError:
            if not target_version or delay is None:
                raise
            sleep(delay)
            continue
        current = try_parse(candidate.version)
        if wanted is None or delay is None or current is None or current >= wanted:
            return candidate
        sleep(delay)
    raise AssertionError("unreachable")


def select_candidate(candidates, target_version="", *, channel=dlux_channel.STABLE,
                     exclude=()) -> ReleaseCandidate:
    """Pin to ``target_version`` when given, otherwise the newest eligible release.

    ``channel`` decides only whether prereleases are eligible. On stable there is
    deliberately no "fall back to a prerelease when no stable exists": a
    deployment that never opted in must get "nothing available", not a beta.

    An explicit ``target_version`` is honoured on either channel. That is not a
    hole — pinning is how a rollback and a `--version` install work, both of
    which are already deliberate operator acts naming an exact release.

    ``exclude`` lists releases the newest-release choice skips: the ones this
    deployment quarantined after a failed update. A pin ignores it; the update
    decides whether a quarantined release may be retried.
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
    excluded = {parsed for parsed in map(try_parse, exclude) if parsed is not None}
    pool = []
    skipped = []
    for candidate in candidates:
        parsed = try_parse(candidate.version)
        if parsed is None or parsed.is_devrelease:
            continue
        if parsed.is_prerelease and not allow_prereleases:
            continue
        if parsed in excluded:
            skipped.append(candidate.version)
            continue
        pool.append((parsed, candidate))
    if not pool:
        note = f" (skipped quarantined {', '.join(skipped)})" if skipped else ""
        raise ReleaseSourceError(
            "No DjangoLux release is available to install on the "
            f"{dlux_channel.normalize_channel(channel)} channel{note}."
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
    safe_files.atomic_write_bytes(destination, payload)
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

    stack_floor = _stack_schema_floor(requires.get("stack_schema"))

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
    if stack_floor is not None:
        normalized["minimum_stack_schema"] = stack_floor
        stamp = stack_schema.deployment_schema()
        if stamp is None or stamp < stack_floor:
            normalized["inline_safe"] = False
            on = f"this stack is on {stamp}" if stamp is not None else "this stack is unstamped"
            then = "first" if inline == "allowed" else "then rebuild the project image"
            normalized["stack_schema_reason"] = (
                f"DjangoLux {expected_version} needs stack schema {stack_floor} ({on}): run "
                f"'./start.sh check --fix' with Composer 1.10 or later, {then}."
            )
    return normalized


def _stack_schema_floor(value):
    """``">=N"`` (or a bare N) as an integer, or None when not required."""
    if value is None:
        return None
    try:
        floor = int(str(value).strip().lstrip(">=").strip())
    except ValueError as exc:
        raise ReleaseSourceError("The release manifest has an invalid stack schema requirement.") from exc
    if floor < 1:
        raise ReleaseSourceError("The release manifest has an invalid stack schema requirement.")
    return floor


def _not_inline_reason(manifest, version) -> str:
    return manifest.get("stack_schema_reason") or (
        f"DjangoLux {version} requires a project image rebuild."
    )


def assess(candidate: ReleaseCandidate, wheel_path) -> dict:
    """Decide whether this wheel may be applied inline.

    `inline_safe` is the release's own declaration; Composer honours it rather
    than second-guessing it. A release that requires an image rebuild is refused
    here, exactly as DjangoLux refuses it today.
    """
    manifest = normalize_manifest(read_wheel_manifest(wheel_path), candidate.version)
    if manifest.get("stack_schema_reason"):
        raise ReleaseSourceError(manifest["stack_schema_reason"])
    if not manifest["inline_safe"]:
        raise ReleaseSourceError(
            f"DjangoLux {candidate.version} requires a project image rebuild "
            "(inline_safe is false); it cannot be applied to the runtime volume."
        )
    return manifest


def describe(target_version="", *, channel=dlux_channel.STABLE, workdir=None,
             opener=urllib.request.urlopen, runner=subprocess.run, exclude=(),
             sleep=time.sleep) -> dict:
    """Resolve and inspect the newest (or pinned) release without installing it.

    Downloads and verifies the wheel — the manifest inside it is the only
    authority on `inline_safe`, and a published version number alone cannot say
    whether a release may be applied to the runtime volume. Nothing is unpacked
    and nothing is activated.
    """
    candidate = resolve_candidate(target_version, channel=channel, exclude=exclude,
                                  opener=opener, sleep=sleep)
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
        "reason": "" if inline_safe else _not_inline_reason(manifest, candidate.version),
        "manifest": manifest,
    }


#: The most an unpacked release may take; a real one is well under a tenth of it.
MAX_UNPACKED_BYTES = 256 * 1024 * 1024
#: Free space kept beyond the unpacked release, for Composer's status and ack.
UNPACK_SPACE_MARGIN = 16 * 1024 * 1024
_DIST_INFO_RE = re.compile(r"^django_lux-[^/]+\.dist-info$", re.IGNORECASE)


def _allowed_top_level(name: str) -> bool:
    """Only the ``dlux`` package and its own metadata go onto the interpreter path."""
    return name == "dlux" or bool(_DIST_INFO_RE.match(name))


def _free_bytes(path: Path) -> Optional[int]:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def unpack(wheel_path, destination) -> Path:
    """Extract a wheel's ``dlux`` package into a fresh directory.

    Refused, with nothing left behind: a path that escapes the target, a
    top-level entry other than ``dlux/`` and its ``django_lux-*.dist-info/`` (a
    ``sitecustomize.py`` beside the package would run in every app process), an
    archive that unpacks to more than ``MAX_UNPACKED_BYTES``, and one the volume
    has no room for, with ``UNPACK_SPACE_MARGIN`` to spare.
    """
    destination = Path(destination)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    root = destination.resolve()
    try:
        with zipfile.ZipFile(wheel_path) as archive:
            members = archive.infolist()
            size = 0
            for member in members:
                target = (root / member.filename).resolve()
                try:
                    relative = target.relative_to(root)
                except ValueError as exc:
                    raise ReleaseSourceError(f"The wheel contains an unsafe path: {member.filename}") from exc
                if not relative.parts or not _allowed_top_level(relative.parts[0]):
                    raise ReleaseSourceError(
                        f"The wheel carries {member.filename!r} outside the dlux package; an "
                        "inline update installs only dlux/ and its .dist-info."
                    )
                size += max(0, int(member.file_size))
            if size > MAX_UNPACKED_BYTES:
                raise ReleaseSourceError(
                    f"The wheel unpacks to {size // (1024 * 1024)} MB, more than the "
                    f"{MAX_UNPACKED_BYTES // (1024 * 1024)} MB a DjangoLux release may take."
                )
            free = _free_bytes(root)
            if free is not None and free < size + UNPACK_SPACE_MARGIN:
                raise ReleaseSourceError(
                    f"The DjangoLux runtime volume has {free // (1024 * 1024)} MB free; unpacking "
                    f"DjangoLux needs {(size + UNPACK_SPACE_MARGIN) // (1024 * 1024)} MB. Free space "
                    "on the volume (old releases/, downloads/), then try again."
                )
            archive.extractall(root)
        if not (root / "dlux").is_dir():
            raise ReleaseSourceError("The wheel does not contain a 'dlux' package.")
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return destination


def obtain(target_version="", *, channel=dlux_channel.STABLE, workdir=None,
           opener=urllib.request.urlopen, runner=subprocess.run,
           expected="", sleep=time.sleep, exclude=()) -> tuple:
    """Resolve, verify and unpack a release. Returns ``(candidate, unpacked_dir)``.

    Order matters: attestation and digest are checked before the archive is
    opened, and `inline_safe` before anything is unpacked into place.

    ``expected`` is the newest release a recent check saw; see
    `resolve_candidate` for how a lagging index is handled. ``exclude`` is
    passed to `select_candidate`.
    """
    candidate = resolve_candidate(target_version, channel=channel, exclude=exclude,
                                  expected=expected, opener=opener, sleep=sleep)
    workdir = Path(workdir or tempfile.mkdtemp(prefix="composer-dlux-"))
    workdir.mkdir(parents=True, exist_ok=True)
    verify_attestation(candidate, runner=runner)
    wheel = download_wheel(candidate, workdir / candidate.filename, opener=opener)
    assess(candidate, wheel)
    unpacked = unpack(wheel, workdir / "unpacked")
    return candidate, unpacked
