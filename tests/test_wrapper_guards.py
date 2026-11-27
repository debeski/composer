"""Wrapper 8 (`start.sh`): break-it round 2 R2-024, R2-029, R2-071, R2-072, R2-128, R2-086 (a)."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from composer import image_pin

REPO = Path(__file__).resolve().parents[1]
B2 = "debeski/composer:v1.11.0b2@sha256:" + "a" * 64


@unittest.skipIf(os.name == "nt", "Bash wrapper test")
class WrapperGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.project = root / "project"
        self.project.joinpath(".secrets").mkdir(parents=True)
        shutil.copy2(REPO / "start.sh", self.project / "start.sh")
        (self.project / ".secrets" / ".env").write_text("POSTGRES_PASSWORD=x\nWEB_IMAGE=demo:1\n")
        (self.project / "compose.yml").write_text("services:\n  web:\n    image: ${WEB_IMAGE:-demo:dev}\n")
        bin_dir = root / "bin"
        bin_dir.mkdir()
        self.args = root / "docker-args.txt"
        fake = bin_dir / "docker"
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$DOCKER_ARGS_FILE"\nexit 0\n')
        fake.chmod(0o755)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("COMPOSER_") and k != "WEB_IMAGE"}
        self.env.update(PATH=f"{bin_dir}:{os.environ.get('PATH', '')}", DOCKER_ARGS_FILE=str(self.args))

    def run_wrapper(self, *argv, env=None):
        result = subprocess.run(
            ["bash", str(self.project / "start.sh"), *(argv or ("--version",))],
            cwd=self.project, env={**self.env, **(env or {})}, capture_output=True, text=True, check=False,
        )
        calls = self.args.read_text().splitlines() if self.args.exists() else []
        return result, calls

    # R2-024 -----------------------------------------------------------------
    def test_a_leading_blank_line_still_reads_the_lock(self):
        (self.project / ".composer-image").write_text(f"\n  {B2}\r\n")
        result, calls = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(B2, calls)
        self.assertNotIn("debeski/composer:latest", calls)
        self.assertEqual(image_pin.read_lock(self.project).ref, B2)

    def test_a_byte_order_mark_reads_the_lock(self):
        (self.project / ".composer-image").write_bytes(b"\xef\xbb\xbf" + B2.encode() + b"\n")
        result, calls = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(B2, calls)

    def test_whitespace_inside_the_reference_is_no_lock_on_both_sides(self):
        (self.project / ".composer-image").write_text(B2.replace("@", " @") + "\n")
        result, calls = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(B2, calls)
        self.assertIn("debeski/composer:latest", calls)
        self.assertIsNone(image_pin.read_lock(self.project))

    # R2-029 -----------------------------------------------------------------
    def test_a_stray_project_env_shadowing_the_secrets_is_refused(self):
        (self.project / ".env").write_text("TIME_ZONE=UTC\n")
        result, calls = self.run_wrapper()
        self.assertEqual(result.returncode, 1)
        self.assertIn(".env is used, but .secrets/.env holds keys it does not (POSTGRES_PASSWORD, WEB_IMAGE)",
                      result.stderr)
        self.assertIn(".xclude/", result.stderr)
        self.assertEqual(calls, [])

    def test_a_first_file_holding_every_key_is_used(self):
        (self.project / ".env").write_text("POSTGRES_PASSWORD=y\nWEB_IMAGE=demo:2\nEXTRA=1\n")
        result, calls = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[calls.index("--env-file") + 1], str(self.project / ".env"))

    # R2-071 -----------------------------------------------------------------
    def test_export_lines_are_refused_naming_the_line(self):
        (self.project / ".secrets" / ".env").write_text("# x\nexport POSTGRES_PASSWORD=x\n")
        result, _ = self.run_wrapper()
        self.assertEqual(result.returncode, 1)
        self.assertIn("line 2: write KEY=value, with no 'export' and no spaces around '='", result.stderr)

    def test_spaces_around_the_equals_sign_are_refused(self):
        (self.project / ".secrets" / ".env").write_text("POSTGRES_PASSWORD = x\n")
        result, _ = self.run_wrapper()
        self.assertEqual(result.returncode, 1)
        self.assertIn("line 1", result.stderr)

    # R2-072 -----------------------------------------------------------------
    def test_a_compose_file_linked_from_outside_the_project_is_refused(self):
        elsewhere = Path(self.tmp.name) / "elsewhere"
        elsewhere.mkdir()
        (self.project / "compose.yml").rename(elsewhere / "compose.yml")
        (self.project / "compose.yml").symlink_to("../elsewhere/compose.yml")
        result, calls = self.run_wrapper()
        self.assertEqual(result.returncode, 1)
        self.assertIn("compose.yml is a symbolic link to", result.stderr)
        self.assertIn("outside the project directory", result.stderr)
        self.assertEqual(calls, [])

    def test_a_link_inside_the_project_works(self):
        (self.project / "config").mkdir()
        (self.project / "compose.yml").rename(self.project / "config" / "compose.yml")
        (self.project / "compose.yml").symlink_to("config/compose.yml")
        result, _ = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)

    # R2-128 -----------------------------------------------------------------
    def test_a_compose_variable_set_only_in_the_shell_is_named(self):
        (self.project / ".secrets" / ".env").write_text("POSTGRES_PASSWORD=x\n")
        result, calls = self.run_wrapper(env={"WEB_IMAGE": "debeski/archive:latest"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WEB_IMAGE set in this shell, not in .secrets/.env", result.stderr)
        self.assertNotIn("debeski/archive:latest", "\n".join(calls))

    def test_a_variable_the_secrets_file_supplies_is_not_named(self):
        result, _ = self.run_wrapper(env={"WEB_IMAGE": "other"})
        self.assertNotIn("set in this shell", result.stderr)

    def test_a_compose_file_without_variables_is_fine(self):
        (self.project / "compose.yml").write_text("services:\n  web:\n    image: demo\n")
        result, _ = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)

    # R2-086 (a) -------------------------------------------------------------
    def test_a_failed_pin_points_at_agent_update(self):
        (self.project / ".composer-image").write_text(B2 + "\n")
        fake = Path(self.tmp.name) / "bin" / "docker"
        fake.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$@" >> "$DOCKER_ARGS_FILE"\n'
            'case "$*" in *"update self --pin"*) echo "usage: composer ..." >&2; exit 2;; esac\n'
            'case "$*" in *org.dlux.composer.image-pin*) echo 1;; esac\nexit 0\n'
        )
        result, _ = self.run_wrapper("update", "self")
        self.assertEqual(result.returncode, 1)
        self.assertIn("run './start.sh agent update'", result.stderr)


class LockRuleTests(unittest.TestCase):
    """R2-024: one rule in both places."""

    def _read(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".composer-image").write_text(text)
            pin = image_pin.read_lock(tmp)
        return pin.ref if pin else None

    def test_the_first_non_empty_line_counts(self):
        self.assertEqual(self._read(f"\n\n{B2}\n"), B2)
        self.assertEqual(self._read(f"\t{B2}  \r\nignored\n"), B2)

    def test_anything_inside_the_reference_is_kept(self):
        self.assertIsNone(self._read(B2[:20] + " " + B2[20:]))

    def test_a_byte_order_mark_is_dropped(self):
        self.assertEqual(self._read("\ufeff" + B2 + "\r\n"), B2)

    def test_lock_state_says_unreadable_for_a_present_malformed_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(image_pin.lock_state(tmp), ("absent", None))
            Path(tmp, ".composer-image").write_text("debeski/composer:beta\n")
            self.assertEqual(image_pin.lock_state(tmp), ("unreadable", None))
            Path(tmp, ".composer-image").write_text(B2 + "\n")
            state, pin = image_pin.lock_state(tmp)
            self.assertEqual((state, pin.ref), ("valid", B2))


if __name__ == "__main__":
    unittest.main()
