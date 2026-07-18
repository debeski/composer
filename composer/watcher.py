"""Resident, trigger-driven updater loop for `composer watch`.

Composer stays a one-shot tool: this loop is a thin supervisor that watches a
trigger file and, on each new request, shells the existing `composer -u`
pipeline (pull + version gate + recreate + health + post_start). Running the
update in a child process keeps all one-shot behavior (including per-run state
and exit codes) intact — no refactor of the launcher's run path.

Contract:
- Trigger file: JSON with a ``token`` field (any string), or any file (its
  ``mtime`` becomes the token). A changed token means "please update".
- Ack file: ``<trigger>.ack`` records the last processed token + child exit
  code + timestamp, so a request is processed once and survives a restart of
  the watcher container.
- Deploy status: the child writes ``COMPOSER_STATUS_FILE`` throughout the run.
  If the child exits non-zero before publishing ``failed``, the watcher writes
  that terminal state itself so downstream maintenance cannot remain stuck.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from .registry import DEFAULT_VERSION_LABEL, remote_image_labels, remote_tag_digest
from .service_selection import join_service_list, parse_service_list


DEFAULT_RELEASE_MANIFEST_LABEL = "org.dlux.project.release-manifest"
_MAX_MANIFEST_LABEL_BYTES = 16384


def _release_manifest_from_label(value) -> Optional[dict]:
    """Normalize optional project release metadata from an image label.

    A missing, malformed, unsupported, or empty manifest is simply absent from
    availability output. The digest signal and optional version label remain
    independent.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    if len(value.encode("utf-8")) > _MAX_MANIFEST_LABEL_BYTES:
        return None
    try:
        source = json.loads(value)
    except (TypeError, ValueError):
        return None
    schema_version = source.get("schema_version", 1) if isinstance(source, dict) else None
    if (
        not isinstance(source, dict)
        or isinstance(schema_version, bool)
        or schema_version not in (1, "1")
    ):
        return None

    manifest = {"schema_version": 1}
    version = source.get("version")
    if isinstance(version, str) and version.strip():
        manifest["version"] = version.strip()[:64]
    summary = source.get("summary")
    if isinstance(summary, str) and summary.strip():
        manifest["summary"] = summary.strip()[:1000]
    highlights = source.get("highlights")
    if isinstance(highlights, list):
        clean_highlights = []
        for item in highlights[:8]:
            if isinstance(item, str) and item.strip():
                clean_highlights.append(item.strip()[:160])
        if clean_highlights:
            manifest["highlights"] = clean_highlights
    release_url = source.get("release_url")
    if isinstance(release_url, str):
        release_url = release_url.strip()
        if release_url.startswith("https://"):
            manifest["release_url"] = release_url[:2048]
    return manifest if len(manifest) > 1 else None


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
    version_label = os.environ.get("COMPOSER_VERSION_LABEL") or DEFAULT_VERSION_LABEL
    manifest_label = (
        os.environ.get("COMPOSER_RELEASE_MANIFEST_LABEL")
        or DEFAULT_RELEASE_MANIFEST_LABEL
    )
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
        # Best-effort image metadata. Version and project release manifest are
        # independently optional and share one config lookup. Metadata failure
        # never changes the digest-driven availability result.
        if new:
            try:
                labels = remote_image_labels(image, token=token) or {}
            except Exception:
                labels = {}
            version = str(labels.get(version_label) or "").strip()
            if version:
                entry["version"] = version
            manifest = _release_manifest_from_label(labels.get(manifest_label))
            if manifest:
                entry["manifest"] = manifest
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


def _publish_terminal_failure(
    status_file: str,
    token: str,
    exit_code: int,
    error: str = "",
) -> bool:
    """Guarantee a terminal deploy status after a failed child process.

    Preserve a detailed error already published by the child. The request token
    also lets consumers reject a terminal record from an older update.
    """
    target = Path(status_file)
    try:
        current = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(current, dict):
            current = {}
    except (OSError, ValueError, AttributeError):
        current = {}

    detail = str(error or "").strip()
    if not detail and current.get("status") == "failed":
        detail = str(current.get("error") or "").strip()
    if not detail:
        detail = f"Composer update process exited with status {exit_code}."

    payload = {
        **current,
        "status": "failed",
        "updated_at": _now_iso(),
        "error": detail[:4000],
        "request_token": token,
        "exit_code": int(exit_code),
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.tmp")
        tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        os.replace(tmp, target)
        return True
    except OSError:
        return False


def _append_terminal_failure(log_file: Optional[str], error: str):
    if not log_file:
        return
    try:
        target = Path(log_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as stream:
            stream.write(f"\nUpdate failed: {error}\n")
    except OSError:
        pass


def run_watch(args) -> int:
    trigger = Path(args.trigger_file)
    ack = Path(f"{trigger}.ack")
    interval = max(2.0, float(args.interval))

    child = [sys.executable, "-m", "composer", "-u"]
    if args.dev:
        child.append("-d")
    if args.file:
        child.extend(["-f", args.file])

    env = os.environ.copy()
    if args.status_file:
        env["COMPOSER_STATUS_FILE"] = args.status_file

    excluded = parse_service_list(env.get("COMPOSER_EXCLUDE_SERVICES"))
    self_service_raw = env.get("COMPOSER_WATCH_SELF_SERVICE")
    self_services = (
        ["composer-updater"]
        if self_service_raw is None
        else parse_service_list(self_service_raw)
    )
    for service in self_services:
        if service not in excluded:
            excluded.append(service)
    if excluded:
        env["COMPOSER_EXCLUDE_SERVICES"] = join_service_list(excluded)

    # Console log for the live progress page: the -u child appends clean,
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
                f"⟳ update request {token} — running `composer -u`",
                flush=True,
            )
            # Fresh console log per update run (the child appends to it).
            if log_file:
                try:
                    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
                    Path(log_file).write_text("", encoding="utf-8")
                except OSError:
                    pass
            launch_error = ""
            try:
                exit_code = subprocess.run(child, env=env).returncode
            except (OSError, subprocess.SubprocessError) as exc:
                exit_code = 127
                launch_error = f"Composer update process could not start: {exc}"
            if exit_code != 0:
                fallback_error = launch_error or f"Composer update process exited with status {exit_code}."
                if args.status_file:
                    _publish_terminal_failure(
                        args.status_file,
                        token,
                        exit_code,
                        error=launch_error,
                    )
                _append_terminal_failure(log_file, fallback_error)
            _write_ack(ack, token, exit_code)
            last_token = token
            result = "ready" if exit_code == 0 else f"failed (exit {exit_code})"
            print(f"✔ update {token} → {result}", flush=True)
            # The running image just changed — refresh availability so the
            # "update available" signal clears promptly.
            maybe_check_availability(force=True)
            if args.once:
                return exit_code
        elif args.once:
            # Nothing pending and we were asked to do a single pass.
            return 0
        time.sleep(interval)
