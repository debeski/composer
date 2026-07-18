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
        """Read a dotenv file into a dict (comments/blank lines ignored).

        Raises ``OSError``/``ValueError`` if the file exists but cannot be read
        or decoded (permissions, Docker userns-remap, bad encoding). Callers
        must not treat an unreadable secrets file as an empty one — doing so
        lets a deploy fall through to the compose's ``${VAR:-default}``
        fallbacks (e.g. default DB credentials).
        """
        values: Dict[str, str] = {}
        content = Path(path).read_text(encoding="utf-8")
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
        (``.env`` → ``secrets/.env`` → ``.secrets/.env``) that both satisfies
        every variable required by the compose and yields at least one value.
        Sets ``self.secrets_source`` (the resolved path) on success.

        A candidate that exists but cannot be read, or that yields no values, is
        never silently accepted. ``required_compose_vars()`` excludes every
        ``${VAR:-default}`` interpolation, so a compose that defaults all of its
        secrets requires almost nothing — accepting an unreadable/empty file
        would then hand the deploy to those defaults (e.g. ``admin``/
        ``admin_pass``). Such a candidate is reported instead of loaded."""
        required = self.required_compose_vars()
        injected = {"COMPOSER_VERSION"}
        if self.dev_mode:
            injected.add("NGINX_PORT")
            injected.add("DEBUG")
            injected.add("DEBUG_STATUS")

        incomplete: Optional[Tuple[Path, List[str]]] = None
        unreadable: Optional[Tuple[Path, str]] = None
        for path in self.plaintext_env_candidates():
            try:
                values = self.parse_env_file(path)
            except (OSError, ValueError) as exc:
                if unreadable is None:
                    unreadable = (path, getattr(exc, "strerror", None) or str(exc))
                continue
            satisfied = set(values) | set(os.environ) | injected
            missing = sorted(v for v in required if v not in satisfied)
            if not missing:
                if not values:
                    # Readable but empty: accepting it would deploy on compose
                    # defaults. Record as incomplete, keep looking.
                    if incomplete is None:
                        incomplete = (path, ["(no values found in file)"])
                    continue
                self.apply_env_values(values)
                self.secrets_source = str(path)
                return True, ""
            if incomplete is None:
                incomplete = (path, missing)

        if unreadable is not None:
            path, reason = unreadable
            return False, (
                f"{path} exists but could not be read ({reason}).\n"
                "   Check its permissions/ownership — a root-only secrets file "
                "can be unreadable inside the updater container under Docker "
                "userns-remap. Refusing to deploy on compose defaults."
            )
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
