"""One `check --fix` at a time per project.

The lock is ``.xclude/composer-check/check-fix.lock``, held with ``flock`` for the
whole run. The kernel drops it when the holder dies, so a lock cannot go stale;
a second run started meanwhile is refused at once, before it reads anything,
with who holds it. Closing a terminal does not stop a running fix (the wrapper
ignores SIGHUP, and a killed wrapper leaves the Composer container finishing),
which is why the refusal says so.

The holder writes who it is into the file and empties it when it ends. A file
still naming a holder when the lock is free is therefore a run that was killed
(SIGKILL, a host crash): the next run reports it, so the operator knows a fix
may have stopped halfway, and takes over.

Where ``flock`` is unsupported (some network filesystems), the record alone
decides: a holder on this host whose process is gone, or one older than
``STALE_AFTER``, is stale; any other is respected.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

LOCK_PATH = Path(".xclude") / "composer-check" / "check-fix.lock"
STALE_AFTER = 2 * 60 * 60
_UNSUPPORTED = {errno.ENOLCK, errno.EOPNOTSUPP, errno.EINVAL, getattr(errno, "ENOTSUP", errno.EOPNOTSUPP)}


class CheckFixLockError(RuntimeError):
    """Another `check --fix` runs here, or the lock cannot be taken; nothing was changed."""


def _read_record(fd: int) -> dict:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 4096)
        record = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return record if isinstance(record, dict) else {}


def _write_record(fd: int, record: dict) -> None:
    payload = json.dumps(record, sort_keys=True).encode("utf-8") if record else b""
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    if payload:
        os.write(fd, payload)
    os.fsync(fd)


def describe(record: dict) -> str:
    """``pid 12 on host abc123, started 2026-11-25 07:48 UTC``, from whatever the record has."""
    parts = []
    if record.get("pid"):
        parts.append(f"pid {record['pid']}")
    if record.get("host"):
        parts.append(f"on host {record['host']}")
    started = record.get("started")
    if isinstance(started, (int, float)):
        parts.append("started " + datetime.fromtimestamp(started, timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    return ", ".join(parts) or "holder unknown"


def _busy_message(path: Path, record: dict) -> str:
    return (
        f"Another 'composer check --fix' is running for this project ({describe(record)}); nothing was changed. "
        "It keeps running when the terminal that started it is closed or its ./start.sh is killed, and "
        "finishes on its own: wait for it (its Composer container is listed by 'docker ps'), then run "
        f"'composer check --fix' again. The lock is {path}, and the system frees it when that run ends."
    )


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError, TypeError, OverflowError):
        return True
    except OSError:
        return True
    return True


def _stale(record: dict, now: float) -> bool:
    if not record:
        return True
    started = record.get("started")
    if not isinstance(started, (int, float)) or now - started > STALE_AFTER:
        return True
    return record.get("host") == socket.gethostname() and not _alive(record.get("pid"))


def _lock(fd: int) -> bool:
    """True when held, False when another process holds it; raises OSError when unsupported."""
    try:
        import fcntl
    except ImportError:
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return False
        raise
    return True


def _unlock(fd: int) -> None:
    try:
        import fcntl
    except ImportError:
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def hold(project_root=".", *, clock=time.time) -> Iterator[Optional[dict]]:
    """Hold the project's `check --fix` lock; yields the record of an interrupted run, or None.

    Raises `CheckFixLockError` at once when another run holds it.
    """
    path = Path(project_root) / LOCK_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), 0o644)
    except OSError as exc:
        raise CheckFixLockError(
            f"Cannot open the check --fix lock {path}: {exc.strerror or exc}; nothing was changed. "
            "Make .xclude/composer-check/ writable for the user that runs ./start.sh."
        ) from exc
    locked = False
    try:
        try:
            locked = _lock(fd)
        except OSError as exc:
            if exc.errno not in _UNSUPPORTED:
                raise CheckFixLockError(
                    f"Cannot take the check --fix lock {path}: {exc.strerror or exc}; nothing was changed."
                ) from exc
            # No flock here: the record decides.
            record = _read_record(fd)
            if not _stale(record, clock()):
                raise CheckFixLockError(_busy_message(path, record)) from exc
        else:
            if not locked:
                raise CheckFixLockError(_busy_message(path, _read_record(fd)))
        previous = _read_record(fd) or None
        _write_record(fd, {"pid": os.getpid(), "host": socket.gethostname(), "started": clock()})
        try:
            yield previous
        finally:
            _write_record(fd, {})
    finally:
        if locked:
            try:
                _unlock(fd)
            except OSError:
                pass
        os.close(fd)
