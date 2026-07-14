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
from typing import List, Optional, Tuple

from .registry import remote_image_version, remote_tag_digest


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _local_repo_digest(image: str) -> Optional[str]:
    """The digest the local image for `image` was pulled at (its RepoDigest),
    via `docker image inspect` (honors DOCKER_HOST). None if not present."""
    repo = str(image).split("@", 1)[0].rsplit(":", 1)[0]
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", image, "--format", "{{json .RepoDigests}}"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        digests = json.loads(proc.stdout.strip() or "[]")
    except ValueError:
        return None
    for entry in digests:
        if isinstance(entry, str) and entry.startswith(f"{repo}@"):
            return entry.split("@", 1)[1]
    if digests and isinstance(digests[0], str) and "@" in digests[0]:
        return digests[0].split("@", 1)[1]
    return None


def check_availability(images: List[str]) -> Tuple[bool, list]:
    """Compare each image's remote tag digest to its local pulled digest."""
    token = os.environ.get("COMPOSER_REGISTRY_TOKEN") or None
    label = os.environ.get("COMPOSER_VERSION_LABEL") or None
    results = []
    any_new = False
    for image in images:
        remote = remote_tag_digest(image, token=token)
        local = _local_repo_digest(image)
        # A difference (or a remote we have never pulled) means an update exists.
        # An unreadable remote is "unknown" — never a false positive.
        new = bool(remote) and remote != local
        any_new = any_new or new
        entry = {
            "image": image,
            "remote_digest": remote,
            "local_digest": local,
            "update_available": new,
        }
        # Best-effort: publish the remote image's own version (OCI version label)
        # so dlux can show "vX available" instead of a digest. Only looked up when
        # an update exists (avoids an extra registry round-trip on every poll); any
        # failure is simply omitted and downstream falls back to the digest.
        if new:
            try:
                version = remote_image_version(image, token=token, label=label)
            except Exception:
                version = None
            if version:
                entry["version"] = version
        results.append(entry)
    return any_new, results


def write_availability(path: str, images: List[str]):
    any_new, results = check_availability(images)
    payload = {"available": any_new, "checked_at": _now_iso(), "images": results}
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.tmp")
        tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        pass


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

    # Console log for the live progress page: the -uo child appends clean,
    # ANSI-free progress lines to this file (via COMPOSER_LOG_FILE). Defaults to
    # a sibling of the status file so a proxy can serve both from one dir.
    log_file = getattr(args, "log_file", None)
    if not log_file and args.status_file:
        log_file = str(Path(args.status_file).with_name("deploy-log.txt"))
    if log_file:
        env["COMPOSER_LOG_FILE"] = log_file

    # Optional registry-availability check: publish whether a newer image than
    # the running one exists, so another process (dlux) can offer an update.
    check_images = list(getattr(args, "check_image", None) or [])
    availability_file = getattr(args, "availability_file", None)
    check_interval = max(60.0, float(getattr(args, "check_interval", 3600.0) or 3600.0))
    availability_enabled = bool(check_images and availability_file)
    next_check = 0.0  # run the first availability check immediately

    def maybe_check_availability(force=False):
        nonlocal next_check
        if not availability_enabled:
            return
        if force or time.monotonic() >= next_check:
            write_availability(availability_file, check_images)
            next_check = time.monotonic() + check_interval

    last_token = _read_ack_token(ack)
    print(
        f"👀 composer watch — trigger={trigger} interval={interval:g}s"
        + (f" · availability check every {check_interval:g}s for {', '.join(check_images)}"
           if availability_enabled else ""),
        flush=True,
    )

    while True:
        maybe_check_availability()
        token = _read_request_token(trigger)
        if token and token != last_token:
            print(
                f"⟳ update request {token} — running `composer -uo`",
                flush=True,
            )
            # Fresh console log per update run (the child appends to it).
            if log_file:
                try:
                    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
                    Path(log_file).write_text("", encoding="utf-8")
                except OSError:
                    pass
            proc = subprocess.run(child, env=env)
            _write_ack(ack, token, proc.returncode)
            last_token = token
            result = "ready" if proc.returncode == 0 else f"failed (exit {proc.returncode})"
            print(f"✔ update {token} → {result}", flush=True)
            # The running image just changed — refresh availability so the
            # "update available" signal clears promptly.
            maybe_check_availability(force=True)
            if args.once:
                return proc.returncode
        elif args.once:
            # Nothing pending and we were asked to do a single pass.
            return 0
        time.sleep(interval)
