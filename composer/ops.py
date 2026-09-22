"""Named operations DjangoLux asks the resident Composer to perform.

DjangoLux's Operations card writes `state/ops-request.json` naming ONE operation
from ``HANDLERS`` below; this module performs it and publishes
`state/ops-result.json` plus `ops-request.json.ack`, both carrying the request's
token so a stale document can never be read as the current answer. The shape is
the package-update handoff's, for the same reason: it already survives
restarts, crashes and a request nobody is left to answer.

Phase 1 is read-only. An operation that is not in ``HANDLERS`` is refused with a
message rather than attempted, and nothing here takes an argument from the
request beyond the operation's own name — the request cannot carry a command,
a path or a flag, so there is nothing for a compromised database to smuggle
through. See the Operations Centre plan in the DjangoLux repository.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .agent_protocol import redact_text

REQUEST_FILENAME = "ops-request.json"
ACK_FILENAME = f"{REQUEST_FILENAME}.ack"
RESULT_FILENAME = "ops-result.json"

#: A result document is rendered in a browser; a runaway check must not become a
#: multi-megabyte payload on the runtime volume.
MAX_FINDINGS = 200
MAX_MESSAGE_CHARS = 2000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _trim(findings) -> list:
    """Bound and redact what a result carries back to the browser."""
    clean = []
    for finding in (findings or [])[:MAX_FINDINGS]:
        if not isinstance(finding, dict):
            continue
        entry = {
            "level": str(finding.get("level") or "ok")[:16],
            "name": str(finding.get("name") or "")[:128],
            "message": redact_text(str(finding.get("message") or ""))[:MAX_MESSAGE_CHARS],
        }
        if finding.get("fix"):
            entry["fix"] = redact_text(str(finding["fix"]))[:MAX_MESSAGE_CHARS]
        clean.append(entry)
    return clean


def _run_check(runtime) -> dict:
    """`composer check`, as a document. Read-only: never `--fix`."""
    from types import SimpleNamespace

    from .launcher import DockerComposeLauncher

    launcher = DockerComposeLauncher()
    args = SimpleNamespace(
        file=getattr(runtime.args, "file", None),
        dev=getattr(runtime.args, "dev", False),
        fix=False, yes=False, deep=False,
        deep_service="web", deep_command="python manage.py dlux_doctor",
        json=True, beta=False, stable=False,
    )
    results, _fixed = launcher.collect_checkup(args)
    return {
        "exit_code": 1 if any(r.get("level") == "fail" for r in results) else 0,
        "composer_version": launcher.composer_version,
        "findings": _trim(results),
    }


#: operation name -> callable(runtime) -> result dict. Nothing else can run.
HANDLERS = {
    "check": _run_check,
}


class OpsResponder:
    """Answers one DjangoLux operation request per tick, at most once each."""

    def __init__(self, runtime):
        self.runtime = runtime
        state_dir = runtime.package_trigger.parent
        self.request = state_dir / REQUEST_FILENAME
        self.ack = state_dir / ACK_FILENAME
        self.result = state_dir / RESULT_FILENAME
        self.last_token: Optional[str] = str(_read_json(self.ack).get("token") or "") or None

    def pending(self) -> Optional[dict]:
        request = _read_json(self.request)
        token = str(request.get("token") or "").strip()
        if not token or token == self.last_token:
            return None
        return request

    def answer(self) -> Optional[str]:
        """Perform a pending operation and publish its result. Never raises."""
        request = self.pending()
        if request is None:
            return None
        token = str(request["token"])
        operation = str(request.get("operation") or "").strip().lower()
        handler = HANDLERS.get(operation)
        error = ""
        result = {"exit_code": 2, "findings": []}
        if handler is None:
            error = (
                f"This Composer does not perform the operation {operation!r}. "
                "Update Composer, or run it on the host."
            )
        else:
            try:
                answer = handler(self.runtime)
            except Exception as exc:  # noqa: BLE001 - reported, never raised at the loop
                error = f"The operation failed: {redact_text(str(exc))}"[:MAX_MESSAGE_CHARS]
            else:
                if isinstance(answer, dict):
                    result = answer
                else:
                    error = f"The operation {operation!r} returned no usable result."

        payload = {
            "schema_version": 1,
            "token": token,
            "operation": operation,
            "finished_at": _now(),
            "error": error,
            **result,
        }
        # Result first, then the ack: DjangoLux reads the result only after it
        # sees the ack, so an ack can never point at a result that is not there.
        try:
            _atomic_json(self.result, payload)
            _atomic_json(self.ack, {
                "token": token,
                "operation": operation,
                "exit_code": payload.get("exit_code", 0),
                "error": error,
                "finished_at": payload["finished_at"],
            })
        except OSError as exc:
            print(f"⚠ operation {operation} could not be published: {redact_text(str(exc))}", flush=True)
            return None
        self.last_token = token
        return token
