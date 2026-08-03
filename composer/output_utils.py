import json
from typing import Dict, List, Optional

from .agent_protocol import redact_text

from .constants import ANSI_ESCAPE_RE, ERROR_KEYWORDS, PROGRESS_KEYWORDS
from .session import terminal_detached


class OutputUtilsMixin:
    def sanitize_output(self, text: str) -> str:
        return ANSI_ESCAPE_RE.sub("", text or "").replace("\r", "\n")

    def summarize_output(self, *texts: str, max_lines: int = 10) -> str:
        lines: List[str] = []
        seen = set()

        for text in texts:
            for raw_line in self.sanitize_output(text).splitlines():
                line = raw_line.strip()
                if not line or line in seen:
                    continue
                seen.add(line)
                lines.append(line)

        if not lines:
            return ""

        matched = [
            line
            for line in lines
            if any(keyword in line.lower() for keyword in ERROR_KEYWORDS)
        ]
        selected = matched[-max_lines:] if matched else lines[-max_lines:]
        return "\n".join(selected)

    def build_failure_detail(self, stdout: str = "", stderr: str = "", diagnostics: str = "") -> str:
        details: List[str] = []
        command_summary = self.summarize_output(stderr, stdout)
        if command_summary:
            details.append(command_summary)
        diagnostics = diagnostics.strip()
        if diagnostics:
            details.append(diagnostics)
        if not details:
            details.append("Docker Compose did not return a detailed error.")
        return "\n\n".join(details)

    def parse_compose_json_output(self, text: str) -> List[Dict[str, str]]:
        payload = self.sanitize_output(text).strip()
        if not payload:
            return []

        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            parsed = None

        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
        if isinstance(parsed, dict):
            return [parsed]

        items: List[Dict[str, str]] = []
        for line in payload.splitlines():
            line = line.strip().rstrip(",")
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                items.append(item)
        return items

    def extract_progress_message(self, raw_line: str) -> Optional[str]:
        line = self.sanitize_output(raw_line).strip()
        if not line:
            return None

        lower = line.lower()
        if line.startswith("#") or line.startswith("[+]") or "=>" in line:
            return line
        if any(keyword in lower for keyword in PROGRESS_KEYWORDS):
            return line
        return None

    def append_console(self, text: str):
        """Append a clean, ANSI-free line to COMPOSER_LOG_FILE (if set), so a
        resident watcher / proxy can serve a live console for the update. The
        terminal panel is unaffected; this is a separate, append-only record."""
        path = getattr(self, "log_file", None)
        if not path:
            return
        try:
            with open(path, "a", encoding="utf-8") as handle:
                for line in str(text).splitlines() or [""]:
                    handle.write(redact_text(line.rstrip()) + "\n")
        except OSError:
            pass

    def emit_progress(self, label: str, raw_line: str):
        message = self.extract_progress_message(raw_line)
        if not message or message == self.last_progress_text:
            return
        self.last_progress_text = message
        self.last_progress_label = label
        self.append_console(f"[{label}] {message}")
        self.print_progress_line(label, message)

    def emit_status(self, label: str, message: str):
        if message == self.last_progress_text:
            return
        self.last_progress_text = message
        self.last_progress_label = label
        self.append_console(f"[{label}] {message}")
        self.print_progress_line(label, message)

    def print_progress_line(self, label: str, message: str):
        if terminal_detached():
            print(f"   [{label}] {message}", flush=True)
            return
        print(f"\r\033[2K   [{label}] {message}", end="", flush=True)

    def finish_progress_line(self):
        """Close the in-place status line so later output starts on its own row."""
        if self.last_progress_text and not terminal_detached():
            print("", flush=True)
        self.last_progress_text = ""
        self.last_progress_label = ""

    def emit_pull_progress(self, label: str, raw_line: str):
        """Draw the aggregated pull bar; anything that is not pull output keeps
        the plain status line. A pull is the one step long enough to look like a
        hang, so it gets a bar instead of the last line Docker happened to say."""
        if not self.pull_progress.feed(raw_line):
            self.emit_progress(label, raw_line)
            return

        summary = self.pull_progress.summary()
        if summary == self.last_progress_text:
            return
        self.last_progress_text = summary
        self.last_progress_label = label
        self.append_console(f"[{label}] {summary}")
        if terminal_detached():
            # No terminal to repaint: the coarse summary keeps the log readable.
            print(f"   [{label}] {summary}", flush=True)
            return
        print(f"\r\033[2K   [{label}] {self.pull_progress.bar()}", end="", flush=True)
