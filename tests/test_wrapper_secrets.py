import os
import pty
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

    def test_self_update_pulls_before_starting_composer(self):
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            fake_bin = root / "bin"
            project.mkdir()
            fake_bin.mkdir()
            shutil.copy2(repo_root / "start.sh", project / "start.sh")
            calls_path = root / "docker-calls.txt"
            fake_docker = fake_bin / "docker"
            fake_docker.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$*" >> "$DOCKER_CALLS_FILE"\n',
                encoding="utf-8",
            )
            fake_docker.chmod(0o755)

            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
            env["DOCKER_CALLS_FILE"] = str(calls_path)
            env["COMPOSER_SELF_IMAGE"] = "registry.example/composer:test"
            result = subprocess.run(
                ["bash", str(project / "start.sh"), "self", "update"],
                cwd=project,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            calls = calls_path.read_text(encoding="utf-8").splitlines()
            self.assertIn("pull registry.example/composer:test", calls)
            self.assertNotIn("self update", "\n".join(calls))

    def _run_wrapper_capturing_docker_args(self, use_pty):
        """Run start.sh against a fake docker and return the argv it received."""
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            fake_bin = root / "bin"
            project.mkdir()
            fake_bin.mkdir()
            shutil.copy2(repo_root / "start.sh", project / "start.sh")
            args_path = root / "docker-args.txt"
            fake_docker = fake_bin / "docker"
            fake_docker.write_text(
                '#!/bin/sh\n'
                'if [ "$1" = "image" ]; then exit 0; fi\n'
                'printf "%s\\n" "$@" > "$DOCKER_ARGS_FILE"\n',
                encoding="utf-8",
            )
            fake_docker.chmod(0o755)

            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
            env["DOCKER_ARGS_FILE"] = str(args_path)

            argv = ["bash", str(project / "start.sh"), "--version"]
            if use_pty:
                controller, follower = pty.openpty()
                try:
                    subprocess.run(
                        argv,
                        cwd=project,
                        env=env,
                        stdin=follower,
                        stdout=follower,
                        stderr=follower,
                        check=True,
                    )
                finally:
                    os.close(follower)
                    os.close(controller)
            else:
                subprocess.run(
                    argv, cwd=project, env=env, capture_output=True, check=True
                )
            return args_path.read_text(encoding="utf-8").splitlines()

    def test_stdin_is_attached_without_a_terminal_so_piped_input_arrives(self):
        args = self._run_wrapper_capturing_docker_args(use_pty=False)
        self.assertIn("-i", args)
        self.assertNotIn("-t", args)
        self.assertNotIn("-it", args)

    def test_terminal_run_also_allocates_a_tty(self):
        args = self._run_wrapper_capturing_docker_args(use_pty=True)
        self.assertIn("-i", args)
        self.assertIn("-t", args)


if __name__ == "__main__":
    unittest.main()

@unittest.skipIf(os.name == "nt", "Bash wrapper test")
class WrapperDeployerRefreshTests(unittest.TestCase):
    IMAGE = "registry.example/composer:test"

    def _run(self, *argv, pull_exit=0):
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            fake_bin = root / "bin"
            project.mkdir()
            fake_bin.mkdir()
            shutil.copy2(repo_root / "start.sh", project / "start.sh")
            calls_path = root / "docker-calls.txt"
            fake_docker = fake_bin / "docker"
            fake_docker.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$*" >> "$DOCKER_CALLS_FILE"\n'
                'if [ "$1" = pull ]; then exit "$PULL_EXIT"; fi\n',
                encoding="utf-8",
            )
            fake_docker.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
            env["DOCKER_CALLS_FILE"] = str(calls_path)
            env["PULL_EXIT"] = str(pull_exit)
            env["COMPOSER_SELF_IMAGE"] = self.IMAGE
            result = subprocess.run(
                ["bash", str(project / "start.sh"), *argv],
                cwd=project, env=env, capture_output=True, text=True, check=False,
            )
            calls = calls_path.read_text(encoding="utf-8").splitlines() if calls_path.exists() else []
            return result, calls

    def _launch_index(self, calls, command):
        launches = [i for i, call in enumerate(calls) if call.startswith("run -i") and f"{self.IMAGE} {command}" in call]
        return launches[0] if launches else None

    def test_update_self_refreshes_only_the_deployer(self):
        result, calls = self._run("update", "self")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"pull {self.IMAGE}", calls)
        self.assertFalse([call for call in calls if call.startswith("run -i")])

    def test_unscoped_update_refreshes_the_deployer_before_the_stack(self):
        for argv in (("update",), ("-d", "update"), ("update", "-d", "--force")):
            with self.subTest(argv=argv):
                result, calls = self._run(*argv)
                self.assertEqual(result.returncode, 0, result.stderr)
                pull = calls.index(f"pull {self.IMAGE}")
                launch = self._launch_index(calls, "update") if argv[0] == "update" else self._launch_index(calls, "-d update")
                self.assertIsNotNone(launch, calls)
                self.assertLess(pull, launch)

    def test_named_services_skip_the_deployer_refresh(self):
        result, calls = self._run("update", "web", "celery")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse([call for call in calls if call.startswith("pull")])
        self.assertIsNotNone(self._launch_index(calls, "update web celery"))

    def test_self_update_is_a_deprecated_alias(self):
        result, calls = self._run("self", "update")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("deprecated", result.stderr)
        self.assertIn(f"pull {self.IMAGE}", calls)
        self.assertFalse([call for call in calls if call.startswith("run -i")])

    def test_a_failed_deployer_pull_still_updates_the_stack(self):
        result, calls = self._run("update", pull_exit=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("could not refresh the Composer deployer", result.stderr)
        self.assertIsNotNone(self._launch_index(calls, "update"))

    def test_a_failed_update_self_pull_fails(self):
        result, _calls = self._run("update", "self", pull_exit=1)
        self.assertNotEqual(result.returncode, 0)
