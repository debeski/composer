"""Agent-side client for the private executor socket.

Synchronous: send one typed request, receive one typed result. The agent uses
this when an executor is configured (``COMPOSER_EXECUTOR_SOCKET``) so it never
touches Docker for restart/recovery. When no executor is configured the agent
keeps its legacy in-process path (backwards compatible).
"""

import os
import socket
import uuid
from typing import Dict, Optional, Tuple

from . import executor_protocol as proto
from .executor import EXECUTOR_ROLE_ENV, EXECUTOR_SOCKET_ENV, resolve_socket_path

# How long to wait for a synchronous op result. Recovery shells a full update, so
# allow a generous ceiling; override with COMPOSER_EXECUTOR_TIMEOUT.
_DEFAULT_TIMEOUT = 1800.0
# A read runs `docker compose` with a 60 s timeout of its own.
READ_TIMEOUT = 120.0


class ExecutorClientError(RuntimeError):
    pass


def executor_configured() -> bool:
    """True when this deployment routes Docker mutations through an executor."""
    return bool(str(os.environ.get(EXECUTOR_SOCKET_ENV) or "").strip())


def reads_via_executor() -> bool:
    """True in the agent of an executor deployment: its Docker reads go to the executor.

    False in the executor itself (and its children), which reads Docker directly.
    """
    return executor_configured() and not os.environ.get(EXECUTOR_ROLE_ENV)


def _timeout() -> float:
    raw = os.environ.get("COMPOSER_EXECUTOR_TIMEOUT")
    try:
        value = float(raw) if raw is not None and str(raw).strip() else _DEFAULT_TIMEOUT
    except ValueError:
        return _DEFAULT_TIMEOUT
    return value if value > 0 else _DEFAULT_TIMEOUT


def _recv_exactly(conn: socket.socket):
    def reader(n: int) -> bytes:
        chunks = []
        remaining = n
        while remaining > 0:
            chunk = conn.recv(remaining)
            if not chunk:
                raise ExecutorClientError("executor closed the connection before a full result.")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    return reader


def send_request(op: str, payload: Dict, *, operation_id: Optional[str] = None, socket_path: Optional[str] = None,
                 timeout: Optional[float] = None) -> Dict:
    request = {
        "protocol_version": proto.EXECUTOR_PROTOCOL_VERSION,
        "operation_id": operation_id or str(uuid.uuid4()),
        "op": op,
        "payload": payload,
    }
    # Validate locally too (fail fast); the executor re-validates as the authority.
    request = proto.validate_executor_request(request)
    path = socket_path or resolve_socket_path()
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout or _timeout())
    try:
        conn.connect(path)
        conn.sendall(proto.encode_frame(request))
        return proto.read_frame(_recv_exactly(conn))
    except (OSError, proto.ProtocolError) as exc:
        raise ExecutorClientError(f"executor request failed: {exc}") from exc
    finally:
        conn.close()


def run_operation(op: str, payload: Dict, operation_id: str) -> Tuple[int, str]:
    """Run an op via the executor, returning ``(exit_code, detail)`` like the
    agent's legacy ``_run_child`` so the caller's reporting path is unchanged."""
    try:
        result = send_request(op, payload, operation_id=operation_id)
    except ExecutorClientError as exc:
        # The agent has no Docker fallback in executor mode; surface a clear
        # non-zero result so the operation is reported failed, not silently lost.
        return 127, str(exc)
    state = result.get("state")
    detail = str(result.get("detail") or "")
    if state == "succeeded":
        return int(result.get("exit_code", 0) or 0), detail
    if state == "rejected":
        return int(result.get("exit_code", 2) or 2), detail or "executor rejected the operation."
    return int(result.get("exit_code", 1) or 1), detail or "executor operation failed."


def read(op: str, payload: Dict) -> Dict:
    """Ask the executor for one of its reads; the result, or ``ValueError`` saying why not."""
    try:
        result = send_request(op, payload, timeout=READ_TIMEOUT)
    except ExecutorClientError as exc:
        raise ValueError(f"composer-executor could not be reached: {exc}") from exc
    if result.get("state") == "succeeded":
        return result
    detail = str(result.get("detail") or "")
    if "Unsupported executor op" in detail:
        raise ValueError(
            "composer-executor does not serve this read yet. "
            "Update both Composer resident services and retry."
        )
    raise ValueError(detail or f"composer-executor could not answer {op}.")
