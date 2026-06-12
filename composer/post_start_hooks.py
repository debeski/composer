import re
import shlex
import sys
from pathlib import Path
from typing import List, Tuple

from .constants import SERVICE_HEALTHY


class PostStartHooksMixin:
    def parse_post_start_commands(self) -> List[Tuple[str, str]]:
        """
        Parse compose.yml to find post_start commands.
        Returns a list of (service_name, command) tuples.
        """
        commands = []

        files = self.active_compose_files
        for file in files:
            p = Path(file)
            if not p.exists():
                continue

            text = p.read_text()
            lines = text.splitlines()
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
                        commands.append((current_service, cmd))

        return commands

    def run_post_start_hooks(self) -> Tuple[bool, str]:
        if self.no_migrate:
            self.emit_status("Skip", "Post-start tasks (Bypass requested)")
            return True, ""

        commands = self.parse_post_start_commands()

        for service, cmd in commands:
            if self.service_state.get(service) != SERVICE_HEALTHY:
                self.emit_status("Skip", f"unhealthy service: {service}")
                continue

            if "manage.py migrator" in cmd:
                if self.target_app:
                    cmd += f" -a {self.target_app}"
                if self.force_makemigrations:
                    cmd += " -mm"

            self.emit_status("Exec", f"{service}: {cmd}")
            try:
                exec_args = ["exec", service] + shlex.split(cmd, posix=sys.platform != "win32")
            except ValueError as e:
                return False, f"{service}: could not parse post_start command `{cmd}`\n{e}"

            ok, out, err = self.run_docker_compose(exec_args)
            if not ok:
                detail = self.build_failure_detail(out, err)
                return False, f"{service}: post_start command failed\nCommand: {cmd}\n\n{detail}"
        return True, ""
