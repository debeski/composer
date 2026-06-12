import json
from typing import Dict, List, Optional

from .constants import ANSI_ESCAPE_RE, ERROR_KEYWORDS, PROGRESS_KEYWORDS


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

    def emit_progress(self, label: str, raw_line: str):
        message = self.extract_progress_message(raw_line)
        if not message or message == self.last_progress_text:
            return
        self.last_progress_text = message
        self.last_progress_label = label
        print(f"\r\033[2K   [{label}] {message}", end="", flush=True)

    def emit_status(self, label: str, message: str):
        if message == self.last_progress_text:
            return
        self.last_progress_text = message
        self.last_progress_label = label
        print(f"\r\033[2K   [{label}] {message}", end="", flush=True)
