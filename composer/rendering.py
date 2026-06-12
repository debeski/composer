from typing import List

from .constants import (
    ERROR,
    IDLE,
    OK,
    RUNNING,
    SERVICE_FAILED,
    SERVICE_HEALTHY,
    SERVICE_NOT_SEEN,
    SERVICE_STARTING,
)


class RenderingMixin:
    def render(self, error_message: str = None):
        lines: List[str] = [
            "",
            "🛡️  COMPOSER - Orchestrator for Docker Compose",
            "█████████████████████████████████████████████████",
        ]
        active_flags: List[str] = []
        if self.dev_mode:
            active_flags.append("\033[91m🛠️  DEV MODE\033[0m")
        if self.debug_mode:
            active_flags.append("\033[93m🪲  DEBUG MODE\033[0m")
        if self.skip_decrypt and not self.dev_mode:
            active_flags.append("\033[93m⚠️  BYPASS DECRYPTION\033[0m")
        if self.no_migrate:
            active_flags.append("\033[93m⏭️  SKIP MIGRATIONS\033[0m")
        if self.force_makemigrations:
            active_flags.append("\033[93m🔄 FORCE MIGRATIONS\033[0m")
        if self.active_compose_files:
            files_str = ", ".join(self.active_compose_files)
            active_flags.append(f"📂  COMPOSE: {files_str}")
        if self.target_app:
            active_flags.append("🎯  TARGET APP")
        if self.build_images:
            active_flags.append("\033[96m🏗️  FORCE BUILD\033[0m")
        if active_flags:
            lines.append("  •  ".join(active_flags))
        lines.extend(
            [
                f"🌐 BASE URL: {self.app_url}",
                "█████████████████████████████████████████████████",
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

        lines.append(f"{icon(self.sections['secrets'])} Decrypt Secrets")
        if self.update_images:
            pull_label = "Pull Images"
            if isinstance(self.pull_service, str):
                pull_label += f" ({self.pull_service})"
            lines.append(f"{icon(self.sections['pull'])} {pull_label}")
        lines.extend(
            [
                f"{icon(self.sections['compose'])} Start Compose",
                f"{icon(self.sections['health'])} Health Check",
                f"{icon(self.sections['post_start'])} Post-Start Tasks",
                "",
                "   " + " ".join(self.service_icon(s) for s in self.services)
                if self.services
                else "",
            ]
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
            SERVICE_STARTING: "🟡",
            SERVICE_HEALTHY: "🟢",
            SERVICE_FAILED: "🔴",
        }[self.service_state.get(svc, SERVICE_NOT_SEEN)]
