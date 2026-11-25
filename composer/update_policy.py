"""The deployment update policy the resident pair enforces.

``DLUX_UPDATE_POLICY`` reaches composer-agent and composer-executor through their
Compose ``environment`` (from ``.secrets/.env``), which ``web`` cannot change. So
the pair refuses what the policy refuses even when a compromised ``web`` writes the
image trigger or an operation request straight onto the runtime volume.

- ``all``: every update path (the default);
- ``inline``: DjangoLux inline runtime updates only;
- ``none``: no updates.

Checks and rollbacks stay allowed, and so does the operator's own shell
(``./start.sh update``, ``agent update``): only the requests the resident pair
answers are refused. An unknown value counts as ``none``.
"""

import os
from typing import Mapping, Optional, Tuple

ENV_KEY = "DLUX_UPDATE_POLICY"
ALL = "all"
INLINE = "inline"
NONE = "none"
POLICIES = (ALL, INLINE, NONE)

INLINE_UPDATE = "inline"
IMAGE_UPDATE = "image"
COMPOSER_UPDATE = "composer"
_ALLOWED = {
    ALL: {INLINE_UPDATE, IMAGE_UPDATE, COMPOSER_UPDATE},
    INLINE: {INLINE_UPDATE},
    NONE: set(),
}
_WHAT = {
    INLINE_UPDATE: "inline DjangoLux updates",
    IMAGE_UPDATE: "application image updates",
    COMPOSER_UPDATE: "Composer updates",
}

#: Exit code of a refused request, as for any other refusal the pair answers.
REFUSED_EXIT = 2


def normalize(value) -> Tuple[str, bool]:
    """``(policy, valid)``: empty means ``all``; anything unknown means ``none``."""
    text = str(value if value is not None else "").strip().lower()
    if not text:
        return ALL, True
    if text in POLICIES:
        return text, True
    return NONE, False


def current(env: Optional[Mapping[str, str]] = None) -> str:
    return normalize((os.environ if env is None else env).get(ENV_KEY, ""))[0]


def allows(kind: str, env: Optional[Mapping[str, str]] = None) -> bool:
    return kind in _ALLOWED[current(env)]


def refusal(kind: str, env: Optional[Mapping[str, str]] = None) -> str:
    return (
        f"Refused by the deployment update policy ({ENV_KEY}={current(env)}): "
        f"{_WHAT[kind]} are deployed from the server, not requested from the application."
    )
