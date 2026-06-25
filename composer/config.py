import re
from pathlib import Path
from typing import Set

# ${VAR}, ${VAR:-default}, ${VAR?err}, $VAR ...
_VAR_REF_RE = re.compile(r"\$\{([A-Za-z_]\w*)((?::?[-+?])[^}]*)?\}|\$([A-Za-z_]\w*)")
# `$$` is the compose escape for a literal `$` (e.g. `$$VAR` is a shell
# variable, not an interpolation). Collapse escapes before scanning for refs.
_ESCAPED_DOLLAR_RE = re.compile(r"\$\$")
# Entries in an `environment:` block — mapping (`KEY: value`) or list
# (`- KEY=value` / `- KEY`) form. Group 2 is the assigned value (if any).
_ENV_MAP_ENTRY_RE = re.compile(r"^([A-Za-z_]\w*)\s*:\s*(.*)$")
_ENV_LIST_ENTRY_RE = re.compile(r"^-\s*([A-Za-z_]\w*)\s*(?:=\s*(.*))?$")


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
        """Names of environment variables the active compose files reference and
        that must be supplied externally.

        Excludes: interpolations carrying their own fallback (``${VAR:-x}``,
        ``${VAR-x}``, ``${VAR:+x}``, ``${VAR+x}``); ``$$``-escaped shell
        variables (a literal ``$`` in compose); and any variable the compose
        already defines itself in an ``environment:`` block.
        """
        required: Set[str] = set()
        defined: Set[str] = set()
        for file in self.active_compose_files:
            p = Path(file)
            if not p.exists():
                continue
            text = p.read_text()
            defined |= self._compose_env_keys(text)
            scannable = _ESCAPED_DOLLAR_RE.sub("", text)
            for braced, op, bare in _VAR_REF_RE.findall(scannable):
                name = braced or bare
                if not name:
                    continue
                if op:
                    sign = op[1] if op[0] == ":" else op[0]
                    if sign in "-+":  # has default / alternate value -> optional
                        continue
                required.add(name)
        return required - defined

    @classmethod
    def _compose_env_keys(cls, text: str) -> Set[str]:
        """Variable names that an ``environment:`` block assigns a concrete
        literal value to (so the compose itself supplies them). Bare
        pass-throughs (``- KEY``) and interpolated values (``KEY: ${KEY}``)
        are not counted — those still need a value from elsewhere.
        """
        keys: Set[str] = set()
        lines = text.splitlines()
        i, n = 0, len(lines)
        while i < n:
            header = re.match(r"(\s*)environment:\s*$", lines[i])
            if not header:
                i += 1
                continue
            base_indent = len(header.group(1))
            i += 1
            while i < n:
                line = lines[i]
                if not line.strip() or line.lstrip().startswith("#"):
                    i += 1
                    continue
                indent = len(line) - len(line.lstrip())
                if indent <= base_indent:
                    break
                entry = line.strip()
                m = _ENV_LIST_ENTRY_RE.match(entry) or _ENV_MAP_ENTRY_RE.match(entry)
                if m and cls._is_literal_value(m.group(2)):
                    keys.add(m.group(1))
                i += 1
        return keys

    @staticmethod
    def _is_literal_value(value) -> bool:
        """True if an environment value is a concrete literal (non-empty and
        free of variable interpolation, ignoring ``$$`` escapes)."""
        if not value:
            return False
        value = value.split(" #", 1)[0].strip()  # drop inline comment
        if not value:
            return False
        return "$" not in _ESCAPED_DOLLAR_RE.sub("", value)
