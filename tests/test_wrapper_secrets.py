import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipIf(os.name == "nt", "Bash wrapper test")
class WrapperSecretsTests(unittest.TestCase):
    def test_start_wrapper_passes_env_file_and_key_marker_without_values(self):
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            fake_bin = root / "bin"
            project.joinpath(".secrets").mkdir(parents=True)
            fake_bin.mkdir()
            shutil.copy2(repo_root / "start.sh", project / "start.sh")
            secret_path = project / ".secrets" / ".env"
            secret_path.write_text(
                "# deployment values\nPOSTGRES_PASSWORD=top-secret\nOPTIONAL_EMPTY=\n",
                encoding="utf-8",
            )
            args_path = root / "docker-args.txt"
            fake_docker = fake_bin / "docker"
            fake_docker.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$@" > "$DOCKER_ARGS_FILE"\n',
                encoding="utf-8",
            )
            fake_docker.chmod(0o755)

            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
            env["DOCKER_ARGS_FILE"] = str(args_path)
            result = subprocess.run(
                ["bash", str(project / "start.sh"), "--version"],
                cwd=project,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            args = args_path.read_text(encoding="utf-8").splitlines()
            env_file_index = args.index("--env-file")
            self.assertEqual(args[env_file_index + 1], str(secret_path))
            self.assertIn(
                "COMPOSER_INHERITED_SECRET_KEYS=POSTGRES_PASSWORD,OPTIONAL_EMPTY",
                args,
            )
            self.assertNotIn("top-secret", "\n".join(args))


if __name__ == "__main__":
    unittest.main()
