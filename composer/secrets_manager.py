import os
from pathlib import Path
from typing import List, Tuple

from .subprocess_runner import SubprocessRunnerMixin


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

    def load_secrets(self, key: str) -> bool:
        ok, out = self.decrypt_secrets_raw(key)
        if not ok:
            return False

        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                os.environ[k] = v.strip("'\"")
                self.loaded_secrets.append(k)
        return True

    def load_secrets_from_file(self) -> bool:
        env_path = Path(".secrets/.env")
        if not env_path.exists():
            return False

        try:
            content = env_path.read_text(encoding="utf-8")
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue

                k, v = line.split("=", 1)
                os.environ[k] = v.strip("'\"")
                self.loaded_secrets.append(k)
            return True
        except Exception:
            return False
