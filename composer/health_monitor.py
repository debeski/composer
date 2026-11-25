import os
import re
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .constants import SERVICE_FAILED, SERVICE_HEALTHY, SERVICE_NOT_SEEN, SERVICE_STARTING

HEALTH_TIMEOUT = 180
POLL_INTERVAL = 0.5
SETTLE_ENV = "COMPOSER_HEALTH_SETTLE_SECONDS"
DEFAULT_SETTLE_SECONDS = 10.0

_STARTED_AT = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)?$")


def settle_seconds() -> float:
    """How long each container must have been up, healthy and without a restart, to pass."""
    raw = os.environ.get(SETTLE_ENV, "").strip()
    try:
        value = float(raw) if raw else DEFAULT_SETTLE_SECONDS
    except ValueError:
        value = DEFAULT_SETTLE_SECONDS
    return max(0.0, value)


def started_epoch(text: str) -> Optional[float]:
    """Docker's ``State.StartedAt`` as epoch seconds, or None (unset or unparseable)."""
    match = _STARTED_AT.match(str(text or "").strip())
    if not match or match.group(1).startswith("0001-"):
        return None
    fraction = (match.group(2) or "")[:7]
    zone = match.group(3) or "Z"
    zone = "+00:00" if zone == "Z" else zone
    try:
        return datetime.fromisoformat(match.group(1) + fraction + zone).timestamp()
    except ValueError:
        return None


def restarted_services(
    baseline: Dict[str, Dict[str, Tuple[int, str]]],
    current: Dict[str, Dict[str, Tuple[int, str]]],
) -> Dict[str, int]:
    """Services whose containers restarted since ``baseline``, with a count.

    A restart shows as a higher RestartCount or a new StartedAt on the same
    container. A container first seen in ``current`` is measured from there.
    """
    found: Dict[str, int] = {}
    for service, containers in current.items():
        before = baseline.setdefault(service, {})
        for container_id, (count, started_at) in containers.items():
            if container_id not in before:
                before[container_id] = (count, started_at)
                continue
            first_count, first_started = before[container_id]
            restarts = max(0, count - first_count)
            if not restarts and started_at and first_started and started_at != first_started:
                restarts = 1
            if restarts:
                found[service] = found.get(service, 0) + restarts
    return found


class HealthMonitorMixin:
    """Wait for every service to become healthy, then require it to stay so.

    One healthy poll is not enough: a crash-looping container without a
    healthcheck reads as running between crashes, so the result used to depend
    on when the poll landed. A service passes once it is healthy and its newest
    container has been up for the settle window, measured from that container's
    own ``StartedAt``; a container already up that long adds no wait. Any
    container restart from the first poll on fails the check.
    """

    def monitor_health(self) -> Tuple[bool, str]:
        settle = settle_seconds()
        deadline = time.time() + HEALTH_TIMEOUT
        last_snapshot: Optional[Tuple[str, ...]] = None
        baseline: Optional[Dict[str, Dict[str, Tuple[int, str]]]] = None
        healthy_since: Optional[float] = None
        first_healthy: Dict[str, float] = {}

        while time.time() < deadline:
            if not self.update_service_states():
                return False, self.last_runtime_diagnostic or "Failed to inspect compose service state."

            markers = self.container_restart_markers()
            if baseline is None:
                baseline = {service: dict(containers) for service, containers in markers.items()}
            restarts = restarted_services(baseline, markers)
            for service in getattr(self, "restarting_services", ()) or ():
                restarts.setdefault(service, 1)
            if restarts:
                self.render()
                return False, self.unstable_detail(restarts, [])

            snapshot = tuple(self.service_state.get(s, SERVICE_NOT_SEEN) for s in self.services)
            if snapshot != last_snapshot:
                if healthy_since is None:
                    deadline = time.time() + HEALTH_TIMEOUT
                last_snapshot = snapshot
                self.render()

            failed = [s for s in self.services if self.service_state.get(s) == SERVICE_FAILED]
            starting = [s for s in self.services if self.service_state.get(s) == SERVICE_STARTING]
            all_healthy = all(self.service_state.get(s) == SERVICE_HEALTHY for s in self.services)

            if healthy_since is not None and not all_healthy:
                dropped = [s for s in self.services if self.service_state.get(s) != SERVICE_HEALTHY]
                return False, self.unstable_detail({}, dropped)

            if failed:
                self.emit_status("Health", f"Failing: {', '.join(failed)}")
            elif starting:
                self.emit_status("Health", f"Waiting: {', '.join(starting)}")

            now = time.time()
            for service in self.services:
                if self.service_state.get(service) == SERVICE_HEALTHY:
                    first_healthy.setdefault(service, now)
            if all_healthy:
                if healthy_since is None:
                    healthy_since = now
                    deadline = max(deadline, now + settle + HEALTH_TIMEOUT)
                young = [
                    service for service in self.services
                    if self.container_uptime(service, markers, first_healthy, now) < settle
                ]
                if not young:
                    self.emit_status("Health", "All services healthy")
                    return True, ""
                self.emit_status(
                    "Health",
                    f"All services healthy; watching {', '.join(young)} until up {settle:g}s",
                )

            time.sleep(POLL_INTERVAL)

        unhealthy = [
            f"{service} ({self.service_state.get(service, SERVICE_NOT_SEEN)})"
            for service in self.services
            if self.service_state.get(service) != SERVICE_HEALTHY
        ]
        details = ""
        if unhealthy:
            details = "Containers failed to become healthy: " + ", ".join(unhealthy)
        diagnostics = self.collect_service_diagnostics()
        if diagnostics:
            details = f"{details}\n\n{diagnostics}".strip()
        return False, details or "Containers failed to become healthy before the timeout."

    @staticmethod
    def container_uptime(
        service: str,
        markers: Dict[str, Dict[str, Tuple[int, str]]],
        first_healthy: Dict[str, float],
        now: float,
    ) -> float:
        """Seconds the service's newest container has been up.

        Taken from Docker's ``StartedAt``; when that cannot be read, from the
        first poll that found the service healthy.
        """
        starts = [
            started_epoch(started_at)
            for _count, started_at in (markers.get(service) or {}).values()
        ]
        known = [start for start in starts if start is not None]
        since = max(known) if known else None
        if since is None or since > now:
            # Unreadable, or a daemon clock ahead of ours: measure from here.
            since = first_healthy.get(service, now)
        return now - since

    def unstable_detail(self, restarts: Dict[str, int], dropped: List[str]) -> str:
        lines: List[str] = []
        for service in sorted(restarts):
            count = restarts[service]
            times = "once" if count == 1 else f"{count} times"
            lines.append(
                f"{service} restarted {times} during the health check: it is crash-looping."
            )
        for service in dropped:
            state = self.service_state.get(service, SERVICE_NOT_SEEN)
            lines.append(
                f"{service} was healthy, then went to {state} before the "
                f"{settle_seconds():g}s settle window ended."
            )
        names = sorted(set(restarts) | set(dropped))
        diagnostics = self.collect_service_diagnostics(log_services=names)
        detail = "Services did not stay healthy:\n" + "\n".join(f"- {line}" for line in lines)
        return f"{detail}\n\n{diagnostics}".strip() if diagnostics else detail
