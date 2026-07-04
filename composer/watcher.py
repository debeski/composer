"""Resident, trigger-driven updater loop for `composer watch`.

Composer stays a one-shot tool: this loop is a thin supervisor that watches a
trigger file and, on each new request, shells the existing `composer -uo`
pipeline (pull + version gate + recreate + health + post_start). Running the
update in a child process keeps all one-shot behavior (including per-run state
and exit codes) intact — no refactor of the launcher's run path.

Contract:
- Trigger file: JSON with a ``token`` field (any string), or any file (its
  ``mtime`` becomes the token). A changed token means "please update".
- Ack file: ``<trigger>.ack`` records the last processed token + child exit
  code + timestamp, so a request is processed once and survives a restart of
  the watcher container.
- Deploy status: the child writes ``COMPOSER_STATUS_FILE`` throughout the run;
  the watcher does not touch it (clean ownership split).
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _read_request_token(trigger: Path) -> Optional[str]:
    if not trigger.exists():
        return None
    try:
        data = json.loads(trigger.read_text(encoding="utf-8"))
        token = str(data.get("token") or "").strip()
        if token:
            return token
    except (OSError, ValueError, AttributeError):
        pass
    # Plain file / no token: use the modification time as an implicit token.
    try:
        return f"mtime:{trigger.stat().st_mtime_ns}"
    except OSError:
        return None


def _read_ack_token(ack: Path) -> Optional[str]:
    try:
        token = str(json.loads(ack.read_text(encoding="utf-8")).get("token") or "").strip()
        return token or None
    except (OSError, ValueError, AttributeError):
        return None


def _write_ack(ack: Path, token: str, exit_code: int):
    payload = {
        "token": token,
        "exit_code": exit_code,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        ack.parent.mkdir(parents=True, exist_ok=True)
        tmp = ack.with_name(f".{ack.name}.tmp")
        tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        os.replace(tmp, ack)
    except OSError:
        pass


def run_watch(args) -> int:
    trigger = Path(args.trigger_file)
    ack = Path(f"{trigger}.ack")
    interval = max(2.0, float(args.interval))

    child = [sys.executable, "-m", "composer", "-uo"]
    if args.dev:
        child.append("-d")
    if args.file:
        child.extend(["-f", args.file])

    env = os.environ.copy()
    if args.status_file:
        env["COMPOSER_STATUS_FILE"] = args.status_file

    last_token = _read_ack_token(ack)
    print(
        f"👀 composer watch — trigger={trigger} interval={interval:g}s "
        f"(last processed: {last_token or 'none'})",
        flush=True,
    )

    while True:
        token = _read_request_token(trigger)
        if token and token != last_token:
            print(
                f"⟳ update request {token} — running `composer -uo`",
                flush=True,
            )
            proc = subprocess.run(child, env=env)
            _write_ack(ack, token, proc.returncode)
            last_token = token
            result = "ready" if proc.returncode == 0 else f"failed (exit {proc.returncode})"
            print(f"✔ update {token} → {result}", flush=True)
            if args.once:
                return proc.returncode
        elif args.once:
            # Nothing pending and we were asked to do a single pass.
            return 0
        time.sleep(interval)
