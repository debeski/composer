import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .subprocess_runner import SubprocessRunnerMixin

# Searched in order; first existing/complete match wins.
PLAINTEXT_ENV_CANDIDATES = (".env", "secrets/.env", ".secrets/.env")


class SecretsMixin(SubprocessRunnerMixin):
    loaded_secrets: List[str]

    # --- env value helpers -------------------------------------------------

    def parse_env_file(self, path) -> Dict[str, str]:
        """Read a dotenv file into a dict (comments/blank lines ignored)."""
        values: Dict[str, str] = {}
        try:
            content = Path(path).read_text(encoding="utf-8")
        except Exception:
            return values
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip().strip("'\"")
        return values

    def apply_env_values(self, values: Dict[str, str]):
        for k, v in values.items():
            os.environ[k] = v
            self.loaded_secrets.append(k)

    # --- source discovery / resolution -------------------------------------

    def plaintext_env_candidates(self) -> List[Path]:
        return [Path(c) for c in PLAINTEXT_ENV_CANDIDATES if Path(c).exists()]

    def resolve_secrets(self) -> Tuple[bool, str]:
        """Resolve secrets from a plaintext env file: use the first candidate
        (``.env`` → ``secrets/.env`` → ``.secrets/.env``) that satisfies every
        variable required by the compose. Sets ``self.secrets_source`` (the
        resolved path) on success."""
        required = self.required_compose_vars()
        injected = {"COMPOSER_VERSION"}
        if self.dev_mode:
            injected.add("NGINX_PORT")
            injected.add("DEBUG")
            injected.add("DEBUG_STATUS")

        incomplete: Optional[Tuple[Path, List[str]]] = None
        for path in self.plaintext_env_candidates():
            values = self.parse_env_file(path)
            satisfied = set(values) | set(os.environ) | injected
            missing = sorted(v for v in required if v not in satisfied)
            if not missing:
                self.apply_env_values(values)
                self.secrets_source = str(path)
                return True, ""
            if incomplete is None:
                incomplete = (path, missing)

        if incomplete is not None:
            path, missing = incomplete
            shown = ", ".join(missing[:8]) + (" …" if len(missing) > 8 else "")
            return False, (
                f"{path} is missing variables required by the compose: {shown}"
            )
        return False, (
            "No secrets source found.\n"
            "   Looked for a plaintext env file (.env, secrets/.env, .secrets/.env)."
        )
