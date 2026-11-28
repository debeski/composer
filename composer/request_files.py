"""Reading the request documents app services hand Composer on the runtime volume.

``web`` and ``worker`` can write the runtime volume, so whatever sits at a request
name may be a FIFO (an ``open()`` that never returns), a symlink (somebody else's
file), a directory, or a sparse file of any size. Every trigger, request and
acknowledgement the agent and the executor read goes through ``read_bytes``: it
never blocks, never follows a link and never reads more than its cap. A refused
entry is renamed aside and logged, so it can neither stall the loop nor fill the
log, and the next request the app writes by rename is read as usual.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable, Optional, Tuple, Union

PathLike = Union[str, os.PathLike]

#: The ceiling for one request, trigger or acknowledgement.
MAX_REQUEST_BYTES = 65536
#: A refused entry is renamed to ``<name>.refused`` beside it.
REFUSED_SUFFIX = ".refused"
#: The same refusal is logged again only after this many seconds.
LOG_REPEAT_SECONDS = 300.0

_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_logged: "OrderedDict[Tuple[str, str], float]" = OrderedDict()
_LOGGED_MAX = 256


def _kind(info: os.stat_result) -> str:
    mode = info.st_mode
    if stat.S_ISLNK(mode):
        return "a symbolic link"
    if stat.S_ISFIFO(mode):
        return "a FIFO"
    if stat.S_ISDIR(mode):
        return "a directory"
    if stat.S_ISSOCK(mode):
        return "a socket"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "a device"
    return "not a regular file"


def _refusal(info: os.stat_result, limit: int) -> Optional[str]:
    if not stat.S_ISREG(info.st_mode):
        return f"it is {_kind(info)}"
    if info.st_size > limit:
        return f"it is larger than {limit} bytes"
    return None


def _log(path: Path, reason: str, aside: bool) -> None:
    key = (str(path), reason)
    now = time.monotonic()
    last = _logged.get(key)
    if last is not None and now - last < LOG_REPEAT_SECONDS:
        return
    _logged[key] = now
    _logged.move_to_end(key)
    while len(_logged) > _LOGGED_MAX:
        _logged.popitem(last=False)
    print(f"⚠ refused {path}: {reason}; {'set aside, ' if aside else ''}not read", flush=True)


def set_aside(path: PathLike) -> Optional[Path]:
    """Rename ``path`` out of the way without opening or following it.

    ``<name>.refused`` is reused, so repeated plants do not pile up; a planted
    directory under that name falls back to a unique one. None when the rename
    is not possible (the entry is still never read).
    """
    source = Path(path)
    for name in (f"{source.name}{REFUSED_SUFFIX}", f"{source.name}{REFUSED_SUFFIX}-{time.time_ns()}"):
        target = source.with_name(name)
        try:
            os.replace(source, target)
            return target
        except FileNotFoundError:
            return None
        except OSError:
            continue
    return None


def refuse(path: PathLike, reason: str, *, aside: bool = True) -> None:
    path = Path(path)
    if aside:
        set_aside(path)
    _log(path, reason, aside)


def read_bytes(path: PathLike, limit: int = MAX_REQUEST_BYTES, *,
               aside: bool = True) -> Optional[Tuple[bytes, os.stat_result]]:
    """``(data, fstat)`` for a regular file of at most ``limit`` bytes, else None.

    Absent and unreadable entries are None. A symlink, FIFO, directory, device,
    socket or oversized file is refused: logged, renamed aside when ``aside``,
    and None. The open is non-blocking and never follows a link, so the check
    and the read are about the same file.
    """
    path = Path(path)
    try:
        info = os.lstat(path)
    except OSError:
        return None
    reason = _refusal(info, limit)
    if reason is None:
        try:
            descriptor = os.open(path, _OPEN_FLAGS)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno != errno.ELOOP:
                return None
            reason = "it is a symbolic link"
        else:
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                reason = _refusal(info, limit)
                if reason is None:
                    data = stream.read(limit + 1)
                    if len(data) <= limit:
                        return data, info
                    reason = f"it is larger than {limit} bytes"
    refuse(path, reason, aside=aside)
    return None


def parse_json(data: bytes) -> Any:
    """The JSON value in ``data``, or None when it is not JSON."""
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None


def read_json(path: PathLike, limit: int = MAX_REQUEST_BYTES, *, aside: bool = True) -> Any:
    got = read_bytes(path, limit, aside=aside)
    return None if got is None else parse_json(got[0])


def read_object(path: PathLike, limit: int = MAX_REQUEST_BYTES, *, aside: bool = True) -> dict:
    """The JSON object at ``path``, or ``{}`` when absent, refused or not an object."""
    value = read_json(path, limit, aside=aside)
    return value if isinstance(value, dict) else {}


def append_text(path: PathLike, text: str) -> bool:
    """Append ``text`` to a regular file, never through a link and never blocking.

    A FIFO opened for writing with no reader would hang the caller; a symlink
    would send Composer's bytes elsewhere. Both are refused (False).
    """
    path = Path(path)
    flags = (os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, flags, 0o664)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENXIO, errno.EISDIR):
            refuse(path, "it is not a regular file", aside=True)
        return False
    with os.fdopen(descriptor, "ab") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            refuse(path, "it is not a regular file", aside=True)
            return False
        stream.write(text.encode("utf-8"))
    return True


def is_regular(path: PathLike) -> bool:
    """Whether ``path`` itself (not a link's target) is a regular file."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


class RecentTokens:
    """The tokens already answered, newest last, bounded.

    A request is run once per token: a replay of any recent token is skipped,
    not only of the newest one. The newest ``PERSISTED`` ride along in the ack so
    a restart still recognises them.
    """

    CAPACITY = 256
    PERSISTED = 32

    def __init__(self, initial: Iterable[str] = ()):
        self._tokens: "OrderedDict[str, None]" = OrderedDict()
        for token in initial:
            self.add(token)

    @classmethod
    def from_ack(cls, ack: dict) -> "RecentTokens":
        recent = ack.get("recent_tokens") if isinstance(ack, dict) else None
        tokens = [item for item in recent if isinstance(item, str)][-cls.PERSISTED:] \
            if isinstance(recent, list) else []
        token = str((ack or {}).get("token") or "").strip() if isinstance(ack, dict) else ""
        if token:
            tokens.append(token)
        return cls(tokens)

    def add(self, token: Optional[str]) -> None:
        token = str(token or "").strip()[:128]
        if not token:
            return
        self._tokens[token] = None
        self._tokens.move_to_end(token)
        while len(self._tokens) > self.CAPACITY:
            self._tokens.popitem(last=False)

    def __contains__(self, token: object) -> bool:
        return isinstance(token, str) and token.strip() in self._tokens

    def __len__(self) -> int:
        return len(self._tokens)

    def persisted(self) -> list:
        return list(self._tokens)[-self.PERSISTED:]

    def persisted_with(self, token: str) -> list:
        """What ``persisted`` would be after ``add(token)``, without adding it."""
        others = [item for item in self._tokens if item != token]
        return (others + [token])[-self.PERSISTED:]
