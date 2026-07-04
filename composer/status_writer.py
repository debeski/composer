import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class StatusWriterMixin:
    """Writes a machine-readable deploy status file so another process (a
    Django admin panel, a dashboard, a health probe) can observe what the
    orchestrator is doing without parsing the terminal UI.

    Opt-in: no file is written unless ``--status-file`` or ``COMPOSER_STATUS_FILE``
    is set. Writes are atomic (temp file + replace) so a reader never sees a
    half-written document.
    """

    # Recognized lifecycle states (documented for readers).
    STATUS_STATES = (
        "starting",
        "pulling",
        "recreating",
        "restarting",
        "migrating",
        "ready",
        "failed",
    )

    def write_status(self, state: str, *, error: Optional[str] = None, extra: Optional[dict] = None):
        path = getattr(self, "status_file", None)
        if not path:
            return
        payload = {
            "status": state,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "composer_version": self.composer_version,
        }
        if self.active_compose_files:
            payload["compose_files"] = list(self.active_compose_files)
        target_images = getattr(self, "gate_images", None)
        if target_images:
            payload["target_images"] = list(target_images)
        target_version = getattr(self, "gate_target_version", None)
        if target_version:
            payload["target_version"] = target_version
        active_version = getattr(self, "gate_active_version", None)
        if active_version:
            payload["active_version"] = active_version
        if error:
            payload["error"] = str(error).strip()[:2000]
        if extra:
            payload.update(extra)

        try:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=".tmp",
                dir=str(target.parent),
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.write("\n")
            os.replace(tmp, target)
        except Exception:
            # Status reporting must never break a deployment.
            pass
