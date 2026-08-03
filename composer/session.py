"""Keep a run alive when its terminal goes away.

Closing a terminal delivers SIGHUP to the foreground process group, which used
to abort a deploy midway: containers half-recreated, health checks and
post-start hooks skipped. Composer ignores the hangup, moves its console output
to a log file and finishes the run in the background. SIGINT (Ctrl+C) is left
untouched, so an explicit cancel still stops the run.
"""

import os
import signal
import sys
from typing import Optional

DEFAULT_DETACH_LOG = "composer-detached.log"

_detached = False
_detach_log: Optional[str] = None


def terminal_detached() -> bool:
    """True once the controlling terminal went away and the run kept going."""
    return _detached


def detach_log_path() -> Optional[str]:
    return _detach_log


def resolve_detach_log() -> str:
    return (
        os.environ.get("COMPOSER_DETACH_LOG")
        or os.environ.get("COMPOSER_LOG_FILE")
        or DEFAULT_DETACH_LOG
    )


def install_hangup_guard() -> bool:
    """Ignore terminal hangups for the rest of the process.

    Returns True when the guard is active (POSIX, main thread).
    """
    hangup = getattr(signal, "SIGHUP", None)
    if hangup is None:
        return False
    try:
        signal.signal(hangup, _on_hangup)
    except (OSError, ValueError):
        return False
    # A detached process writing to (or reading from) the old terminal would
    # otherwise be stopped by the kernel instead of continuing.
    for name in ("SIGTTIN", "SIGTTOU"):
        stop_signal = getattr(signal, name, None)
        if stop_signal is None:
            continue
        try:
            signal.signal(stop_signal, signal.SIG_IGN)
        except (OSError, ValueError):
            pass
    return True


def restore_default_signals() -> None:
    """Child-side reset for commands that are meant to die with the terminal."""
    for name in ("SIGHUP", "SIGTTIN", "SIGTTOU"):
        child_signal = getattr(signal, name, None)
        if child_signal is None:
            continue
        try:
            signal.signal(child_signal, signal.SIG_DFL)
        except (OSError, ValueError):
            pass


def _on_hangup(signum, frame):
    global _detached, _detach_log

    if _detached:
        return
    _detached = True
    _detach_log = _redirect_console(resolve_detach_log())

    target = _detach_log or os.devnull
    print(
        f"\nTerminal closed — composer keeps running detached (pid {os.getpid()}).\n"
        f"Console output continues in {target}.",
        flush=True,
    )


def _redirect_console(path: str) -> Optional[str]:
    """Point stdout/stderr at ``path`` and stdin at /dev/null.

    The old terminal is gone, so every further write would fail with EIO and
    every read would spin on EOF.
    """
    resolved: Optional[str] = path
    try:
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError:
        resolved = None
        try:
            handle = os.open(os.devnull, os.O_WRONLY)
        except OSError:
            return None

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass

    try:
        os.dup2(handle, 1)
        os.dup2(handle, 2)
    except OSError:
        resolved = None
    finally:
        os.close(handle)

    try:
        null_in = os.open(os.devnull, os.O_RDONLY)
    except OSError:
        return resolved
    try:
        os.dup2(null_in, 0)
    except OSError:
        pass
    finally:
        os.close(null_in)

    return resolved


def _reset_for_tests() -> None:
    global _detached, _detach_log

    _detached = False
    _detach_log = None
