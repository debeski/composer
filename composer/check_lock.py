"""One Composer operation at a time per project: deploys, `check --fix`, Operations applies.

A deploy (`./start.sh`, `update`, `restart`, `stop`, `agent update`, the
resident pair's updates and recoveries) and a `check --fix` (the CLI's, or the
sibling an Operations-panel apply starts) all change the same containers and
files, so they take one lock: ``.xclude/composer-check/check-fix.lock`` (the path
the `check --fix` lock always had, so a Composer of either age respects it). It
is held with ``flock`` for the whole run. The kernel drops it when the holder
dies, so a lock cannot go stale; a second run started meanwhile is refused at
once, before it changes anything, with what holds it and how to stop that.
Closing a terminal does not stop a running operation (the wrapper ignores SIGHUP,
and a killed wrapper leaves the Composer container finishing), which is why the
refusal says so.

The resident pair mounts the project read-only. It takes the same lock through a
read-only descriptor (``flock`` needs no write access) and writes no record.

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

#: What a holder is doing, for the refusal another run shows.
CHECK_FIX = "check --fix"
DEPLOY = "deploy"

LOCK_PATH = Path(".xclude") / "composer-check" / "check-fix.lock"
STALE_AFTER = 2 * 60 * 60
_UNSUPPORTED = {errno.ENOLCK, errno.EOPNOTSUPP, errno.EINVAL, getattr(errno, "ENOTSUP", errno.EOPNOTSUPP)}


class CheckFixLockError(RuntimeError):
    """Another Composer operation runs here, or the lock cannot be taken; nothing was changed."""


ProjectLockError = CheckFixLockError


def _container_id() -> str:
    """This process's container (Docker sets the hostname to its short ID), or "" outside one."""
    if not os.path.exists("/.dockerenv"):
        return ""
    return socket.gethostname()


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
    if record.get("command"):
        parts.append(f"'{record['command']}'")
    if record.get("container"):
        parts.append(f"in container {record['container']}")
    if record.get("pid"):
        parts.append(f"pid {record['pid']}")
    if record.get("host"):
        parts.append(f"on host {record['host']}")
    started = record.get("started")
    if isinstance(started, (int, float)):
        parts.append("started " + datetime.fromtimestamp(started, timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    return ", ".join(parts) or "holder unknown"


def _busy_message(path: Path, record: dict, command: str = CHECK_FIX) -> str:
    container = str(record.get("container") or "")
    stop = (
        f"If it waits for an answer nobody will give, stop it with 'docker stop {container}'."
        if container else
        "Its Composer container is listed by 'docker ps'."
    )
    return (
        f"Another Composer operation is running for this project ({describe(record)}); this "
        f"'{command}' was refused and nothing was changed. That operation keeps running when the "
        "terminal that started it is closed or its ./start.sh is killed, and finishes on its own: wait for "
        f"it, then run this again. {stop} The lock is {path}, and the system frees it when that run ends."
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


def _open(path: Path):
    """``(fd, writable)``: read-write when it can be, else read-only on an existing lock file."""
    flags = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return os.open(path, os.O_RDWR | os.O_CREAT | flags, 0o644), True
    except OSError as exc:
        if exc.errno not in (errno.EROFS, errno.EACCES, errno.EPERM):
            raise
        try:
            return os.open(path, os.O_RDONLY | flags), False
        except FileNotFoundError:
            # A read-only project nobody deployed from yet: there is no lock to share.
            return None, False


@contextmanager
def hold(project_root=".", *, command: str = CHECK_FIX, clock=time.time) -> Iterator[Optional[dict]]:
    """Hold the project's operation lock; yields the record of an interrupted run, or None.

    Raises `CheckFixLockError` at once when another run holds it.
    """
    path = Path(project_root) / LOCK_PATH
    try:
        fd, writable = _open(path)
    except OSError as exc:
        raise CheckFixLockError(
            f"Cannot open the Composer operation lock {path}: {exc.strerror or exc}; nothing was changed. "
            "Make .xclude/composer-check/ writable for the user that runs ./start.sh."
        ) from exc
    if fd is None:
        yield None
        return
    locked = False
    try:
        try:
            locked = _lock(fd)
        except OSError as exc:
            if exc.errno not in _UNSUPPORTED:
                raise CheckFixLockError(
                    f"Cannot take the Composer operation lock {path}: {exc.strerror or exc}; nothing was changed."
                ) from exc
            # No flock here: the record decides.
            record = _read_record(fd)
            if not _stale(record, clock()):
                raise CheckFixLockError(_busy_message(path, record, command)) from exc
        else:
            if not locked:
                raise CheckFixLockError(_busy_message(path, _read_record(fd), command))
        previous = _read_record(fd) or None
        if not writable:
            yield None
            return
        record = {"pid": os.getpid(), "host": socket.gethostname(), "started": clock(), "command": command}
        if _container_id():
            record["container"] = _container_id()
        _write_record(fd, record)
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
