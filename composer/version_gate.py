import json
import re
from pathlib import Path
from typing import List, Optional, Tuple

from .service_selection import scoped_service_list

# Default image label carrying the release version. OCI-standard; override with
# COMPOSER_VERSION_LABEL for a project-specific label (e.g. a baked framework
# version).
DEFAULT_VERSION_LABEL = "org.opencontainers.image.version"
DEFAULT_ACTIVE_VERSION_KEY = "version"

_GO_NO_VALUE = "<no value>"

# A DjangoLux deployment can answer what an older target image actually does to
# the release it is running. Opt-in: without the command the gate is unchanged.
DEFAULT_RUNTIME_GATE_SERVICE = "web"
RUNTIME_GATE_COMMAND = ["python", "manage.py", "dlux_image_gate"]


class VersionGateMixin:
    """Preflight guard for deploy update flows (``-u``).

    When wired up (``COMPOSER_ACTIVE_VERSION_FILE`` set), it refuses to recreate
    onto a target image whose version label is OLDER than the deployment's
    currently-active version — the one dangerous move a generic pull-and-restart
    can't undo, because forward-only migrations may already have run against the
    newer schema. Bypass with ``--force``.

    The gate is fully opt-in and generic: with no active-version source
    configured it is disabled and always passes.
    """

    def gate_enabled(self) -> bool:
        return bool(getattr(self, "active_version_file", None))

    @staticmethod
    def parse_version(raw) -> Optional[Tuple[int, ...]]:
        """Lightweight PEP440/semver-ish parse into a comparable int tuple.
        Returns None when no numeric release component is present. Avoids a
        dependency on ``packaging`` (not installed in the composer image)."""
        text = str(raw or "").strip()
        # Take the leading release segment (digits and dots), before any
        # pre/post/dev suffix or build metadata.
        m = re.match(r"\s*v?(\d+(?:\.\d+)*)", text)
        if not m:
            return None
        return tuple(int(part) for part in m.group(1).split("."))

    @classmethod
    def _version_lt(cls, a: Tuple[int, ...], b: Tuple[int, ...]) -> bool:
        width = max(len(a), len(b))
        a = a + (0,) * (width - len(a))
        b = b + (0,) * (width - len(b))
        return a < b

    def read_active_version(self) -> Optional[str]:
        """Read the deployment's active version from the configured JSON file
        and key (e.g. the dlux runtime ``active.json`` → ``version``)."""
        path = getattr(self, "active_version_file", None)
        if not path:
            return None
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        key = getattr(self, "active_version_key", None) or DEFAULT_ACTIVE_VERSION_KEY
        value = payload
        for part in str(key).split("."):
            if not isinstance(value, dict) or part not in value:
                return None
            value = value[part]
        version = str(value).strip()
        return version or None

    def compose_config_images(self) -> List[str]:
        """Resolved image refs for the services in scope for this update."""
        args = ["config", "--images"]
        scoped = scoped_service_list(getattr(self, "pull_service", None))
        if scoped:
            args.extend(scoped)
        elif getattr(self, "exclude_services", None):
            if not getattr(self, "services", None):
                if not self.discover_services(silent=True):
                    return []
            services = getattr(self, "services", []) or []
            if not services:
                return []
            args.extend(services)
        ok, out, _ = self.run_docker_compose(args, timeout=15)
        if not ok:
            return []
        seen: List[str] = []
        for line in out.splitlines():
            ref = line.strip()
            if ref and ref not in seen:
                seen.append(ref)
        return seen

    def image_label_version(self, image: str) -> Optional[str]:
        label = getattr(self, "version_label", None) or DEFAULT_VERSION_LABEL
        ok, out, _ = self.run_command(
            [
                "docker", "image", "inspect", image,
                "--format", f'{{{{ index .Config.Labels "{label}" }}}}',
            ],
            timeout=15,
        )
        if not ok:
            return None
        value = out.strip()
        if not value or value == _GO_NO_VALUE:
            return None
        return value

    def runtime_release_verdict(self, target_label: str) -> Optional[dict]:
        """Ask the deployment what an older target image does to its active release.

        The gate blocks a downgrade because old code against a forward-migrated
        schema cannot be undone. A DjangoLux deployment can answer that
        precisely: its active release lives on the runtime volume and is what
        actually runs, so an image baking an older version does not by itself
        downgrade anything.

        Best-effort. Any failure — no such command, service down, unparseable
        output — returns None and the caller blocks exactly as it does today, so
        a deployment without the command is unaffected.
        """
        service = (
            getattr(self, "runtime_gate_service", None) or DEFAULT_RUNTIME_GATE_SERVICE
        )
        ok, out, _ = self.run_docker_compose(
            ["exec", "-T", service]
            + RUNTIME_GATE_COMMAND
            + ["--baked-dlux-version", str(target_label or "")],
            timeout=30,
        )
        if not ok:
            return None
        text = out or ""
        # The command prints JSON; compose may prepend its own noise.
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            verdict = json.loads(text[start : end + 1])
        except ValueError:
            return None
        return verdict if isinstance(verdict, dict) else None

    def preflight_version_gate(self) -> Tuple[bool, str]:
        """Returns (ok, message). ``ok`` False means block the recreate.

        Records ``gate_active_version``/``gate_target_version``/``gate_images``
        for the status file. Passes (with a possible note) whenever the gate is
        disabled, the metadata is missing, or ``--force`` is set.
        """
        if not self.gate_enabled():
            return True, ""

        active_raw = self.read_active_version()
        self.gate_active_version = active_raw
        active = self.parse_version(active_raw)

        images = self.compose_config_images()
        self.gate_images = images

        target_versions: List[Tuple[Tuple[int, ...], str, str]] = []
        for image in images:
            label = self.image_label_version(image)
            parsed = self.parse_version(label)
            if parsed is not None:
                target_versions.append((parsed, label, image))

        if not target_versions:
            self.gate_target_version = None
            return True, (
                "Version gate: no readable version label on the target image(s); "
                "proceeding without a version check."
            )

        # Report the lowest target version (the one that could regress).
        lowest = min(target_versions, key=lambda item: item[0])
        self.gate_target_version = lowest[1]

        if active is None:
            return True, (
                "Version gate: could not read an active version to compare against; "
                "proceeding without a version check."
            )

        if self._version_lt(lowest[0], active):
            if getattr(self, "force", False):
                return True, (
                    f"Version gate: {lowest[1]} < active {active_raw}, but --force was "
                    f"given — proceeding."
                )
            verdict = self.runtime_release_verdict(lowest[1]) or {}
            self.gate_runtime_verdict = verdict.get("verdict") or None
            detail = str(verdict.get("reason") or "").strip()
            if self.gate_runtime_verdict == "keep":
                # Not a downgrade: the deployment keeps running its own newer
                # release off the runtime volume, whatever the image bakes.
                return True, (
                    f"Version gate: target image '{lowest[2]}' bakes {lowest[1]}, older "
                    f"than the active {active_raw}, but the deployment keeps its active "
                    f"release — proceeding."
                    + (f"\n   {detail}" if detail else "")
                )
            message = (
                f"Version gate: target image '{lowest[2]}' is version {lowest[1]}, "
                f"OLDER than the active deployment version {active_raw}. Recreating "
                f"onto it risks running old code against a forward-migrated schema.\n"
                f"   Re-run with --force to override."
            )
            if detail:
                message = f"{message}\n   {detail}"
            return False, message

        return True, ""
