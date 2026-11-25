"""The way forward when a stack's schema and its DjangoLux hold each other back.

`check --fix` raises the stack schema one step at a time, and each step needs a
DjangoLux that can run the new layout; an inline update needs a stack schema at
least its release's ``requires.stack_schema``. On an old stack the two refusals
point at each other: the stored-files fix asks for a newer DjangoLux, and the
newest DjangoLux asks for `check --fix` first (break-it finding #1, 2026-11-25).

The way out is a stepping stone: the newest release this stack still takes
inline, then `check --fix`, and for what no inline release can reach, a project
image on a newer DjangoLux. ``stepping_stone`` finds that release from the
release data itself: the PyPI index, and the release manifest inside each wheel
it probes. Nothing is hard-coded but the DjangoLux each schema step needs, which
the steps themselves define.

Probes read only the manifest: each wheel is checked against the index's SHA-256
but not its attestation, because nothing probed is installed (the update that
follows verifies its wheel in full). Probes are few: release floors only rise,
so a binary search finds the newest release the stack's schema allows.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

from . import dlux_channel
from . import dlux_release_source as release_source
from . import media_migration, worker_migration
from .versions import try_parse

#: ``{stack schema: the DjangoLux its check --fix step needs}``. Schema 3 (the
#: maintenance page) needs Composer's agent, not a DjangoLux release.
SCHEMA_STEP_FLOORS: Dict[int, Tuple[int, int, int]] = {
    4: tuple(media_migration.MEDIA_ACCESS_FLOOR),
    5: tuple(worker_migration.WORKER_FLOOR),
}
#: The steps ``check --fix`` takes on the DjangoLux of the image the Compose files
#: name once Docker has it (``worker``, which the deploy then starts). The others
#: change what the running stack serves at once, so they wait for the running
#: DjangoLux: that image is deployed first.
IMAGE_GATED_STEPS = frozenset({5})
MAX_PROBES = 6

# The refusal dlux_release_source.normalize_manifest words; tests pin the two together.
_REFUSAL_RE = re.compile(
    r"DjangoLux (?P<version>\S+) needs stack schema (?P<floor>\d+) "
    r"\((?:this stack is on (?P<stamp>\d+)|this stack is unstamped)\)"
)


def _triple(version) -> Optional[Tuple[int, int, int]]:
    if isinstance(version, tuple):
        return tuple(version[:3]) if len(version) >= 3 else None
    parsed = try_parse(str(version or ""))
    if parsed is None:
        return None
    release = tuple(parsed.release) + (0, 0, 0)
    return release[:3]


def _text(version: Tuple[int, int, int]) -> str:
    return ".".join(map(str, version))


def parse_refusal(message: str) -> Optional[dict]:
    """``{"version", "floor", "stamp"}`` from a stack schema refusal, or None."""
    match = _REFUSAL_RE.search(message or "")
    if not match:
        return None
    return {
        "version": match.group("version"),
        "floor": int(match.group("floor")),
        "stamp": int(match.group("stamp")) if match.group("stamp") else None,
        "summary": match.group(0) + ".",
    }


def reachable_schema(stamp: Optional[int], running) -> int:
    """The highest stack schema `check --fix` can bring a ``stamp`` stack to on ``running``."""
    # An unstamped stack predates stamping (schema 2 or older).
    schema = stamp if stamp is not None else 2
    running = _triple(running)
    while schema + 1 in SCHEMA_STEP_FLOORS or schema + 1 == 3:
        floor = SCHEMA_STEP_FLOORS.get(schema + 1)
        if floor is not None and (running is None or running < floor):
            break
        schema += 1
    return schema


def image_floor(reach: int, needed: int) -> Optional[Tuple[int, int, int]]:
    """The DjangoLux an image needs so `check --fix` can go from ``reach`` to ``needed``."""
    floors = [floor for schema, floor in SCHEMA_STEP_FLOORS.items() if reach < schema <= needed]
    return max(floors) if floors else None


class _Prober:
    """Reads release manifests, each at most once."""

    def __init__(self, opener, limit: int):
        self.opener = opener
        self.limit = limit
        self.cache: Dict[str, Optional[dict]] = {}
        self.workdir = Path(tempfile.mkdtemp(prefix="composer-dlux-path-"))

    def __call__(self, candidate) -> Optional[dict]:
        if candidate.version in self.cache:
            return self.cache[candidate.version]
        if len(self.cache) >= self.limit:
            raise LookupError("probe limit")
        try:
            wheel = release_source.download_wheel(
                candidate, self.workdir / candidate.filename, opener=self.opener
            )
            manifest = release_source.normalize_manifest(
                release_source.read_wheel_manifest(wheel), candidate.version
            )
            wheel.unlink()
        except release_source.ReleaseSourceError:
            manifest = None
        self.cache[candidate.version] = manifest
        return manifest

    def close(self):
        shutil.rmtree(self.workdir, ignore_errors=True)


def _allows_stack(manifest: Optional[dict]) -> bool:
    return manifest is not None and not manifest.get("stack_schema_reason")


def stepping_stone(
    running,
    *,
    below: str = "",
    channel: str = dlux_channel.STABLE,
    exclude: Sequence[str] = (),
    opener=urllib.request.urlopen,
    limit: int = MAX_PROBES,
) -> Optional[str]:
    """The newest release above ``running`` this stack takes inline, or None.

    "Takes inline" is the release manifest's own verdict for this stack (its
    stack schema floor against compose.yml's stamps, its migration policy, its
    Composer floor), as `dlux update` would judge it. ``below`` bounds the
    search (the release that was refused); ``exclude`` drops quarantined
    releases. Raises ``release_source.ReleaseSourceError`` when the index cannot
    be read, and returns None when the probe budget runs out first.
    """
    current = try_parse(".".join(map(str, running)) if isinstance(running, tuple) else str(running or ""))
    ceiling = try_parse(below) if below else None
    skipped = {parsed for parsed in map(try_parse, exclude) if parsed is not None}
    prereleases = dlux_channel.prereleases_allowed(channel)
    pool = []
    for candidate in release_source.fetch_index(opener=opener):
        parsed = try_parse(candidate.version)
        if parsed is None or parsed.is_devrelease or parsed in skipped:
            continue
        if parsed.is_prerelease and not prereleases:
            continue
        if current is not None and parsed <= current:
            continue
        if ceiling is not None and parsed >= ceiling:
            continue
        pool.append((parsed, candidate))
    pool.sort(key=lambda item: item[0])
    candidates = [candidate for _parsed, candidate in pool]
    if not candidates:
        return None
    probe = _Prober(opener, limit)
    try:
        # Floors only rise with the version: find the newest release whose floor
        # this stack meets, then walk down past any the manifest refuses inline.
        low, high = 0, len(candidates) - 1
        newest_allowed = -1
        while low <= high:
            middle = (low + high) // 2
            manifest = probe(candidates[middle])
            if manifest is None or _allows_stack(manifest):
                if manifest is not None:
                    newest_allowed = max(newest_allowed, middle)
                    low = middle + 1
                else:
                    # Unreadable: no evidence either way; look below it.
                    high = middle - 1
            else:
                high = middle - 1
        for index in range(newest_allowed, -1, -1):
            manifest = probe(candidates[index])
            if _allows_stack(manifest) and manifest.get("inline_safe"):
                return candidates[index].version
        return None
    except LookupError:
        return None
    finally:
        probe.close()


def _image_step(reach: int, needed: int, refused: str) -> str:
    floor = image_floor(reach, needed)
    where = f"DjangoLux {_text(floor)} or later" if floor else "a newer DjangoLux"
    image = (
        f"put the project image on {where} ('./start.sh pull' for a published image, or bump the "
        "django-lux pin and rebuild)"
    )
    comes = f"DjangoLux {refused} then comes with the image or installs with './start.sh dlux update'."
    steps = [schema for schema in SCHEMA_STEP_FLOORS if reach < schema <= needed]
    if all(schema in IMAGE_GATED_STEPS for schema in steps):
        return f"{image}, run './start.sh check --fix' (stack schema {needed}), then deploy; {comes}"
    return f"{image}, deploy it, then run './start.sh check --fix' (stack schema {needed}); {comes}"


def update_way_out(*, refused: str, needed: int, stamp: Optional[int], running, stone: Optional[str],
                   index_error: str = "") -> str:
    """What to run when DjangoLux ``refused`` needs stack schema ``needed`` and the stack is on ``stamp``."""
    now = reachable_schema(stamp, running)
    if now >= needed:
        return (
            f"Run './start.sh check --fix' first (it brings this stack to schema {needed}), then "
            "'./start.sh dlux update' again."
        )
    if stone:
        after = reachable_schema(stamp, stone)
        first = (
            f"Run './start.sh dlux update --version {stone}' (the newest release this stack takes inline), "
            f"then './start.sh check --fix'" + (f" (stack schema {after})" if after > (stamp or 0) else "")
        )
        if after >= needed:
            return first + ", then './start.sh dlux update' again."
        return first + ". After that, " + _image_step(after, needed, refused)
    lead = "No release newer than the running one installs inline on this stack" if not index_error else (
        f"Composer could not read the release index ({index_error}) to name a release this stack takes inline"
    )
    return f"{lead}: " + _image_step(now, needed, refused)


def media_way_out(*, running, stamp: Optional[int], stone: Optional[str], index_error: str = "") -> str:
    """How to get DjangoLux to the stored-files floor (1.15.3) on this stack."""
    floor = SCHEMA_STEP_FLOORS[4]
    finish = "then run 'composer check --fix' to make stored files private (stack schema 4)."
    stone_triple = _triple(stone) if stone else None
    if stone_triple is not None and stone_triple >= floor:
        return (
            f"Run './start.sh dlux update --version {stone}' (the newest release this stack takes inline; "
            f"stored files need DjangoLux {_text(floor)} or later), {finish}"
        )
    if index_error:
        return (
            f"Update DjangoLux to {_text(floor)} or later: inline with './start.sh dlux update --version X', X "
            f"being the newest release this stack takes (Composer could not read the release index: "
            f"{index_error}), or with a project image built on it; {finish}"
        )
    return (
        f"No release this stack takes inline reaches DjangoLux {_text(floor)}: put the project image on "
        f"{_text(floor)} or later ('./start.sh pull' for a published image, or bump the django-lux pin and "
        f"rebuild), deploy, {finish}"
    )


def find_stone(running, *, below: str = "", channel: str = dlux_channel.STABLE,
               exclude: Sequence[str] = ()) -> Tuple[Optional[str], str]:
    """``(stepping stone or None, index error or "")``, never raising."""
    try:
        return stepping_stone(running, below=below, channel=channel, exclude=exclude), ""
    except release_source.ReleaseSourceError as exc:
        return None, str(exc)
    except Exception as exc:  # the advice must never take the refusal down with it
        return None, f"{type(exc).__name__}: {exc}"
