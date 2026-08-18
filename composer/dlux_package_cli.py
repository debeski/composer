"""`composer dlux-update` — apply or roll back an inline DjangoLux release.

The plumbing between the trigger file / agent command and the orchestration in
``dlux_package_update``. This is where the injected `restart` and `health_check`
become real Docker work: a scoped `docker compose restart` of the services that
run DjangoLux, followed by the same health wait `composer update` uses.

`dlux-updater` is deliberately *not* restarted here. It is the service being
retired (removed in DjangoLux v1.9.0), it is labelled
``org.dlux.restart: "protected"``, and restarting it mid-update is exactly the
loop this design removes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .dlux_package_update import apply_package_update, rollback_package_update
from .dlux_runtime import DluxRuntime
from .service_selection import parse_service_list

DEFAULT_RUNTIME_ROOT = "/opt/dlux-runtime"
# Services that load DjangoLux from the runtime volume and must pick up a new
# release. `dlux-updater` is excluded on purpose (see the module docstring).
DEFAULT_RESTART_SERVICES = ("web", "celery")


def parse_dlux_update_args(argv):
    parser = argparse.ArgumentParser(
        prog="composer dlux-update",
        description="Apply or roll back an inline DjangoLux release on the runtime volume.",
    )
    parser.add_argument("mode", choices=("apply", "rollback"), nargs="?", default="apply")
    parser.add_argument("--version", default="", help="Target version (default: newest eligible)")
    parser.add_argument(
        "--runtime-root",
        default=os.environ.get("DLUX_UPDATE_RUNTIME_ROOT", DEFAULT_RUNTIME_ROOT),
        help="DjangoLux runtime volume root",
    )
    parser.add_argument(
        "--restart-service", action="append", dest="restart_services", default=None,
        metavar="SERVICE", help="Service to restart (repeatable; default: web, celery)",
    )
    parser.add_argument("-f", "--file", help="Alternate compose file")
    parser.add_argument("-d", "--dev", action="store_true", help="Use compose.dev.yml")
    parser.add_argument("--status-file", help="Write the result as JSON to PATH")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Resolve and verify the release, but do not activate or restart it",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Publish what is available to the runtime volume; install nothing",
    )
    parser.add_argument(
        "--availability-file", default=None,
        help="Where --check writes its result (default: state/package-available.json)",
    )
    return parser.parse_args(argv)


AVAILABILITY_FILENAME = "package-available.json"


def write_availability(runtime, payload, path=None) -> Path:
    """Publish availability where DjangoLux reads it, atomically."""
    target = Path(path) if path else (runtime.state_dir / AVAILABILITY_FILENAME)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return target


def build_availability_payload(target_version="") -> dict:
    """Resolve and verify the newest release, as a publishable report.

    A failure becomes a report too, never an exception: DjangoLux showing "could
    not check" is correct, and far better than it showing a stale "up to date"
    after PyPI became unreachable or an attestation stopped verifying.
    """
    from . import dlux_release_source as source

    try:
        described = source.describe(target_version)
    except Exception as exc:
        return {
            "checked_at": _utc_now(),
            "available": False,
            "version": "",
            "inline_safe": False,
            "reason": "",
            "error": str(exc),
        }
    return {
        "checked_at": _utc_now(),
        "available": True,
        "version": described["version"],
        "inline_safe": described["inline_safe"],
        "reason": described["reason"],
        "error": "",
    }


def run_availability_check(args, runtime) -> int:
    """`--check`: resolve, verify and publish. Never activates anything."""
    payload = build_availability_payload(args.version)
    path = write_availability(runtime, payload, args.availability_file)
    if payload["error"]:
        print(f"✖ {payload['error']}", file=sys.stderr)
        print(f"  published to {path}", file=sys.stderr)
        return 1
    state = "inline-safe" if payload["inline_safe"] else "requires an image rebuild"
    print(f"✔ DjangoLux {payload['version']} available ({state}) — published to {path}")
    return 0


def _utc_now() -> str:
    from datetime import datetime, timezone as _tz

    return datetime.now(_tz.utc).isoformat()


def _restart_services(args):
    if args.restart_services:
        return list(args.restart_services)
    configured = parse_service_list(os.environ.get("COMPOSER_DLUX_RESTART_SERVICES", ""))
    return configured or list(DEFAULT_RESTART_SERVICES)


def _build_operations(args, services):
    """Return `(restart, health_check)` bound to a configured launcher."""
    from .launcher import DockerComposeLauncher

    launcher = DockerComposeLauncher()
    launcher.compose_file = args.file
    launcher.dev_mode = args.dev
    launcher.resolve_active_compose_files()
    launcher.restart_mode = True
    launcher.restart_service = None
    launcher.restart_services = list(services)

    def restart():
        ok, _out, err = launcher.restart_containers()
        return ok, err or ""

    def health_check():
        # Scope the health wait to the services actually restarted, so an
        # unrelated unhealthy service does not veto a good update.
        launcher.services = list(services)
        return launcher.monitor_health()

    return restart, health_check


def _publish(path, result) -> None:
    if not path:
        return
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result.as_dict(), sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        pass


def run_dlux_update(args) -> int:
    runtime = DluxRuntime(args.runtime_root)
    if not runtime.exists():
        print(f"✖ No DjangoLux runtime volume at {args.runtime_root}.", file=sys.stderr)
        return 2

    if args.check:
        return run_availability_check(args, runtime)

    services = _restart_services(args)

    if args.dry_run:
        from . import dlux_release_source as source

        try:
            candidate, _unpacked = source.obtain(args.version)
        except Exception as exc:
            print(f"✖ {exc}", file=sys.stderr)
            return 1
        print(f"✔ {candidate.version} resolved and verified (dry run; nothing activated).")
        return 0

    restart, health_check = _build_operations(args, services)
    progress = lambda message: print(f"⟳ {message}", flush=True)

    if args.mode == "rollback":
        result = rollback_package_update(
            runtime, restart=restart, health_check=health_check, progress=progress
        )
    else:
        result = apply_package_update(
            runtime, restart=restart, health_check=health_check,
            target_version=args.version, progress=progress,
        )

    _publish(args.status_file, result)

    if result.ok:
        print(f"✔ {result.message}", flush=True)
        return 0
    if result.critical:
        # Distinct exit code: the deployment needs a human, and a caller that
        # retries on failure must not retry this.
        print(f"✖ CRITICAL: {result.message}", file=sys.stderr)
        return 3
    print(f"✖ {result.message}", file=sys.stderr)
    return 1
