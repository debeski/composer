import re
import shlex
import sys
from pathlib import Path
from typing import List, Tuple

from .constants import POST_START_LABEL, SERVICE_HEALTHY


class PostStartHooksMixin:
    def parse_post_start_commands(self) -> Tuple[List[Tuple[str, str]], bool]:
        """Post-start commands to run, and whether they came from a legacy source.

        The canonical declaration is the ``org.dlux.post-start`` service label,
        read from the resolved compose config so overrides merge exactly once.
        Compose does not act on that label, which leaves composer the only runner.

        Deployments generated before the label still declare a native Compose
        ``post_start`` hook. Compose runs those itself, so composer running them
        too means two copies; they are returned with legacy=True so the caller
        can say so, because dropping them would break ``-mm`` on those projects.
        """
        commands = self.parse_post_start_labels()
        if commands:
            return commands, False
        return self.parse_legacy_post_start_blocks(), True

    def parse_post_start_labels(self) -> List[Tuple[str, str]]:
        config = self.compose_config_json()
        if not config:
            return []

        commands = []
        services = config.get("services")
        if not isinstance(services, dict):
            return []
        for name, spec in services.items():
            if not isinstance(spec, dict):
                continue
            labels = spec.get("labels")
            # Compose normalizes to a mapping, but a hand-written list form
            # ("key=value") survives in projects that were edited by hand.
            if isinstance(labels, list):
                labels = dict(
                    item.split("=", 1) for item in labels if isinstance(item, str) and "=" in item
                )
            if not isinstance(labels, dict):
                continue
            command = labels.get(POST_START_LABEL)
            if isinstance(command, str) and command.strip():
                commands.append((name, command.strip()))
        return commands

    def parse_legacy_post_start_blocks(self) -> List[Tuple[str, str]]:
        """Scrape native ``post_start:`` blocks out of the raw compose files."""
        commands = []

        seen = set()
        for file in self.active_compose_files:
            p = Path(file)
            if not p.exists():
                continue

            lines = p.read_text().splitlines()
            current_service = None
            in_post_start = False

            for line in lines:
                m_svc = re.match(r"^  ([a-zA-Z0-9_-]+):", line)
                if m_svc:
                    current_service = m_svc.group(1)
                    in_post_start = False
                    continue

                if not current_service:
                    continue

                if "post_start:" in line:
                    in_post_start = True
                    continue

                if in_post_start:
                    if re.match(r"^\S", line) or re.match(r"^  \S", line):
                        in_post_start = False
                        continue

                    m_cmd = re.search(r"-\s+command:\s+(.+)$", line)
                    if m_cmd:
                        cmd = m_cmd.group(1).strip()
                        # An override file repeating the block would otherwise
                        # queue the same command twice under `-d`.
                        if (current_service, cmd) in seen:
                            continue
                        seen.add((current_service, cmd))
                        commands.append((current_service, cmd))

        return commands

    def migrator_flags(self) -> List[str]:
        """Flags appended to a migrator invocation.

        The contract the migrator implements: `-mm` forces makemigrations for
        every app then migrates, `-nm` skips makemigrations and migrate while
        still collecting static, and bare means makemigrations only for apps
        with no initial migration. They are mutually exclusive.
        """
        flags = []
        if self.target_app:
            flags += ["-a", str(self.target_app)]
        if self.force_makemigrations:
            flags.append("-mm")
        elif self.no_migrate:
            flags.append("-nm")
        return flags

    def run_post_start_hooks(self) -> Tuple[bool, str]:
        if self.skip_post_start:
            self.emit_status("Skip", "Post-start tasks (Bypass requested)")
            return True, ""

        commands, legacy = self.parse_post_start_commands()
        if commands and legacy:
            self.emit_status(
                "Note",
                "legacy post_start hook — Compose also runs it; `composer check --fix` migrates it",
            )

        for service, cmd in commands:
            if self.service_state.get(service) != SERVICE_HEALTHY:
                self.emit_status("Skip", f"unhealthy service: {service}")
                continue

            try:
                argv = shlex.split(cmd, posix=sys.platform != "win32")
            except ValueError as e:
                return False, f"{service}: could not parse post_start command `{cmd}`\n{e}"

            if "migrator" in argv:
                argv += self.migrator_flags()

            display = " ".join(argv)
            self.emit_status("Exec", f"{service}: {display}")
            ok, out, err = self.run_docker_compose(["exec", service] + argv)
            if not ok:
                detail = self.build_failure_detail(out, err)
                return False, f"{service}: post_start command failed\nCommand: {display}\n\n{detail}"
        return True, ""
