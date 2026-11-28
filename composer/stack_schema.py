"""Stack schema stamps in a DjangoLux project's generated stack files.

DjangoLux stamps each generated stack file with the stack contract
``schema_version`` it was written for. ``read_stamp`` mirrors
``dlux.contracts.stack.read_stamp`` (DjangoLux cannot be imported here), so both
sides read the same three spellings: ``DLUX_STACK_SCHEMA: "N"`` in compose.yml,
``LABEL org.dlux.stack-schema="N"`` in the Dockerfile, and a
``dlux stack schema N`` header comment elsewhere.

A file may carry more than one stamp: Decrees' and CRM gov's compose.yml have a
``# dlux stack schema N`` comment above the ``DLUX_STACK_SCHEMA`` value DjangoLux
reads. ``read_stamps`` returns every one and ``restamp`` rewrites every one, so a
schema step can never leave half a file behind; ``read_stamp`` keeps DjangoLux's
first-stamp rule for single-stamp files.

The schema migrations restamp the plaintext secrets file as well
(``restamp_secrets``). Only its stamp changes: it is rewritten byte for byte and
in place, and its lines never reach a diff, a log or a backup copy. Every other
rewrite keeps the file's line endings (``write_updates``).
"""
import difflib
import errno
import os
import re
import shutil
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional

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


_ENV_STAMP_RE = re.compile(r"""DLUX_STACK_SCHEMA["']?\s*[:=]\s*["']?\d+""")
# The whole value after ``DLUX_STACK_SCHEMA:`` (or ``=``), quoted or bare, for a strict read.
_ENV_VALUE_RE = re.compile(
    r"""DLUX_STACK_SCHEMA["']?[ \t]*[:=][ \t]*(?P<value>"[^"\r\n]*"|'[^'\r\n]*'|[^\s#,}\]]*)"""
)


def read_stamp(text: str) -> Optional[int]:
    """The first stamp in ``text`` (DjangoLux's rule), or None."""
    match = _STAMP_RE.search(text or "")
    return int(match.group(1)) if match else None


def read_stamps(text: str) -> List[int]:
    """Every stamp in ``text``, in order."""
    return [int(match.group(1)) for match in _STAMP_RE.finditer(text or "")]


def has_env_stamp(text: str) -> bool:
    """Whether ``text`` carries the ``DLUX_STACK_SCHEMA`` value DjangoLux reads at runtime."""
    return bool(_ENV_STAMP_RE.search(text or ""))


def env_stamp_values(text: str) -> List[str]:
    """Every ``DLUX_STACK_SCHEMA`` value in ``text`` as written, quotes removed."""
    values = []
    for match in _ENV_VALUE_RE.finditer(text or ""):
        value = match.group("value")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values.append(value.strip())
    return values


def invalid_env_stamps(text: str, newest: int) -> List[str]:
    """The ``DLUX_STACK_SCHEMA`` values in ``text`` that are not a schema from 1 to ``newest``.

    ``read_stamps`` takes the digits it finds, so ``"5x"`` reads as 5, ``"-1"``
    as no stamp and ``"99"`` passes every floor; DjangoLux reads the value
    itself, so anything but a plain known number is reported as written.
    """
    return [
        value for value in env_stamp_values(text)
        if not re.fullmatch(r"[1-9][0-9]*", value) or int(value) > newest
    ]


def restamp(text: str, schema: int) -> str:
    """Rewrite every stamp in ``text`` to ``schema``, keeping each one's spelling."""
    return _STAMP_RE.sub(
        lambda match: match.group(0)[: match.start(1) - match.start(0)] + str(schema), text or ""
    ) if text else text


def restamp_step(text: str, legacy: int, target: int) -> Optional[str]:
    """``text`` restamped ``target`` by a ``legacy`` -> ``target`` schema step, or None.

    A file takes the step when one of its stamps is ``legacy`` and none is newer
    than ``target``; every stamp is rewritten, the stale ones a half restamp left
    behind included.
    """
    stamps = read_stamps(text)
    if legacy not in stamps or max(stamps) > target:
        return None
    return restamp(text, target)


def deployment_schema(root: str = ".") -> Optional[int]:
    """The stack schema the deployment's compose file declares, or None.

    compose.yml's stamps are the ones DjangoLux reads at runtime. When they
    disagree (a half restamp) the lowest wins: the stack is only as far along as
    its most behind stamp says, and ``check`` reports the disagreement. Composer
    runs from the project root, on the host and in its resident services alike.
    """
    from .worker_migration import TARGET_SCHEMA

    for name in ("compose.yml", "docker-compose.yml"):
        path = Path(root) / name
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
            stamps = read_stamps(text)
            # A value DjangoLux cannot read as a known schema is no schema: it
            # must not pass a floor ("99"), nor read as the digits in it ("5x").
            if invalid_env_stamps(text, TARGET_SCHEMA) or any(not 1 <= stamp <= TARGET_SCHEMA for stamp in stamps):
                return None
            return min(stamps) if stamps else None
    return None


def collect_all_stamps(root: str, compose_file: str, extra: Iterable[str] = ()) -> Dict[str, List[int]]:
    """``{relative path: [every stamp]}`` for each stamped-file candidate that exists."""
    base = Path(root)
    stamps: Dict[str, List[int]] = {}
    for relative in (compose_file, *STAMPED_FILES, *extra):
        if not relative or relative in stamps:
            continue
        path = Path(relative) if Path(relative).is_absolute() else base / relative
        if not path.is_file():
            continue
        try:
            stamps[relative] = read_stamps(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return stamps


def collect_stamps(root: str, compose_file: str, extra: Iterable[str] = ()) -> Dict[str, Optional[int]]:
    """``{relative path: lowest schema or None}`` for each stamped file that exists."""
    return {
        relative: (min(found) if found else None)
        for relative, found in collect_all_stamps(root, compose_file, extra).items()
    }


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
    restamped = restamp_step(text, legacy, target)
    if restamped is not None:
        updates[path] = restamped


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


def line_ending(path: Path) -> str:
    """``"\r\n"`` when the file at ``path`` uses CRLF line endings, ``"\r"`` for CR alone, else ``"\n"``.

    The text Composer rewrites is read with universal newlines (LF), so the
    file's own endings are restored when it is written back. ``check --fix``
    leaves a file that mixes endings alone (``mixed_line_endings``); for any
    other caller a mixed file goes by its majority.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return "\n"
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n")
    if not lf and data.count(b"\r"):
        return "\r"
    return "\r\n" if crlf and crlf * 2 >= lf else "\n"


def mixed_line_endings(path: Path) -> bool:
    """Whether the file at ``path`` uses more than one of LF, CRLF and CR alone.

    A rewrite writes one ending for the whole file, so the operator's diff
    would show every line of the other kind as changed.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return False
    crlf = data.count(b"\r\n")
    kinds = [crlf, data.count(b"\n") - crlf, data.count(b"\r") - crlf]
    return sum(1 for count in kinds if count) > 1


def write_text_keeping_endings(path: Path, text: str, *, newline: Optional[str] = None) -> None:
    """Overwrite ``path`` with ``text`` (LF-normalised) in the file's own line endings."""
    newline = newline or line_ending(path)
    with path.open("w", encoding="utf-8", newline=newline) as stream:
        stream.write(text)


def read_only_files(paths: Iterable[Path]) -> List[Path]:
    """The files among ``paths`` this process cannot open for writing.

    Opened for real (``r+b`` writes and truncates nothing): ``os.access`` says
    yes to root even where the filesystem refuses the write.
    """
    blocked = []
    for path in paths:
        try:
            with open(path, "r+b"):
                pass
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
                blocked.append(path)
    return blocked


def read_only_message(root, paths: Iterable[Path]) -> str:
    """Which files are read-only for Composer and how to make them writable; nothing was changed."""
    base = Path(root).resolve()
    names = []
    for path in paths:
        try:
            names.append(str(Path(path).resolve().relative_to(base)))
        except ValueError:
            names.append(str(path))
    uid = os.geteuid() if hasattr(os, "geteuid") else None
    who = f" (uid {uid})" if uid is not None else ""
    quoted = " ".join(f"'{name}'" for name in names)
    return (
        f"Composer{who} cannot write {', '.join(names)}: "
        f"{'it is' if len(names) == 1 else 'they are'} read-only for it, so nothing was changed. "
        f"Make {'it' if len(names) == 1 else 'them'} writable for the user that runs ./start.sh "
        f"(for example 'chmod u+w {quoted}', or 'sudo chown \"$(id -u)\" {quoted}' when another "
        "user owns it), then run 'composer check --fix' again."
    )


def ensure_writable(root, paths: Iterable[Path]) -> None:
    """Raise ``PermissionError`` naming the read-only files among ``paths``.

    Migrations call it before they validate or archive anything, so a read-only
    file stops the fix with nothing changed and a message that says what to do.
    """
    blocked = read_only_files(paths)
    if blocked:
        raise PermissionError(read_only_message(root, blocked))


def write_updates(root, updates: Mapping[Path, str], backup: Path) -> None:
    """Copy each original under ``backup``, then write ``updates``.

    A read-only file stops the whole write before anything is copied or
    written, with a message naming it (``PermissionError``, an ``OSError``).

    The secrets file is not copied, so no second copy of the secrets outlives
    their rotation. It is overwritten in place: it keeps its mode, owner and
    inode, and is never left empty. Every other file keeps its line endings.
    """
    base = Path(root).resolve()
    secrets = secrets_file(base)
    ensure_writable(base, updates)
    endings = {path: line_ending(path) for path in updates if path != secrets}
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
        write_text_keeping_endings(path, text, newline=endings[path])
