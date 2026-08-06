from typing import List

from .constants import (
    ANSI_ESCAPE_RE,
    ERROR,
    IDLE,
    OK,
    RUNNING,
    SERVICE_FAILED,
    SERVICE_HEALTHY,
    SERVICE_NOT_SEEN,
    SERVICE_STARTING,
    SERVICE_UPDATING,
)
from .service_selection import scoped_service_list
from .session import terminal_detached


RULE = "━" * 49


class RenderingMixin:
    def render(self, error_message: str = None):
        lines: List[str] = [
            "",
            " \033[1m🛡️  COMPOSER\033[0m · Orchestrator for Docker Compose",
            RULE,
        ]
        active_flags: List[str] = []
        if self.dev_mode:
            active_flags.append("\033[91m🛠️  DEV MODE\033[0m")
        if self.debug_mode:
            active_flags.append("\033[93m🪲  DEBUG MODE\033[0m")
        if self.secrets_source:
            active_flags.append(f"\033[93m🔓 PLAINTEXT {self.secrets_source}\033[0m")
        if self.no_migrate:
            active_flags.append("\033[93m⏭️  SKIP MIGRATIONS (STATIC STILL COLLECTED)\033[0m")
        if self.force_makemigrations:
            active_flags.append("\033[93m🔄 FORCE MIGRATIONS\033[0m")
        if self.target_app:
            active_flags.append(f"🎯  APP: {self.target_app}")
        if self.build_images:
            active_flags.append("\033[96m🏗️  FORCE BUILD\033[0m")
        if active_flags:
            lines.append(" " + "  •  ".join(active_flags))
        if self.active_compose_files:
            lines.append(f" 📂 {', '.join(self.active_compose_files)}")
        lines.extend(
            [
                f" 🌐 {self.app_url}",
                RULE,
                "",
            ]
        )

        def icon(state):
            return {
                IDLE: "⠿",
                RUNNING: "⟳",
                OK: "✔",
                ERROR: "✖",
            }[state]

        secrets_label = "Load Secrets"
        if self.secrets_source:
            secrets_label += f"  ·  {self.secrets_source}"
        lines.append(f" {icon(self.sections['secrets'])} {secrets_label}")
        if self.update_images:
            pull_label = "Pull Images"
            pull_scope = scoped_service_list(self.pull_service)
            if pull_scope:
                pull_label += f" ({', '.join(pull_scope)})"
            lines.append(f" {icon(self.sections['pull'])} {pull_label}")
        if not getattr(self, "pull_only_mode", False):
            if self.restart_mode:
                compose_label = "Restart Services"
                if isinstance(self.restart_service, str):
                    compose_label += f" ({self.restart_service})"
            else:
                compose_label = "Start Compose"
                up_scope = scoped_service_list(self.up_service)
                if up_scope:
                    compose_label += f" ({', '.join(up_scope)})"
            lines.append(f" {icon(self.sections['compose'])} {compose_label}")
            lines.append(f" {icon(self.sections['health'])} Health Check")
            if not self.restart_mode:
                lines.append(f" {icon(self.sections['post_start'])} Post-Start Tasks")
        lines.append("")
        lines.append(
            "   " + " ".join(self.service_icon(s) for s in self.services)
            if self.services
            else ""
        )

        if error_message:
            lines.append("")
            lines.append("\033[91m✖ ERROR:\033[0m")
            for line in str(error_message).splitlines():
                lines.append(f"  {line}")
        else:
            if self.last_progress_text:
                lines.append(f"   [{self.last_progress_label}] {self.last_progress_text}")
            else:
                lines.append("")

        if terminal_detached():
            # No terminal to repaint: append a plain frame to the log instead.
            self.last_render_line_count = 0
            for line in lines:
                print(ANSI_ESCAPE_RE.sub("", line))
            print("", end="", flush=True)
            return

        total_lines = max(self.last_render_line_count, len(lines))

        if self.last_render_line_count > 1:
            print(f"\r\033[{self.last_render_line_count - 1}F", end="")
        elif self.last_render_line_count == 1:
            print("\r", end="")

        for index in range(total_lines):
            line = lines[index] if index < len(lines) else ""
            end = "\n" if index < total_lines - 1 else ""
            print(f"\033[2K{line}", end=end)

        self.last_render_line_count = len(lines)
        print("", end="", flush=True)

    def service_icon(self, svc: str) -> str:
        return {
            SERVICE_NOT_SEEN: "⚪",
            SERVICE_UPDATING: "🔵",
            SERVICE_STARTING: "🟡",
            SERVICE_HEALTHY: "🟢",
            SERVICE_FAILED: "🔴",
        }[self.service_state.get(svc, SERVICE_NOT_SEEN)]
