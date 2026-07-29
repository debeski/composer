"""composer-executor: the sole holder of Docker authority.

Listens on a private Unix socket for the small, fixed set of typed operations the
network-facing agent initiates (``restart``, ``recovery_deploy``), re-validates
each (the security boundary), and runs the corresponding composer op with real
Docker access. The agent itself holds no Docker socket and no ``DOCKER_HOST``.

Image updates are handled by the executor's (relocated) trigger-watcher loop, not
over this socket; backups are DjangoLux-side. This module is the socket half.

The transport/validation layer takes an injected ``handler(request) -> result``
so it is fully unit-testable without Docker.
"""

import os
import socket
import struct
import sys
import threading
from typing import Callable, Dict, Optional

from . import executor_protocol as proto

# Runtime location of the private socket, on a volume mounted only into the
# agent and executor. Never TCP, never the shared bridge dir.
EXECUTOR_SOCKET_ENV = "COMPOSER_EXECUTOR_SOCKET"
# Dedicated mount (a shared named volume) rather than a subpath of dlux_runtime,
# so the socket volume never nests inside another mount.
DEFAULT_EXECUTOR_SOCKET = "/run/composer-exec/composer-exec.sock"
# Optional expected peer uid (the agent's uid); when set, enforced via SO_PEERCRED.
EXECUTOR_PEER_UID_ENV = "COMPOSER_EXECUTOR_PEER_UID"

_ACCEPT_BACKLOG = 8
# Cap concurrent connection handlers so a flood cannot exhaust threads. Only one
# op ever runs (the lease); extra connections are cheaply rejected as busy.
_MAX_INFLIGHT_CONNS = 4

Handler = Callable[[Dict], Dict]


def resolve_socket_path() -> str:
    return os.environ.get(EXECUTOR_SOCKET_ENV) or DEFAULT_EXECUTOR_SOCKET


def _recv_exactly(conn: socket.socket) -> Callable[[int], bytes]:
    def reader(n: int) -> bytes:
        chunks = []
        remaining = n
        while remaining > 0:
            chunk = conn.recv(remaining)
            if not chunk:
                raise proto.ProtocolError("Executor connection closed mid-frame.")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    return reader


def _peer_uid(conn: socket.socket) -> Optional[int]:
    """Return the connecting peer's uid via SO_PEERCRED (Linux), else None."""
    try:
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", creds)
        return uid
    except (OSError, AttributeError, ValueError):
        return None


class Executor:
    def __init__(self, socket_path: str, handler: Handler, *, expected_peer_uid: Optional[int] = None):
        self.socket_path = socket_path
        self.handler = handler
        self.expected_peer_uid = expected_peer_uid
        # One operation in flight across every channel (socket ops + watcher).
        # Public so the watch loop serializes against socket ops on the same lease.
        self.op_lease = threading.Lock()
        self._op_lease = self.op_lease
        self._conn_slots = threading.BoundedSemaphore(_MAX_INFLIGHT_CONNS)
        self._stop = threading.Event()
        self._server: Optional[socket.socket] = None

    # -- lifecycle -------------------------------------------------------

    def _bind(self) -> socket.socket:
        directory = os.path.dirname(self.socket_path) or "."
        os.makedirs(directory, exist_ok=True)
        # Remove a stale socket left by a crash; never follow a symlink here.
        try:
            if os.path.islink(self.socket_path):
                raise proto.ProtocolError("Executor socket path is a symlink; refusing to bind.")
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # Create the socket group-accessible only (0660): the agent shares the
        # executor's gid on the private volume; nothing else can connect.
        old_umask = os.umask(0o117)
        try:
            srv.bind(self.socket_path)
        finally:
            os.umask(old_umask)
        try:
            os.chmod(self.socket_path, 0o660)
        except OSError:
            pass
        srv.listen(_ACCEPT_BACKLOG)
        return srv

    def serve_forever(self) -> None:
        self._server = self._bind()
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = self._server.accept()
                except OSError:
                    if self._stop.is_set():
                        break
                    continue
                if not self._conn_slots.acquire(blocking=False):
                    # Too many in flight; shed load rather than queue unbounded.
                    conn.close()
                    continue
                threading.Thread(target=self._serve_conn, args=(conn,), daemon=True).start()
        finally:
            self._close()

    def stop(self) -> None:
        self._stop.set()
        server = self._server
        if server is not None:
            try:
                server.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _close(self) -> None:
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass

    # -- per-connection --------------------------------------------------

    def _serve_conn(self, conn: socket.socket) -> None:
        try:
            with conn:
                if not self._authorized(conn):
                    self._send(conn, proto.build_result("", "rejected", exit_code=2, detail="Executor: unauthorized peer."))
                    return
                try:
                    raw = proto.read_frame(_recv_exactly(conn))
                    request = proto.validate_executor_request(raw)
                except proto.ProtocolError as exc:
                    self._send(conn, proto.build_result("", "rejected", exit_code=2, detail=f"Executor: {exc}"))
                    return
                operation_id = request["operation_id"]
                if not self._op_lease.acquire(blocking=False):
                    self._send(
                        conn,
                        proto.build_result(operation_id, "rejected", exit_code=2, detail="Executor is busy with another operation."),
                    )
                    return
                try:
                    result = self.handler(request)
                except Exception as exc:  # handler must never crash the server
                    result = proto.build_result(operation_id, "failed", exit_code=1, detail=f"Executor handler error: {exc}")
                finally:
                    self._op_lease.release()
                if not isinstance(result, dict):
                    result = proto.build_result(operation_id, "failed", exit_code=1, detail="Executor handler returned no result.")
                self._send(conn, result)
        except (OSError, proto.ProtocolError):
            pass
        finally:
            self._conn_slots.release()

    def _authorized(self, conn: socket.socket) -> bool:
        if self.expected_peer_uid is None:
            return True
        uid = _peer_uid(conn)
        # If the platform cannot report peer creds, fall back to the filesystem
        # permission control (the socket is 0660 on a private volume).
        if uid is None:
            return True
        return uid == self.expected_peer_uid

    def _send(self, conn: socket.socket, obj: Dict) -> None:
        try:
            conn.sendall(proto.encode_frame(obj))
        except (OSError, proto.ProtocolError):
            pass


def _expected_peer_uid_from_env() -> Optional[int]:
    raw = os.environ.get(EXECUTOR_PEER_UID_ENV)
    if raw is None or not str(raw).strip():
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        return None


def _run_watch_loop(watch, op_lease, stop_event) -> None:
    """The executor's trigger-watched image-update loop.

    Serializes with socket ops on the shared ``op_lease``. Availability checks are
    NOT run here — those are a Docker read the agent performs via the read-only
    proxy. A single bad cycle must never take the executor down.
    """
    interval = max(2.0, float(getattr(watch, "interval", 2) or 2))
    while not stop_event.is_set():
        try:
            request = watch.pending_request()
            if request:
                with op_lease:
                    watch.process(request)
        except Exception:
            pass
        stop_event.wait(interval)


def _build_watch_runtime(args):
    """Build a WatchRuntime for the executor's write-only update loop, or None
    when no trigger file is configured (interactive / test use)."""
    trigger = getattr(args, "trigger_file", None)
    if not trigger:
        return None
    from types import SimpleNamespace

    from .watcher import WatchRuntime

    watch_args = SimpleNamespace(
        trigger_file=trigger,
        status_file=getattr(args, "status_file", None),
        log_file=getattr(args, "log_file", None),
        interval=getattr(args, "interval", 2) or 2,
        dev=getattr(args, "dev", False),
        file=getattr(args, "file", None),
        # Availability is a Docker READ that stays with the agent (read-only
        # proxy); the executor never runs it.
        check_image=[],
        availability_file=None,
        check_interval=3600,
    )
    return WatchRuntime(watch_args)


def run_executor(args) -> int:
    """Entry point for ``composer executor``: serve the socket with the real
    operation handler, and (when a trigger file is configured) run the
    trigger-watched image-update loop sharing the same one-op-in-flight lease."""
    from .executor_ops import default_operation_handler

    socket_path = getattr(args, "socket", None) or resolve_socket_path()
    executor = Executor(
        socket_path,
        default_operation_handler,
        expected_peer_uid=_expected_peer_uid_from_env(),
    )
    stop_event = threading.Event()
    watch = _build_watch_runtime(args)
    if watch is not None:
        threading.Thread(
            target=_run_watch_loop,
            args=(watch, executor.op_lease, stop_event),
            daemon=True,
        ).start()
    try:
        executor.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        executor.stop()
    return 0
