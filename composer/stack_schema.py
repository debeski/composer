"""Stack schema stamps in a DjangoLux project's generated stack files.

DjangoLux stamps each generated stack file with the stack contract
``schema_version`` it was written for. ``read_stamp`` mirrors
``dlux.contracts.stack.read_stamp`` (DjangoLux cannot be imported here), so both
sides read the same three spellings: ``DLUX_STACK_SCHEMA: "N"`` in compose.yml,
``LABEL org.dlux.stack-schema="N"`` in the Dockerfile, and a
``dlux stack schema N`` header comment elsewhere.
"""
import re
from pathlib import Path
from typing import Dict, Iterable, Optional

STACK_SCHEMA_ENV = "DLUX_STACK_SCHEMA"

_STAMP_RE = re.compile(
    r"""(?:DLUX_STACK_SCHEMA["']?\s*[:=]|org\.dlux\.stack-schema=|dlux stack schema)\s*["']?(\d+)"""
)

# Files DjangoLux stamps, relative to the project root. compose.yml and the
# secrets file are passed in separately because composer resolves both.
STAMPED_FILES = (
    "compose.dev.yml",
    "Dockerfile",
    "entrypoint.sh",
    "gunicorn.py",
    ".proxy/Caddyfile",
    ".proxy/default.conf.template",
    ".proxy/maintenance.html",
)


def read_stamp(text: str) -> Optional[int]:
    match = _STAMP_RE.search(text or "")
    return int(match.group(1)) if match else None


def collect_stamps(root: str, compose_file: str, extra: Iterable[str] = ()) -> Dict[str, Optional[int]]:
    """``{relative path: schema or None}`` for each stamped file that exists."""
    base = Path(root)
    stamps: Dict[str, Optional[int]] = {}
    for relative in (compose_file, *STAMPED_FILES, *extra):
        if not relative or relative in stamps:
            continue
        path = Path(relative) if Path(relative).is_absolute() else base / relative
        if not path.is_file():
            continue
        try:
            stamps[relative] = read_stamp(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return stamps
