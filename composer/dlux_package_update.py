"""Health-gated inline DjangoLux update.

Ties the two halves together: fetch and verify a release
(`dlux_release_source`), stage and activate it on the runtime volume
(`dlux_runtime`), restart the services that run DjangoLux, and only then decide
whether the update stands.

The point of doing this from Composer rather than from inside DjangoLux is this
function's failure branch. When the new release does not come back healthy, the
process that rolls it back is *not* the process that just failed to start — it
is Composer, outside the container. An in-container updater cannot make that
guarantee, which is how a deployment ends up in a restart loop holding a release
it cannot undo.

`restart` and `health_check` are injected so this module stays testable and free
of Docker: the executor passes the real implementations
(`composer restart` and `HealthMonitorMixin.monitor_health`).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from . import dlux_release_source as release_source
from .dlux_runtime import DluxRuntime, DluxRuntimeError, version_sort_key

# Releases kept on the volume besides the active one, so a rollback target is
# always present without the volume growing without bound.
DEFAULT_KEEP_RELEASES = 3


@dataclass
class PackageUpdateResult:
    ok: bool
    version: str = ""
    previous_version: str = ""
    rolled_back: bool = False
    # Set when the rollback itself did not come back healthy. The deployment
    # needs an operator; nothing automatic can be trusted to fix it.
    critical: bool = False
    message: str = ""
    steps: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "version": self.version,
            "previous_version": self.previous_version,
            "rolled_back": self.rolled_back,
            "critical": self.critical,
            "message": self.message,
            "steps": list(self.steps),
        }


def _noop(_message: str) -> None:
    return None


def prune_releases(runtime: DluxRuntime, keep=DEFAULT_KEEP_RELEASES, protected=()) -> List[str]:
    """Drop the oldest staged releases, never touching a protected one."""
    keep = max(1, int(keep))
    safe = {str(v) for v in protected if v}
    try:
        active = runtime.read_active()
    except DluxRuntimeError:
        active = {}
    if active.get("version"):
        safe.add(active["version"])
    versions = [v for v in runtime.staged_versions() if v not in safe]
    removed = []
    for version in versions[: max(0, len(versions) - keep)]:
        target = runtime.release_path(version)
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            removed.append(version)
    return removed


def apply_package_update(
    runtime: DluxRuntime,
    *,
    restart: Callable[[], tuple],
    health_check: Callable[[], tuple],
    target_version: str = "",
    source=release_source,
    progress: Optional[Callable[[str], None]] = None,
    keep_releases: int = DEFAULT_KEEP_RELEASES,
    workdir=None,
) -> PackageUpdateResult:
    """Fetch, stage, activate, restart, health-check — and undo if unhealthy.

    `restart` and `health_check` each return ``(ok, detail)``.
    """
    say = progress or _noop
    steps: List[str] = []

    def step(name: str, message: str = "") -> None:
        steps.append(name)
        say(message or name)

    try:
        step("resolving", "Resolving the DjangoLux release")
        candidate, unpacked = source.obtain(target_version, workdir=workdir)
    except Exception as exc:
        return PackageUpdateResult(ok=False, message=str(exc), steps=steps)

    try:
        current = runtime.read_active()
    except DluxRuntimeError as exc:
        return PackageUpdateResult(ok=False, message=str(exc), steps=steps)

    if current.get("version") == candidate.version and current.get("source") == "volume":
        step("already-active")
        return PackageUpdateResult(
            ok=True, version=candidate.version, previous_version=candidate.version,
            message=f"DjangoLux {candidate.version} is already the active release.", steps=steps,
        )

    try:
        step("staging", f"Staging DjangoLux {candidate.version}")
        runtime.stage_release(candidate.version, unpacked, overwrite=True)
        # activate() verifies before it moves the pointer, so a bad artifact
        # never becomes the release the next container start would load.
        step("activating", f"Activating DjangoLux {candidate.version}")
        previous = runtime.activate(candidate.version)
    except DluxRuntimeError as exc:
        return PackageUpdateResult(ok=False, version=candidate.version,
                                   message=str(exc), steps=steps)

    previous_version = previous.get("version", "")

    step("restarting", "Restarting services on the new release")
    restarted, restart_detail = restart()
    healthy, health_detail = (False, restart_detail)
    if restarted:
        step("health-check", "Waiting for services to become healthy")
        healthy, health_detail = health_check()

    if healthy:
        step("pruning")
        prune_releases(runtime, keep_releases, protected=[previous_version])
        step("done")
        return PackageUpdateResult(
            ok=True, version=candidate.version, previous_version=previous_version,
            message=f"DjangoLux {candidate.version} is active.", steps=steps,
        )

    # -- the branch this whole design exists for ------------------------
    reason = health_detail or restart_detail or "The updated deployment did not become healthy."
    step("rolling-back", f"Rolling back to {previous_version or 'the image release'}")
    try:
        runtime.restore(previous)
        runtime.quarantine(candidate.version, reason=reason)
    except DluxRuntimeError as exc:
        return PackageUpdateResult(
            ok=False, version=candidate.version, previous_version=previous_version,
            rolled_back=False, critical=True,
            message=f"{reason} Rollback also failed: {exc}", steps=steps,
        )

    recovered, recover_detail = restart()
    if recovered:
        step("rollback-health-check")
        recovered, recover_detail = health_check()

    if not recovered:
        step("rollback-failed")
        return PackageUpdateResult(
            ok=False, version=candidate.version, previous_version=previous_version,
            rolled_back=True, critical=True,
            message=(f"{reason} The previous release was restored but did not become "
                     f"healthy either: {recover_detail}"),
            steps=steps,
        )

    step("rolled-back")
    return PackageUpdateResult(
        ok=False, version=candidate.version, previous_version=previous_version,
        rolled_back=True,
        message=f"{reason} Rolled back to {previous_version or 'the image release'}.",
        steps=steps,
    )


def rollback_package_update(
    runtime: DluxRuntime,
    *,
    restart: Callable[[], tuple],
    health_check: Callable[[], tuple],
    progress: Optional[Callable[[str], None]] = None,
) -> PackageUpdateResult:
    """Operator-requested rollback to the newest staged release below the active one."""
    say = progress or _noop
    steps: List[str] = []
    try:
        active = runtime.read_active()
    except DluxRuntimeError as exc:
        return PackageUpdateResult(ok=False, message=str(exc), steps=steps)

    current = active.get("version", "")
    # Strictly below the active release, which is what a rollback means. Taking
    # the newest staged release that merely differs would roll *forward* onto a
    # release the deployment had already stepped back from — the exact shape of
    # a rollback loop.
    targets = [
        version for version in runtime.staged_versions()
        if version_sort_key(version) < version_sort_key(current)
    ]
    if not targets:
        # Nothing below the active release: fall back to the image copy.
        steps.append("restore-image")
        say("Returning to the DjangoLux release baked into the image")
        runtime.restore({})
    else:
        target = targets[-1]
        steps.append("activating")
        say(f"Activating DjangoLux {target}")
        try:
            runtime.activate(target)
        except DluxRuntimeError as exc:
            return PackageUpdateResult(ok=False, message=str(exc), steps=steps)

    steps.append("restarting")
    ok, detail = restart()
    if ok:
        steps.append("health-check")
        ok, detail = health_check()
    if not ok:
        return PackageUpdateResult(ok=False, previous_version=current, critical=True,
                                   message=f"Rollback did not become healthy: {detail}",
                                   steps=steps)
    try:
        restored = runtime.read_active().get("version", "")
    except DluxRuntimeError:
        restored = ""
    steps.append("done")
    return PackageUpdateResult(ok=True, version=restored, previous_version=current,
                               rolled_back=True,
                               message=f"Rolled back to {restored or 'the image release'}.",
                               steps=steps)
