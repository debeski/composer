import time
from typing import Optional, Tuple

from .constants import SERVICE_FAILED, SERVICE_HEALTHY, SERVICE_NOT_SEEN, SERVICE_STARTING


class HealthMonitorMixin:
    def monitor_health(self) -> Tuple[bool, str]:
        timeout = 180
        deadline = time.time() + timeout
        last_snapshot: Optional[Tuple[str, ...]] = None

        while time.time() < deadline:
            if not self.update_service_states():
                return False, self.last_runtime_diagnostic or "Failed to inspect compose service state."

            snapshot = tuple(self.service_state.get(s, SERVICE_NOT_SEEN) for s in self.services)
            if snapshot != last_snapshot:
                deadline = time.time() + timeout
                last_snapshot = snapshot
                self.render()

            failed = [s for s in self.services if self.service_state.get(s) == SERVICE_FAILED]
            starting = [s for s in self.services if self.service_state.get(s) == SERVICE_STARTING]

            if failed:
                self.emit_status("Health", f"Failing: {', '.join(failed)}")
            elif starting:
                self.emit_status("Health", f"Waiting: {', '.join(starting)}")

            if all(self.service_state.get(s) == SERVICE_HEALTHY for s in self.services):
                self.emit_status("Health", "All services healthy")
                return True, ""

            time.sleep(0.5)

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
