"""Stack schema stamps in a DjangoLux project's generated stack files.

DjangoLux stamps each generated stack file with the stack contract
``schema_version`` it was written for. ``read_stamp`` mirrors
``dlux.contracts.stack.read_stamp`` (DjangoLux cannot be imported here), so both
sides read the same three spellings: ``DLUX_STACK_SCHEMA: "N"`` in compose.yml,
``LABEL org.dlux.stack-schema="N"`` in the Dockerfile, and a
``dlux stack schema N`` header comment elsewhere.

The schema migrations restamp the plaintext secrets file as well
(``restamp_secrets``). Only its stamp changes: it is rewritten byte for byte and
in place, and its lines never reach a diff, a log or a backup copy.
"""
import difflib
import os
import re
import shutil
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional

from .secrets_manager import PLAINTEXT_ENV_CANDIDATES

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


def restamp(text: str, schema: int) -> str:
    """Rewrite the first stamp in ``text`` to ``schema``, keeping its spelling."""
    match = _STAMP_RE.search(text or "")
    if not match:
        return text
    return text[: match.start(1)] + str(schema) + text[match.end(1):]


def deployment_schema(root: str = ".") -> Optional[int]:
    """The stack schema the deployment's compose file declares, or None.

    compose.yml's stamp is the one DjangoLux reads at runtime. Composer runs from
    the project root, on the host and in its resident services alike.
    """
    for name in ("compose.yml", "docker-compose.yml"):
        path = Path(root) / name
        if path.is_file():
            try:
                return read_stamp(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                return None
    return None


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


def secrets_file(root) -> Optional[Path]:
    """The plaintext secrets file Composer loads (the first candidate that exists).

    It is the file ``check`` reads a stamp from. None when there is none, or when
    it resolves outside the project.
    """
    base = Path(root).resolve()
    for name in PLAINTEXT_ENV_CANDIDATES:
        path = base / name
        if path.is_file():
            resolved = path.resolve()
            return resolved if resolved.is_relative_to(base) else None
    return None


def restamp_secrets(root, updates: Dict[Path, str], legacy: int, target: int) -> None:
    """Add the secrets file to ``updates``, restamped ``target``, if it is stamped ``legacy``.

    It is decoded byte for byte (line endings and undecodable bytes kept), so
    ``write_updates`` changes nothing in it but the stamp. An unreadable file is
    left alone.
    """
    path = secrets_file(root)
    if path is None or path in updates:
        return
    try:
        text = path.read_bytes().decode("utf-8", "surrogateescape")
    except OSError:
        return
    if read_stamp(text) == legacy:
        updates[path] = restamp(text, target)


def preview_diff(root, updates: Mapping[Path, str]) -> str:
    """Unified diffs of ``updates``, leaving out the secrets file's lines."""
    base = Path(root).resolve()
    secrets = secrets_file(base)
    return "".join(
        "".join(difflib.unified_diff(
            path.read_text(encoding="utf-8").splitlines(keepends=True),
            updates[path].splitlines(keepends=True),
            fromfile=f"a/{name}", tofile=f"b/{name}",
        ))
        for name, path in sorted((str(path.relative_to(base)), path) for path in updates)
        if path != secrets
    )


def secrets_note(root, updates: Mapping[Path, str], target: int) -> str:
    """The preview's line about the secrets file, whose diff is not shown."""
    base = Path(root).resolve()
    secrets = secrets_file(base)
    if secrets is None or secrets not in updates:
        return ""
    return (
        f"{secrets.relative_to(base)} is restamped {target} in place; nothing else in it changes, "
        "and it is not shown or copied to the backup."
    )


def write_updates(root, updates: Mapping[Path, str], backup: Path) -> None:
    """Copy each original under ``backup``, then write ``updates``.

    The secrets file is not copied, so no second copy of the secrets outlives
    their rotation. It is overwritten in place: it keeps its mode, owner and
    inode, and is never left empty.
    """
    base = Path(root).resolve()
    secrets = secrets_file(base)
    for path in updates:
        if path == secrets:
            continue
        destination = backup / path.relative_to(base)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    for path, text in updates.items():
        if path == secrets:
            with path.open("r+b") as stream:
                stream.write(text.encode("utf-8", "surrogateescape"))
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
            continue
        with path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
