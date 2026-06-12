import re
from pathlib import Path


class ConfigMixin:
    def extract_config(self):
        for file in self.active_compose_files:
            p = Path(file)
            if not p.exists():
                continue

            text = p.read_text()
            if m := re.search(r"BASE_URL:\s*(.+)", text):
                self.app_url = m.group(1).strip(" '\"")
            if m := re.search(r"DEBUG_STATUS:\s*['\"]?(true|false)['\"]?", text, re.I):
                self.debug_mode = m.group(1).lower() == "true"
