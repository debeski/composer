"""`composer dlux` — check, apply, or roll back an inline DjangoLux release.

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


def parse_dlux_update_args(argv, *, action="update"):
    if action not in {"check", "update", "rollback"}:
        raise ValueError(f"unsupported dlux action: {action}")
    parser = argparse.ArgumentParser(
        prog=f"composer dlux {action}",
        description="Check, apply, or roll back an inline DjangoLux release on the runtime volume.",
    )
    parser.add_argument("--version", default="", help="Target version (default: newest eligible)")
    parser.add_argument(
        "--runtime-root",
        default=os.environ.get("DLUX_UPDATE_RUNTIME_ROOT", DEFAULT_RUNTIME_ROOT),
        help="DjangoLux runtime volume root",
    )
    parser.add_argument("-f", "--file", help="Alternate compose file")
    parser.add_argument("-d", "--dev", action="store_true", help="Use compose.dev.yml")
    parser.add_argument("--status-file", help="Write the result as JSON to PATH")
    if action == "check":
        parser.add_argument(
            "--availability-file", default=None,
            help="Where the check writes its result (default: state/package-available.json)",
        )
    else:
        parser.set_defaults(availability_file=None)
        parser.add_argument(
            "--restart-service", action="append", dest="restart_services", default=None,
            metavar="SERVICE", help="Service to restart (repeatable; default: web, celery)",
        )
    if action == "update":
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Resolve and verify the release, but do not activate or restart it",
        )
        parser.add_argument(
            "--staged-wheel", default="", metavar="FILENAME",
            help="Apply a wheel already staged in the runtime volume's downloads/ (no network)",
        )
        parser.add_argument(
            "--staged-sha256", default="", metavar="HEX",
            help="The digest --staged-wheel must hash to; required with it",
        )
    else:
        parser.set_defaults(dry_run=False, staged_wheel="", staged_sha256="")
    parser.add_argument(
        # Set on the child composer this command starts when the runtime volume
        # is not mounted where it runs. Not for hand use: it turns the missing
        # volume back into the plain failure it is inside that container.
        "--no-delegate", action="store_true", help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    args.action = action
    args.mode = "rollback" if action == "rollback" else "apply"
    args.check = action == "check"
    return args


AVAILABILITY_FILENAME = "package-available.json"


def write_availability(runtime, payload, path=None) -> Path:
    """Publish availability where DjangoLux reads it, atomically."""
    target = Path(path) if path else (runtime.state_dir / AVAILABILITY_FILENAME)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return target


def build_availability_payload(target_version="", *, channel=None, runtime=None) -> dict:
    """Resolve and verify the newest release, as a publishable report.

    A failure becomes a report too, never an exception: DjangoLux showing "could
    not check" is correct, and far better than it showing a stale "up to date"
    after PyPI became unreachable or an attestation stopped verifying.
    """
    from . import dlux_channel
    from . import dlux_release_source as source

    policy_error = ""
    if channel is None:
        if runtime is None:
            channel = dlux_channel.STABLE
        else:
            channel, policy_error = dlux_channel.read_policy(runtime.state_dir)
    try:
        described = source.describe(target_version, channel=channel)
    except Exception as exc:
        return {
            "checked_at": _utc_now(),
            "available": False,
            "version": "",
            "inline_safe": False,
            "channel": channel,
            "reason": "",
            # A policy that could not be read is reported alongside the real
            # failure rather than instead of it: the operator needs to know the
            # check ran on stable because the policy was unreadable, not just
            # that it found nothing.
            "error": " ".join(part for part in (str(exc), policy_error) if part),
        }
    return {
        "checked_at": _utc_now(),
        "available": True,
        "version": described["version"],
        "inline_safe": described["inline_safe"],
        "channel": described.get("channel", channel),
        "prerelease": bool(described.get("prerelease")),
        "reason": described["reason"],
        "error": policy_error,
    }


def run_availability_check(args, runtime) -> int:
    """`composer dlux check`: resolve, verify and publish. Never activates anything."""
    payload = build_availability_payload(args.version, runtime=runtime)
    path = write_availability(runtime, payload, args.availability_file)
    if not payload.get("available"):
        print(f"✖ {payload['error']}", file=sys.stderr)
        print(f"  published to {path}", file=sys.stderr)
        return 1
    state = "inline-safe" if payload["inline_safe"] else "requires an image rebuild"
    channel_note = f", {payload['channel']} channel" if payload.get("channel") else ""
    print(f"✔ DjangoLux {payload['version']} available ({state}{channel_note}) — published to {path}")
    if payload.get("error"):
        print(f"  note: {payload['error']}", file=sys.stderr)
    return 0


def _resolved_channel(runtime) -> str:
    """The published channel for this deployment, or stable when unreadable."""
    from . import dlux_channel

    channel, _error = dlux_channel.read_policy(runtime.state_dir)
    return channel


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


def _staged_source(args, runtime):
    """The release source for `--staged-wheel`, or None for the network one.

    The executor applies releases this way: it holds the Docker authority but no
    egress, so the agent fetches and verifies the wheel and hands over only what
    it must hash to. See composer/dlux_package_stage.py.
    """
    if not args.staged_wheel:
        if args.staged_sha256:
            raise ValueError("--staged-sha256 means nothing without --staged-wheel.")
        return None
    if args.mode != "apply":
        raise ValueError("--staged-wheel applies a release; it cannot be rolled back.")
    if not args.staged_sha256 or not args.version:
        raise ValueError("--staged-wheel requires --staged-sha256 and --version.")
    from .dlux_package_stage import StagedRelease

    return StagedRelease(
        runtime, filename=args.staged_wheel, sha256=args.staged_sha256, version=args.version,
    )


def _without_the_runtime_volume(args, argv) -> int:
    """The runtime root is a container path; find it and re-run there.

    The deployer CLI runs in a container (or on a host) with no runtime mount,
    which is not an error — it is the normal way an operator types this command.
    See composer/dlux_runtime_access.py for why the work goes to a sibling
    container rather than to a service of the stack.
    """
    if args.no_delegate:
        print(f"✖ No DjangoLux runtime volume at {args.runtime_root}.", file=sys.stderr)
        return 2
    from .dlux_runtime_access import RuntimeVolumeError, delegate_dlux_update

    try:
        return delegate_dlux_update(args, list(argv or []))
    except RuntimeVolumeError as exc:
        print(f"✖ {exc}", file=sys.stderr)
        return 2


def run_dlux_update(args, argv=None) -> int:
    runtime = DluxRuntime(args.runtime_root)
    if not runtime.exists():
        return _without_the_runtime_volume(args, argv)

    if args.check:
        return run_availability_check(args, runtime)

    services = _restart_services(args)

    try:
        staged = _staged_source(args, runtime)
    except ValueError as exc:
        print(f"✖ {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        from . import dlux_release_source

        source = staged or dlux_release_source
        try:
            candidate, _unpacked = source.obtain(args.version, channel=_resolved_channel(runtime))
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
            **({"source": staged} if staged else {}),
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


# --- `composer dlux channel` -------------------------------------------------
# Reporting and requesting only. Composer never writes the policy itself: the
# DjangoLux worker owns that file because it owns the database row it mirrors,
# and two writers of one policy is how a deployment ends up disagreeing with
# itself about which releases it may install.

def parse_dlux_channel_args(argv):
    parser = argparse.ArgumentParser(
        prog="composer dlux channel",
        description="Report or change the DjangoLux release channel for this deployment.",
    )
    parser.add_argument(
        "channel", nargs="?", default="", choices=["", "stable", "beta"],
        help="Channel to request. Omit to report the current one.",
    )
    parser.add_argument(
        "--runtime-root",
        default=os.environ.get("DLUX_UPDATE_RUNTIME_ROOT", DEFAULT_RUNTIME_ROOT),
        help="DjangoLux runtime volume root",
    )
    parser.add_argument("-f", "--file", help="Alternate compose file")
    parser.add_argument("-d", "--dev", action="store_true", help="Use compose.dev.yml")
    parser.add_argument("--no-delegate", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    args.action = "channel"
    args.version = ""
    args.check = False
    args.mode = "apply"
    args.status_file = None
    args.availability_file = None
    return args


def run_dlux_channel(args, argv=None) -> int:
    from . import dlux_channel

    runtime = DluxRuntime(args.runtime_root)
    if not runtime.exists():
        return _without_the_runtime_volume(args, argv)

    if args.channel:
        try:
            request = dlux_channel.request_channel(runtime.state_dir, args.channel)
        except dlux_channel.ChannelError as exc:
            print(f"✖ {exc}", file=sys.stderr)
            return 2
        except OSError as exc:
            print(f"✖ Could not write the channel request: {exc}", file=sys.stderr)
            return 2
        print(
            f"✔ Requested the {request['channel']} channel. The DjangoLux worker "
            "applies it on its next tick; re-run 'composer dlux channel' to confirm."
        )
        return 0

    status = dlux_channel.describe(runtime.state_dir)
    active = runtime.read_active() if runtime.exists() else {}
    installed = str(active.get("version") or "") if isinstance(active, dict) else ""
    print(f"Channel:   {status['channel']}")
    if installed:
        print(f"Installed: DjangoLux {installed}")
    if status["pending"]:
        print(f"Pending:   {status['pending']} (requested, not yet applied by the worker)")
    if status["failed"]:
        print(f"✖ Last change failed: {status['failed']}", file=sys.stderr)
        return 1
    if status["error"]:
        print(f"✖ {status['error']}", file=sys.stderr)
        return 1
    return 0
