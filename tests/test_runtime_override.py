import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from composer.docker_compose_manager import DockerComposeMixin


class RuntimeOverrideHarness(DockerComposeMixin):
    def __init__(self):
        self.services = ["web"]
        self.composer_version = "test-version"
        self.dev_mode = False
        self.compose_runtime_override = None
        self.last_runtime_diagnostic = ""


class RuntimeOverrideTests(unittest.TestCase):
    def test_override_does_not_require_a_writable_project_directory(self):
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as project_dir:
            project_path = Path(project_dir)
            project_path.chmod(0o555)
            try:
                os.chdir(project_path)
                launcher = RuntimeOverrideHarness()

                self.assertTrue(launcher.sync_runtime_compose_override())
                override = launcher.compose_runtime_override
                self.assertIsNotNone(override)
                self.assertNotEqual(override.parent.resolve(), project_path.resolve())
                self.assertEqual(list(project_path.iterdir()), [])
                self.assertEqual(
                    override.read_text(encoding="utf-8"),
                    'services:\n  web:\n    environment:\n      COMPOSER_VERSION: "test-version"\n',
                )

                launcher.remove_runtime_compose_override()
                self.assertFalse(override.exists())
            finally:
                os.chdir(original_cwd)
                project_path.chmod(0o755)

    def test_tempfile_creation_failure_becomes_a_runtime_diagnostic(self):
        launcher = RuntimeOverrideHarness()

        with patch(
            "composer.docker_compose_manager.tempfile.mkstemp",
            side_effect=PermissionError("temporary storage unavailable"),
        ):
            self.assertFalse(launcher.sync_runtime_compose_override())

        self.assertIsNone(launcher.compose_runtime_override)
        self.assertIn("Failed to create Composer runtime override", launcher.last_runtime_diagnostic)
        self.assertIn("temporary storage unavailable", launcher.last_runtime_diagnostic)


if __name__ == "__main__":
    unittest.main()
