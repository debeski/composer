import re
import shlex
import sys
from pathlib import Path
from typing import List, Tuple

from .constants import (
    DEFAULT_MIGRATOR_COMMAND,
    DEFAULT_MIGRATOR_SERVICE,
    POST_START_LABEL,
    SERVICE_HEALTHY,
)


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
        self._post_start_compatibility_fallback = False
        config = self.compose_config_json()
        commands = self.parse_post_start_labels(config)
        if commands:
            return commands, False
        legacy = self.parse_legacy_post_start_blocks()
        if legacy:
            return legacy, True

        # DjangoLux's existing-project updater path historically upgraded the
        # dlux-updater service but did not add the new web label. Recognize that
        # topology narrowly so established DLUX projects do not silently skip
        # migrations/static collection until `composer check --fix` repairs it.
        services = config.get("services") if isinstance(config, dict) else None
        if (
            isinstance(services, dict)
            and "dlux-updater" in services
            and DEFAULT_MIGRATOR_SERVICE in services
        ):
            self._post_start_compatibility_fallback = True
            return [(
                DEFAULT_MIGRATOR_SERVICE,
                self.default_migrator_command(config),
            )], False
        return [], False

    def parse_post_start_labels(self, config=None) -> List[Tuple[str, str]]:
        if config is None:
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

    def default_migrator_command(self, config=None) -> str:
        """DLUX migrator command matching an existing updater's supervisor."""
        if config is None:
            config = self.compose_config_json()
        services = config.get("services") if isinstance(config, dict) else None
        updater = services.get("dlux-updater") if isinstance(services, dict) else None
        command = updater.get("command") if isinstance(updater, dict) else None
        if isinstance(command, str):
            try:
                argv = shlex.split(command, posix=sys.platform != "win32")
            except ValueError:
                argv = []
        elif isinstance(command, list) and all(isinstance(item, str) for item in command):
            argv = list(command)
        else:
            argv = []
        if "--" in argv:
            boundary = argv.index("--")
            prefix = argv[: boundary + 1]
            if any("supervisor" in token for token in prefix):
                return " ".join(prefix + ["python", "manage.py", "migrator"])
        return DEFAULT_MIGRATOR_COMMAND

    def migrator_command_for_service(self, service: str) -> List[str]:
        """Configured migrator argv for a service, or the DLUX default."""
        config = self.compose_config_json()
        for configured_service, command in self.parse_post_start_labels(config):
            if configured_service != service:
                continue
            try:
                argv = shlex.split(command, posix=sys.platform != "win32")
            except ValueError:
                break
            if "migrator" in argv:
                return argv
        return shlex.split(
            self.default_migrator_command(config),
            posix=sys.platform != "win32",
        )

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
        elif commands and getattr(self, "_post_start_compatibility_fallback", False):
            self.emit_status(
                "Note",
                "missing org.dlux.post-start label — using DLUX compatibility migrator; run `composer check --fix`",
            )

        for service, cmd in commands:
            if self.service_state.get(service) != SERVICE_HEALTHY:
                return False, f"{service}: post-start task cannot run because the service is not healthy"

            try:
                argv = shlex.split(cmd, posix=sys.platform != "win32")
            except ValueError as e:
                return False, f"{service}: could not parse post_start command `{cmd}`\n{e}"

            if "migrator" in argv:
                argv += self.migrator_flags()

            display = " ".join(argv)
            self.emit_status("Exec", f"{service}: {display}")
            ok, out, err = self.run_docker_compose_streaming(
                ["exec", "-T", service] + argv,
                progress_callback=lambda line: self.emit_status("Post-start", line),
            )
            self.finish_progress_line()
            if not ok:
                detail = self.build_failure_detail(out, err)
                return False, f"{service}: post_start command failed\nCommand: {display}\n\n{detail}"
        return True, ""
