"""Which Composer image this project runs: stable or beta.

Distinct from the DjangoLux channel in ``dlux_channel``. That one decides which
django-lux *releases* a deployment installs and is owned by the DjangoLux
administrator; this one decides which *Composer* image the wrapper, the deployer
and the resident agent/executor pair run, and is owned by whoever administers
the host. The plan keeps them independent on purpose: running a Composer beta to
test Composer is not a reason to start feeding a production deployment beta
framework releases, and vice versa.

Persisted as one line in ``.composer-channel`` beside the wrapper, because the
wrapper resolves its own image *before* any Composer code runs. A value baked
into the wrapper's program text would have to be rewritten on every switch, and
then every switch would be a wrapper edit that the marker/checksum machinery has
to forgive.

Precedence, and it is deliberate:

1. ``COMPOSER_SELF_IMAGE`` — an explicit pin, and explicit always wins. An
   operator who pinned an exact image gets that image, channel or no channel.
2. ``.composer-channel`` — the persisted choice.
3. ``:latest`` — stable, the default for a project that never chose.
"""

from __future__ import annotations

import os
from pathlib import Path

STABLE = "stable"
BETA = "beta"
CHANNELS = (STABLE, BETA)

CHANNEL_FILENAME = ".composer-channel"
IMAGE_REPOSITORY = "debeski/composer"
CHANNEL_TAGS = {STABLE: "latest", BETA: "beta"}


class ChannelConfigError(RuntimeError):
    """The requested Composer channel is not one this build understands."""


def normalize_channel(value) -> str:
    text = str(value or "").strip().lower()
    if text not in CHANNELS:
        raise ChannelConfigError(
            f"'{value}' is not a Composer channel; expected one of: " + ", ".join(CHANNELS) + "."
        )
    return text


def channel_file(project_dir=".") -> Path:
    return Path(project_dir) / CHANNEL_FILENAME


def read_channel(project_dir=".") -> str:
    """The persisted channel, or stable. An unreadable file reads as stable.

    Never raises: this is consulted on the way to running a command, and a
    corrupt one-line file must not be the reason a deployment cannot be operated.
    Stable is the safe answer — the worst case is that a beta tester has to set
    the channel again, rather than a production host silently running a beta.
    """
    try:
        raw = channel_file(project_dir).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return STABLE
    value = raw.strip().lower()
    return value if value in CHANNELS else STABLE


def write_channel(channel, project_dir=".") -> Path:
    """Persist the channel atomically. Returns the file written."""
    channel = normalize_channel(channel)
    path = channel_file(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(channel + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def image_for_channel(channel) -> str:
    return f"{IMAGE_REPOSITORY}:{CHANNEL_TAGS[normalize_channel(channel)]}"


def resolve_self_image(project_dir=".", env=None) -> str:
    """The Composer image this project should run, honouring the pin first."""
    env = os.environ if env is None else env
    pinned = str(env.get("COMPOSER_SELF_IMAGE") or "").strip()
    if pinned:
        return pinned
    return image_for_channel(read_channel(project_dir))


def describe(project_dir=".", env=None) -> dict:
    """Channel, resolved image, and whether a pin is overriding the channel."""
    env = os.environ if env is None else env
    pinned = str(env.get("COMPOSER_SELF_IMAGE") or "").strip()
    channel = read_channel(project_dir)
    return {
        "channel": channel,
        "image": pinned or image_for_channel(channel),
        "pinned": bool(pinned),
        "channel_image": image_for_channel(channel),
    }
