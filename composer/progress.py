"""A single-line progress bar for the slow, opaque part of a run: image pulls.

Composer pipes Docker's output so it can drive its own panel, which means
Docker's per-layer bars never reach the terminal — a multi-hundred-megabyte pull
looked like a hang. Both `docker pull` and `docker compose pull` announce every
layer up front and then report each layer's phase, so their output aggregates
into one honest bar without asking Docker for anything extra.
"""

import re
from typing import Dict, Optional, Tuple

from .constants import ANSI_ESCAPE_RE

BAR_WIDTH = 16
FILLED = "█"
EMPTY = "░"

_LAYER_ID = re.compile(r"^[0-9a-f]{10,}$")
# "9824c27679d3: Downloading [==>   ]  1.1MB/28.5MB" (docker pull) and
# " 9824c27679d3 Downloading [==>   ]  1.1MB/28.5MB" (compose pull).
_ENTRY = re.compile(r"^(?P<id>[^\s:]+)\s*:?\s+(?P<rest>\S.*)$")
_SIZES = re.compile(
    r"(?P<current>[\d.]+)\s*(?P<current_unit>[kKMGTP]?i?B)"
    r"\s*/\s*(?P<total>[\d.]+)\s*(?P<total_unit>[kKMGTP]?i?B)"
)
_UNITS = {
    "b": 1,
    "kb": 1000, "kib": 1024,
    "mb": 1000**2, "mib": 1024**2,
    "gb": 1000**3, "gib": 1024**3,
    "tb": 1000**4, "tib": 1024**4,
    "pb": 1000**5, "pib": 1024**5,
}

# How far through a layer each phase is. Downloading and extracting are refined
# by the byte counts Docker reports alongside them.
_PHASES = {
    "pulling fs layer": 0.0,
    "waiting": 0.0,
    "retrying": 0.0,
    "downloading": 0.0,
    "verifying checksum": 0.5,
    "download complete": 0.5,
    "extracting": 0.5,
    "pull complete": 1.0,
    "already exists": 1.0,
    "downloaded newer image": 1.0,
}
_PARTIAL_PHASES = {"downloading": (0.0, 0.5), "extracting": (0.5, 0.5)}


def parse_size(value: str, unit: str) -> Optional[float]:
    try:
        return float(value) * _UNITS[unit.lower()]
    except (TypeError, ValueError, KeyError):
        return None


def format_size(size: float) -> str:
    for unit, step in (("GB", 1000**3), ("MB", 1000**2), ("kB", 1000)):
        if size >= step:
            return f"{size / step:.1f}{unit}"
    return f"{int(size)}B"


def format_bar(fraction: float, width: int = BAR_WIDTH) -> str:
    fraction = min(1.0, max(0.0, fraction))
    filled = int(round(fraction * width))
    return FILLED * filled + EMPTY * (width - filled)


class PullProgress:
    """Aggregates pull output into one fraction, layer count, and byte total."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.image = ""
        self.layers: Dict[str, float] = {}
        self.bytes: Dict[str, Tuple[float, float]] = {}
        self.services: Dict[str, bool] = {}
        self.cached: set = set()
        self.finished = False
        self._progress = 0.0

    @property
    def active(self) -> bool:
        """True once there is something real to draw a bar from."""
        return bool(self.layers or self.services or self.image)

    @property
    def fraction(self) -> float:
        return self._progress

    @property
    def complete(self) -> bool:
        return self.finished or bool(self.services) and all(self.services.values())

    def _advance(self):
        """Recompute the displayed fraction, which only ever moves forward.

        Layers are announced as the pull discovers them, so the raw ratio dips
        every time the denominator grows — and a batch of cached layers would
        otherwise read as 100% before the real download has started. The ratio
        is held below completion until the pull actually reports it.
        """
        if self.complete:
            self._progress = 1.0
            return
        if self.layers:
            raw = sum(self.layers.values()) / len(self.layers)
        elif self.services:
            raw = sum(1 for done in self.services.values() if done) / len(self.services)
        else:
            raw = 0.0
        ceiling = 0.99
        if self.services:
            done = sum(1 for value in self.services.values() if value)
            ceiling = min(ceiling, (done + 0.99) / len(self.services))
        self._progress = max(self._progress, min(raw, ceiling))

    @property
    def completed_layers(self) -> int:
        return sum(1 for weight in self.layers.values() if weight >= 1.0)

    def feed(self, raw_line: str) -> bool:
        """Consume one output line. True when it was image-pull output.

        Anything else is left to the caller, which keeps showing it as a plain
        status line (compose's container/health chatter, build output, errors).
        """
        recognized = self._consume(raw_line)
        if recognized:
            self._advance()
        return recognized

    def _consume(self, raw_line: str) -> bool:
        line = ANSI_ESCAPE_RE.sub("", raw_line or "").strip()
        if not line:
            return False

        lowered = line.lower()
        if lowered.startswith("status: "):
            # `docker pull` epilogue: "Downloaded newer image" / "Image is up to date".
            self.finished = True
            return True
        if lowered.startswith("digest: "):
            return True

        match = _ENTRY.match(line)
        if not match:
            return False
        identifier = match.group("id")
        rest = match.group("rest").strip()
        lowered_rest = rest.lower()

        if lowered_rest.startswith("pulling from "):
            self.image = rest[len("pulling from "):].strip()
            return True

        phase = self._phase(lowered_rest)
        if phase is None:
            return False

        if _LAYER_ID.match(identifier):
            if phase == "already exists":
                self.cached.add(identifier)
            self.layers[identifier] = self._weight(identifier, phase, rest)
            return True

        # A compose service line: " web Pulling" / " web Pulled".
        if phase == "pull complete" or lowered_rest.startswith("pulled"):
            self.services[identifier] = True
            return True
        self.services.setdefault(identifier, False)
        return True

    def _phase(self, lowered_rest: str) -> Optional[str]:
        if lowered_rest.startswith("pulled"):
            return "pull complete"
        if lowered_rest.startswith("pulling"):
            return "pulling fs layer"
        for name in _PHASES:
            if lowered_rest.startswith(name):
                return name
        return None

    def _weight(self, identifier: str, phase: str, rest: str) -> float:
        base = _PHASES[phase]
        span = _PARTIAL_PHASES.get(phase)
        current, total = self._sizes(rest)
        if phase == "downloading" and total:
            self.bytes[identifier] = (current, total)
        elif phase in ("download complete", "verifying checksum", "pull complete"):
            known = self.bytes.get(identifier)
            if known:
                self.bytes[identifier] = (known[1], known[1])
        if span and total:
            start, length = span
            base = min(1.0, start + length * (current / total))
        # A layer never reports less progress than it already had.
        return max(base, self.layers.get(identifier, 0.0))

    def _sizes(self, rest: str) -> Tuple[float, float]:
        match = _SIZES.search(rest)
        if not match:
            return 0.0, 0.0
        current = parse_size(match.group("current"), match.group("current_unit"))
        total = parse_size(match.group("total"), match.group("total_unit"))
        if current is None or total is None or total <= 0:
            return 0.0, 0.0
        return current, total

    def byte_summary(self) -> str:
        total = sum(total for _, total in self.bytes.values())
        if not total:
            return ""
        current = sum(min(current, total) for current, total in self.bytes.values())
        return f"{format_size(current)}/{format_size(total)}"

    def scope(self, limit: int = 2) -> str:
        """What is being pulled right now.

        Compose interleaves several services' layers and never says which layer
        belongs to which service, so naming the last service it happened to
        mention points at the wrong image. Name everything still in flight.
        """
        pending = [name for name, done in self.services.items() if not done]
        names = pending or list(self.services)
        if not names:
            return self.image
        if len(names) > limit:
            return f"{', '.join(names[:limit])} +{len(names) - limit}"
        return ", ".join(names)

    def _counts(self) -> str:
        if self.layers:
            counts = f"{self.completed_layers}/{len(self.layers)} layers"
            if self.cached:
                counts += f" ({len(self.cached)} cached)"
            return counts
        if self.services:
            done = sum(1 for value in self.services.values() if value)
            return f"{done}/{len(self.services)} images"
        return ""

    def summary(self) -> str:
        """Coarse, log-friendly text — no bar, no shifting byte counts."""
        parts = [part for part in (self.scope(), f"{int(self.fraction * 100)}%") if part]
        counts = self._counts()
        if counts:
            parts.append(counts)
        return " · ".join(parts)

    def bar(self) -> str:
        parts = []
        scope = self.scope()
        if scope:
            parts.append(scope)
        parts.extend([format_bar(self.fraction), f"{int(self.fraction * 100):3d}%"])
        counts = self._counts()
        if counts:
            parts.append(f"· {counts}")
        transferred = self.byte_summary()
        if transferred:
            parts.append(f"· {transferred}")
        return " ".join(parts)
