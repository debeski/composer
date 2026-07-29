"""Typed contract for the private agent -> executor Unix socket.

The executor is the only holder of Docker authority. The network-facing agent
sends a small, fixed set of typed requests for control-plane-initiated
operations; the executor re-validates every one (defense in depth) and refuses
anything else. A ``protocol_version`` handshake makes a transient agent/executor
version skew fail safe instead of misinterpreting an operation.

Image updates are NOT socket requests: they stay file-triggered and are executed
by the executor's watcher loop. Backups are DjangoLux-side (no Docker). So the
socket surface here is deliberately just the two agent-initiated Docker ops:
``restart`` and ``recovery_deploy``.
"""

import json
import re
import uuid
from typing import Any, Callable, Dict

from .agent_protocol import ProtocolError, redact_text

# Bumped only on an incompatible request/result shape change. The executor
# rejects any request whose protocol_version does not exactly equal this.
EXECUTOR_PROTOCOL_VERSION = 1

# Bound the framed request/result the same way commands are bounded (64 KiB).
MAX_EXECUTOR_MESSAGE_BYTES = 65536
# The 4-byte big-endian length prefix caps a frame body well under the limit.
_LENGTH_PREFIX_BYTES = 4

# The agent-initiated Docker operations that travel over the socket. Note the
# absence of image_update (file-triggered), backup (DLUX-side), and
# rotate_credentials (agent-local) — none of them belong on this surface.
EXECUTOR_OPS = frozenset({"restart", "recovery_deploy"})

REQUEST_FIELDS = frozenset({"protocol_version", "operation_id", "op", "payload"})
RESULT_STATES = frozenset({"succeeded", "failed", "rejected"})

_SERVICE_RE = re.compile(r"[A-Za-z0-9_-]+")


def _clip(value: Any, limit: int) -> str:
    return str(value or "").replace("\x00", "").strip()[:limit]


def _require_payload_fields(payload: Dict[str, Any], allowed: set) -> None:
    unexpected = set(payload) - allowed
    if unexpected:
        raise ProtocolError(f"Unsupported payload field: {sorted(unexpected)[0]}.")


def validate_executor_request(value: Any) -> Dict[str, Any]:
    """Parse and validate an agent->executor request. Raises ``ProtocolError``.

    This is the security boundary: the executor calls this on every request and
    executes nothing that does not pass. It never trusts the agent to have
    validated first.
    """
    if not isinstance(value, dict):
        raise ProtocolError("Executor request must be a JSON object.")
    try:
        encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError("Executor request is not valid JSON.") from exc
    if len(encoded) > MAX_EXECUTOR_MESSAGE_BYTES:
        raise ProtocolError("Executor request exceeds the 64 KiB limit.")

    # Version handshake first: refuse anything we do not exactly speak, so a
    # skewed agent can never coax an older executor into guessing.
    if value.get("protocol_version") != EXECUTOR_PROTOCOL_VERSION:
        raise ProtocolError("Unsupported executor protocol version.")

    unexpected = set(value) - REQUEST_FIELDS
    if unexpected:
        raise ProtocolError(f"Unsupported request field: {sorted(unexpected)[0]}.")

    raw_id = str(value.get("operation_id") or "").strip()
    try:
        operation_id = str(uuid.UUID(raw_id))
    except (ValueError, AttributeError) as exc:
        raise ProtocolError("operation_id must be a UUID.") from exc

    op = str(value.get("op") or "").strip()
    if op not in EXECUTOR_OPS:
        raise ProtocolError(f"Unsupported executor op: {op or '<empty>'}.")

    payload = value.get("payload") or {}
    if not isinstance(payload, dict):
        raise ProtocolError("Executor request payload must be an object.")

    if op == "restart":
        _require_payload_fields(payload, {"service"})
        service = _clip(payload.get("service"), 200)
        if service and not _SERVICE_RE.fullmatch(service):
            raise ProtocolError("Restart service name is invalid.")
        payload = {"service": service}
    elif op == "recovery_deploy":
        _require_payload_fields(payload, {"force", "reason"})
        force = payload.get("force", False)
        if not isinstance(force, bool):
            raise ProtocolError("force must be a JSON boolean.")
        reason = _clip(payload.get("reason"), 1000)
        if not reason:
            raise ProtocolError("A recovery deployment requires a reason.")
        payload = {"force": force, "reason": reason}

    return {
        "protocol_version": EXECUTOR_PROTOCOL_VERSION,
        "operation_id": operation_id,
        "op": op,
        "payload": payload,
    }


def build_result(operation_id: str, state: str, *, exit_code: int = 0, detail: str = "") -> Dict[str, Any]:
    """Build a redacted, typed executor result. ``detail`` is always redacted."""
    if state not in RESULT_STATES:
        raise ProtocolError(f"Invalid executor result state: {state}.")
    return {
        "protocol_version": EXECUTOR_PROTOCOL_VERSION,
        "operation_id": _clip(operation_id, 64),
        "state": state,
        "exit_code": int(exit_code),
        "detail": redact_text(detail),
    }


def encode_frame(obj: Dict[str, Any]) -> bytes:
    """Encode one length-prefixed JSON frame (4-byte big-endian length + body)."""
    body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_EXECUTOR_MESSAGE_BYTES:
        raise ProtocolError("Executor message exceeds the 64 KiB limit.")
    return len(body).to_bytes(_LENGTH_PREFIX_BYTES, "big") + body


def read_frame(recv_exactly: Callable[[int], bytes]) -> Dict[str, Any]:
    """Read one bounded length-prefixed frame using ``recv_exactly(n) -> bytes``.

    ``recv_exactly`` must return exactly ``n`` bytes or raise. Kept transport-
    agnostic so it is unit-testable without a real socket.
    """
    header = recv_exactly(_LENGTH_PREFIX_BYTES)
    length = int.from_bytes(header, "big")
    if length <= 0:
        raise ProtocolError("Executor frame has a non-positive length.")
    if length > MAX_EXECUTOR_MESSAGE_BYTES:
        raise ProtocolError("Executor frame exceeds the 64 KiB limit.")
    body = recv_exactly(length)
    try:
        value = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProtocolError("Executor frame is not valid JSON.") from exc
    if not isinstance(value, dict):
        raise ProtocolError("Executor frame must be a JSON object.")
    return value
