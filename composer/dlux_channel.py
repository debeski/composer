"""Composer's view of the DjangoLux release channel.

The channel is a property of the *deployment*, not of Composer: an administrator
sets it in DjangoLux's Options, and the DjangoLux worker publishes it to
``state/channel-policy.json`` on the runtime volume. Composer reads that file to
decide which releases it may resolve, and never writes it — one writer, and it
is the process that owns the database row the file mirrors.

``composer dlux channel beta`` therefore does not set anything directly. It
leaves a request beside the policy for that same worker to apply and
acknowledge, which is how every other cross-boundary mutation on this volume
already works. The command reports what it observes, including "requested, not
yet applied" — a channel it cannot verify is not a channel it should claim.

Stable is the answer to every doubt: no file, an unreadable file, a schema from
a future release. A deployment that has not opted in is never handed a beta
because a file was missing or corrupt.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

STABLE = "stable"
BETA = "beta"
CHANNELS = (STABLE, BETA)

# Mirrored from dlux/updater/channel.py — keep in step.
POLICY_SCHEMA_VERSION = 1
POLICY_FILENAME = "channel-policy.json"
REQUEST_FILENAME = "channel-request.json"
ACK_FILENAME = f"{REQUEST_FILENAME}.ack"


class ChannelError(RuntimeError):
    """The requested channel is not one this Composer understands."""


def normalize_channel(value) -> str:
    text = str(value or "").strip().lower()
    if text not in CHANNELS:
        raise ChannelError(
            f"'{value}' is not a DjangoLux release channel; expected one of: "
            + ", ".join(CHANNELS)
            + "."
        )
    return text


def prereleases_allowed(channel) -> bool:
    return str(channel or "").strip().lower() == BETA


def read_policy(state_dir) -> tuple:
    """``(channel, error)`` from the published policy. Never raises."""
    path = Path(state_dir) / POLICY_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return STABLE, ""
    except OSError as exc:
        return STABLE, f"The release channel policy could not be read ({exc})."
    try:
        data = json.loads(raw)
    except ValueError:
        return STABLE, "The release channel policy is not valid JSON."
    if not isinstance(data, dict):
        return STABLE, "The release channel policy is not an object."
    schema = data.get("schema_version")
    if not isinstance(schema, int) or isinstance(schema, bool) or schema < 1:
        return STABLE, "The release channel policy has an invalid schema version."
    if schema > POLICY_SCHEMA_VERSION:
        return STABLE, (
            f"The release channel policy uses schema {schema}, which this Composer "
            "does not understand; staying on the stable channel."
        )
    channel = str(data.get("channel") or "").strip().lower()
    if channel not in CHANNELS:
        return STABLE, "The release channel policy names an unknown channel."
    return channel, ""


def request_channel(state_dir, channel, *, requested_by="composer-cli", token="") -> dict:
    """Leave a channel change for the DjangoLux worker to apply."""
    channel = normalize_channel(channel)
    payload = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "token": str(token or uuid.uuid4()),
        "channel": channel,
        "requested_at": _utc_now(),
        "requested_by": str(requested_by or "")[:150],
    }
    _atomic_json(Path(state_dir) / REQUEST_FILENAME, payload)
    return payload


def read_request(state_dir) -> dict:
    return _read_json(Path(state_dir) / REQUEST_FILENAME)


def read_ack(state_dir) -> dict:
    return _read_json(Path(state_dir) / ACK_FILENAME)


def describe(state_dir) -> dict:
    """Everything the CLI needs to report honestly, in one read."""
    channel, error = read_policy(state_dir)
    request = read_request(state_dir)
    ack = read_ack(state_dir)
    token = str(request.get("token") or "").strip()
    acked = str(ack.get("token") or "").strip() == token if token else False
    pending = ""
    failed = ""
    if token and not acked:
        pending = str(request.get("channel") or "").strip().lower()
    elif token and acked and not ack.get("applied", True):
        failed = str(ack.get("error") or "The DjangoLux worker refused the channel change.")
    return {
        "channel": channel,
        "error": error,
        "pending": pending if pending in CHANNELS else "",
        "failed": failed,
    }


def _utc_now() -> str:
    return datetime.now(dt_timezone.utc).isoformat()


def _read_json(path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _atomic_json(path, payload) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path
