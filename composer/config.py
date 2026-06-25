import re
from pathlib import Path
from typing import Set

# ${VAR}, ${VAR:-default}, ${VAR?err}, $VAR ...
_VAR_REF_RE = re.compile(r"\$\{([A-Za-z_]\w*)((?::?[-+?])[^}]*)?\}|\$([A-Za-z_]\w*)")


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

    def required_compose_vars(self) -> Set[str]:
        """Names of environment variables referenced by the active compose
        files that must be supplied (i.e. interpolations without a default
        or alternate value). Variables written as ``${VAR:-x}``, ``${VAR-x}``,
        ``${VAR:+x}`` or ``${VAR+x}`` carry their own fallback and are skipped.
        """
        required: Set[str] = set()
        for file in self.active_compose_files:
            p = Path(file)
            if not p.exists():
                continue
            for braced, op, bare in _VAR_REF_RE.findall(p.read_text()):
                name = braced or bare
                if not name:
                    continue
                if op:
                    sign = op[1] if op[0] == ":" else op[0]
                    if sign in "-+":  # has default / alternate value -> optional
                        continue
                required.add(name)
        return required
