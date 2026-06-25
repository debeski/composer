import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .subprocess_runner import SubprocessRunnerMixin

# Searched in order; first existing/complete match wins.
PLAINTEXT_ENV_CANDIDATES = (".env", "secrets/.env", ".secrets/.env")
ENCRYPTED_CANDIDATES = ("secrets.enc", "secrets/secrets.enc", ".secrets/secrets.enc")


class SecretsMixin(SubprocessRunnerMixin):
    enc_file: str
    loaded_secrets: List[str]

    def decrypt_secrets_raw(
        self,
        key: str = None,
        input_file: str = None,
        output_file: str = None,
    ) -> Tuple[bool, str]:
        in_path = input_file or self.enc_file
        cmd = ["sops", "-d", "--input-type", "dotenv", "--output-type", "dotenv"]
        if output_file:
            cmd.extend(["--output", output_file])
        cmd.append(in_path)

        env = os.environ.copy()
        if key:
            env["SOPS_AGE_KEY"] = key
        ok, out, err = self.run_command(cmd, timeout=10, env=env)
        if not ok:
            return False, err.strip() or "Decryption failed"
        return True, out

    def encrypt_secrets_raw(
        self,
        public_key: str = None,
        input_file: str = None,
        output_file: str = None,
    ) -> Tuple[bool, str]:
        in_path = input_file or ".secrets/.env"
        out_path = output_file or self.enc_file

        if public_key:
            cmd = [
                "sops",
                "-e",
                "-a",
                public_key,
                "--input-type",
                "dotenv",
                "--output",
                out_path,
                in_path,
            ]
            env = None
        else:
            return False, "Public key required for encryption"

        ok, out, err = self.run_command(cmd, timeout=10, env=env)
        if not ok:
            return False, err.strip() or "Encryption failed"
        return True, out

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

    def parse_dotenv_text(self, text: str) -> Dict[str, str]:
        values: Dict[str, str] = {}
        for line in text.splitlines():
            if "=" in line:
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

    def encrypted_secrets_path(self) -> Optional[Path]:
        for c in ENCRYPTED_CANDIDATES:
            p = Path(c)
            if p.exists():
                return p
        return None

    def resolve_secrets(self, args) -> Tuple[bool, str]:
        """Default secrets flow: prefer a plaintext env file that satisfies the
        compose's required variables; otherwise fall back to decrypting an
        encrypted secrets file (prompting for the AGE key when one was not
        supplied via ``-k``). Sets ``self.secrets_source`` on success."""
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
                self.secrets_source = ("plain", str(path))
                return True, ""
            if incomplete is None:
                incomplete = (path, missing)

        enc_path = self.encrypted_secrets_path()
        if enc_path is not None:
            self.enc_file = str(enc_path)
            key = (
                args.key
                or args.key_positional
                or os.environ.get("SOPS_AGE_KEY")
                or input("🔑 Paste AGE private key: ").strip()
            )
            ok, out = self.decrypt_secrets_raw(key=key, input_file=str(enc_path))
            if not ok:
                hint = ""
                if "no identity matched" in out:
                    hint = (
                        f"\n   Hint: the private key does not match the recipients "
                        f"in {enc_path}."
                    )
                return False, f"Failed to decrypt {enc_path}: {out}{hint}"
            self.apply_env_values(self.parse_dotenv_text(out))
            self.secrets_source = ("encrypted", str(enc_path))
            return True, ""

        if incomplete is not None:
            path, missing = incomplete
            shown = ", ".join(missing[:8]) + (" …" if len(missing) > 8 else "")
            return False, (
                f"{path} is missing variables required by the compose: {shown}\n"
                "   No encrypted fallback (secrets.enc) was found either."
            )
        return False, (
            "No secrets source found.\n"
            "   Looked for a plaintext env file (.env, secrets/.env, .secrets/.env)\n"
            "   and an encrypted file (secrets.enc, secrets/secrets.enc, .secrets/secrets.enc)."
        )
