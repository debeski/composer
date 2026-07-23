import json
import os
import re
import uuid
from datetime import datetime
from typing import Any, Dict


SCHEMA_VERSION = 1
MAX_COMMAND_BYTES = 65536
MAX_EVENT_TEXT = 32768
REMOTE_ACTIONS = frozenset({
    "dlux.image_update",
    "dlux.backup.create",
    "composer.restart",
    "composer.recovery_deploy",
    "agent.rotate_credentials",
})
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})
COMMAND_FIELDS = frozenset(
    {"schema_version", "operation_id", "action", "created_at", "deadline_at", "actor", "payload"}
)
_SENSITIVE_RE = re.compile(
    r"(?i)(authorization|password|passwd|secret|token|api[_-]?key)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)


class ProtocolError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now().astimezone().isoformat()


def _bounded_text(value: Any, limit: int) -> str:
    return str(value or "").replace("\x00", "").strip()[:limit]


def _timestamp(value: Any, label: str) -> tuple[str, datetime]:
    text = _bounded_text(value, 64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProtocolError(f"{label} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise ProtocolError(f"{label} must include a timezone.")
    return text, parsed


def _require_payload_fields(payload: Dict[str, Any], allowed: set[str]):
    unexpected = set(payload) - allowed
    if unexpected:
        raise ProtocolError(f"Unsupported payload field: {sorted(unexpected)[0]}.")


def validate_command(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError("Command must be a JSON object.")
    try:
        encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError("Command is not valid JSON.") from exc
    if len(encoded) > MAX_COMMAND_BYTES:
        raise ProtocolError("Command exceeds the 64 KiB limit.")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ProtocolError("Unsupported command schema version.")
    unexpected = set(value) - COMMAND_FIELDS
    if unexpected:
        raise ProtocolError(f"Unsupported command field: {sorted(unexpected)[0]}.")

    raw_id = str(value.get("operation_id") or "").strip()
    try:
        operation_id = str(uuid.UUID(raw_id))
    except (ValueError, AttributeError) as exc:
        raise ProtocolError("operation_id must be a UUID.") from exc

    action = str(value.get("action") or "").strip()
    if action not in REMOTE_ACTIONS:
        raise ProtocolError(f"Unsupported remote action: {action or '<empty>'}.")
    payload = value.get("payload") or {}
    if not isinstance(payload, dict):
        raise ProtocolError("Command payload must be an object.")

    if action == "dlux.image_update":
        _require_payload_fields(payload, {"backup_mode"})
        mode = str(payload.get("backup_mode") or "data").strip().lower()
        if mode not in {"data", "full", "skip"}:
            raise ProtocolError("backup_mode must be data, full, or skip.")
        payload = {"backup_mode": mode}
    elif action == "dlux.backup.create":
        _require_payload_fields(payload, {"backup_mode"})
        mode = str(payload.get("backup_mode") or "data").strip().lower()
        if mode not in {"data", "full"}:
            raise ProtocolError("backup_mode must be data or full.")
        payload = {"backup_mode": mode}
    elif action == "composer.restart":
        _require_payload_fields(payload, {"service"})
        service = str(payload.get("service") or "").strip()
        if service and not re.fullmatch(r"[A-Za-z0-9_-]+", service):
            raise ProtocolError("Restart service name is invalid.")
        payload = {"service": service}
    elif action == "composer.recovery_deploy":
        _require_payload_fields(payload, {"force", "reason"})
        payload = {
            "force": bool(payload.get("force", False)),
            "reason": _bounded_text(payload.get("reason"), 1000),
        }
        if not payload["reason"]:
            raise ProtocolError("A recovery deployment requires a reason.")
    else:
        _require_payload_fields(payload, set())
        payload = {}

    created_at, created = _timestamp(value.get("created_at"), "created_at")
    deadline_at, deadline = _timestamp(value.get("deadline_at"), "deadline_at")
    if deadline < created:
        raise ProtocolError("deadline_at cannot precede created_at.")

    actor = value.get("actor") or {}
    if not isinstance(actor, dict):
        actor = {}
    return {
        "schema_version": SCHEMA_VERSION,
        "operation_id": operation_id,
        "action": action,
        "created_at": created_at,
        "deadline_at": deadline_at,
        "actor": {
            "id": _bounded_text(actor.get("id"), 150),
            "display": _bounded_text(actor.get("display"), 200),
        },
        "payload": payload,
    }


def inherited_secret_values() -> list[str]:
    keys = str(os.environ.get("COMPOSER_INHERITED_SECRET_KEYS") or "").split(",")
    values = []
    for key in keys:
        key = key.strip()
        value = os.environ.get(key) if key else None
        if value and len(value) >= 4:
            values.append(value)
    return sorted(set(values), key=len, reverse=True)


def redact_text(value: Any) -> str:
    text = _bounded_text(value, MAX_EVENT_TEXT)
    for secret in inherited_secret_values():
        text = text.replace(secret, "[REDACTED]")
    return _SENSITIVE_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)
