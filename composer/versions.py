"""One place that compares DjangoLux and Composer version strings.

Composer reimplements almost everything rather than take a dependency, and for
most of it that is the right call. Version ordering is the exception. Once
prereleases exist the rules stop being intuitive — ``1.9.0b10`` is newer than
``1.9.0b2`` but older than ``1.9.0rc1``, which is older than ``1.9.0``, and
``>=1.4.0`` must NOT be satisfied by ``1.4.0b1`` — and a hand-rolled regex gets
at least one of those wrong. The regexes this module replaced got two: they
sorted ``b10`` below ``b2``, and they matched the leading release segment of
``1.3.14b1`` so it satisfied ``>=1.3.14``.

``packaging`` is the reference implementation of PEP 440 and is installed in the
Composer image deliberately. There is no fallback: a Composer that cannot order
versions must refuse to choose a release, not guess at one.
"""

from __future__ import annotations

from packaging.version import InvalidVersion, Version

__all__ = [
    "InvalidVersion",
    "Version",
    "parse",
    "try_parse",
    "is_prerelease",
    "at_least",
    "newest",
]


def parse(text) -> Version:
    """Parse a version, tolerating a leading ``v`` from a Git tag."""
    return Version(str(text or "").strip().lstrip("vV"))


def try_parse(text):
    """``parse`` but ``None`` instead of raising. For filtering unknown input."""
    try:
        return parse(text)
    except InvalidVersion:
        return None


def is_prerelease(text) -> bool:
    parsed = try_parse(text)
    return bool(parsed is not None and parsed.is_prerelease)


def at_least(current, requirement) -> bool:
    """Is ``current`` at or above the floor in ``requirement`` (``">=1.3.14"``)?

    Unparseable input on either side is False — a floor that cannot be evaluated
    has not been met. Prereleases compare by PEP 440, so ``1.3.14b1`` does not
    satisfy ``>=1.3.14``: a beta is not the release it is a beta of, and a
    manifest that needs a fix shipped in 1.3.14 must not accept its beta.
    """
    text = str(requirement or "").strip()
    if text.startswith(">="):
        text = text[2:].strip()
    floor = try_parse(text)
    installed = try_parse(current)
    if floor is None or installed is None:
        return False
    return installed >= floor


def newest(versions):
    """The highest parseable version in ``versions``, or ``None``."""
    parsed = [v for v in (try_parse(item) for item in versions) if v is not None]
    return max(parsed) if parsed else None
