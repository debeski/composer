"""Writing and reading files on a volume other services can write to.

The runtime volume is group-writable by the app services, so a name Composer is
about to write can already exist as a symlink someone else planted. A fixed temp
name (``.x.json.tmp``) opened with ``open("w")`` follows that link and writes
Composer's bytes wherever it points. These helpers never open a name that is
predictable: the temp file is created exclusively under a random name, and
reads refuse symlinks and anything that is not a regular file of bounded size.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Union

from .app_permissions import SHARED_FILE_MODE

PathLike = Union[str, os.PathLike]

#: The default ceiling for a JSON document another service hands Composer.
MAX_DOCUMENT_BYTES = 65536


def atomic_write_bytes(path: PathLike, data: bytes, *, mode: int = SHARED_FILE_MODE) -> Path:
    """Write ``data`` to a fresh random temp file beside ``path``, then rename it over.

    The rename replaces whatever ``path`` was, a planted symlink included,
    without following it.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return target


def atomic_write_text(path: PathLike, text: str, *, mode: int = SHARED_FILE_MODE) -> Path:
    return atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def atomic_write_json(path: PathLike, payload: Any, *, sort_keys: bool = True,
                      mode: int = SHARED_FILE_MODE) -> Path:
    return atomic_write_text(path, json.dumps(payload, sort_keys=sort_keys) + "\n", mode=mode)


def read_bounded(path: PathLike, limit: int = MAX_DOCUMENT_BYTES) -> bytes:
    """A regular file's bytes, refusing a symlink, a FIFO or more than ``limit`` bytes.

    Raises ``OSError`` for every refusal, so callers that already treat an
    unreadable file as absent need no new branch.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"{path} is not a regular file")
        if info.st_size > limit:
            raise OSError(f"{path} is larger than {limit} bytes")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise OSError(f"{path} is larger than {limit} bytes")
    return data


def read_tail(path: PathLike, limit: int) -> bytes:
    """The last ``limit`` bytes of a regular file, refusing a symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"{path} is not a regular file")
        if info.st_size > limit:
            stream.seek(info.st_size - limit)
        return stream.read(limit)
