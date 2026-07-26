import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from composer.stack_cleanup import (
    StackCleanupError,
    remove_obsolete_service_blocks,
    remove_obsolete_services,
)


COMPOSE = """name: demo

services:
  web:
    image: example/web:latest

  pgadmin:
    image: dpage/pgadmin4:latest
    volumes:
      - pgadmin_data:/var/lib/pgadmin

  db-backup:
    image: example/backup:latest
    volumes:
      - database_backups:/backups

  db_backup:
    image: example/old-backup:latest

volumes:
  pgadmin_data:
  database_backups:
"""


ORIGINAL_MODEL = json.dumps(
    {
        "services": {
            "web": {},
            "pgadmin": {},
            "db-backup": {},
            "db_backup": {},
        },
        "volumes": {
            "pgadmin_data": {"name": "demo_pgadmin_data"},
            "database_backups": {"name": "demo_database_backups"},
        },
    }
)
CLEAN_MODEL = json.dumps(
    {
        "services": {"web": {}},
        "volumes": {
            "pgadmin_data": {"name": "demo_pgadmin_data"},
            "database_backups": {"name": "demo_database_backups"},
        },
    }
)
EXISTING_VOLUMES = "demo_pgadmin_data\ndemo_database_backups\n"


def _completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _successful_lifecycle():
    return [
        _completed(),
        _completed(stdout=ORIGINAL_MODEL),
        _completed(stdout=EXISTING_VOLUMES),
        _completed(stdout=CLEAN_MODEL),
        _completed(),
        _completed(stdout=CLEAN_MODEL),
        _completed(stdout=EXISTING_VOLUMES),
    ]


class StackCleanupTransformTests(unittest.TestCase):
    def test_removes_all_obsolete_spellings_but_keeps_data_volumes(self):
        updated, removed = remove_obsolete_service_blocks(COMPOSE)

        self.assertEqual(removed, {"pgadmin", "db-backup", "db_backup"})
        self.assertIn("  web:", updated)
        self.assertNotIn("  pgadmin:", updated)
        self.assertNotIn("  db-backup:", updated)
        self.assertNotIn("  db_backup:", updated)
        self.assertIn("  pgadmin_data:", updated)
        self.assertIn("  database_backups:", updated)

    def test_inline_service_and_following_comment_are_handled(self):
        contents = """services:
  pgadmin: {image: dpage/pgadmin4:latest}
  # Main application
  web:
    image: example/web:latest
"""
        updated, removed = remove_obsolete_service_blocks(contents)

        self.assertEqual(removed, {"pgadmin"})
        self.assertIn("  # Main application", updated)
        self.assertIn("  web:", updated)

    def test_clean_compose_is_unchanged(self):
        contents = "services:\n  web:\n    image: example/web:latest\n"
        self.assertEqual(remove_obsolete_service_blocks(contents), (contents, set()))


class StackCleanupApplyTests(unittest.TestCase):
    def test_validates_backs_up_and_applies_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compose = root / "compose.yml"
            compose.write_text(COMPOSE, encoding="utf-8")
            runner = Mock(side_effect=_successful_lifecycle())

            with patch("composer.stack_cleanup.shutil.which", return_value="/usr/bin/docker"):
                result = remove_obsolete_services(
                    str(root),
                    ["compose.yml"],
                    command_runner=runner,
                )

            self.assertTrue(result["applied"])
            self.assertEqual(
                result["removed_services"],
                ["db-backup", "db_backup", "pgadmin"],
            )
            self.assertNotIn("  pgadmin:", compose.read_text(encoding="utf-8"))
            backup = Path(result["backup_root"]) / "original" / "compose.yml"
            self.assertEqual(backup.read_text(encoding="utf-8"), COMPOSE)
            self.assertTrue(result["container_cleanup_applied"])
            self.assertTrue(result["postflight_verified"])
            self.assertEqual(
                result["preserved_volumes"],
                ["demo_database_backups", "demo_pgadmin_data"],
            )
            validation = runner.call_args_list[3].args[0]
            self.assertEqual(validation[-3:], ["config", "--format", "json"])
            self.assertIn("--project-directory", validation)
            cleanup = runner.call_args_list[4].args[0]
            self.assertEqual(
                cleanup[-6:],
                ["rm", "-s", "-f", "db-backup", "db_backup", "pgadmin"],
            )
            self.assertNotIn("up", cleanup)

    def test_validation_failure_keeps_source_and_archives_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compose = root / "compose.yml"
            compose.write_text(COMPOSE, encoding="utf-8")
            runner = Mock(
                side_effect=[
                    _completed(),
                    _completed(stdout=ORIGINAL_MODEL),
                    _completed(stdout=EXISTING_VOLUMES),
                    _completed(1, stderr="invalid"),
                ]
            )

            with (
                patch("composer.stack_cleanup.shutil.which", return_value="/usr/bin/docker"),
                self.assertRaises(StackCleanupError),
            ):
                remove_obsolete_services(
                    str(root),
                    ["compose.yml"],
                    command_runner=runner,
                )

            self.assertEqual(compose.read_text(encoding="utf-8"), COMPOSE)
            rejected = list((root / ".xpose" / "composer-check").glob("*/rejected/compose.yml"))
            self.assertEqual(len(rejected), 1)
            self.assertNotIn("  pgadmin:", rejected[0].read_text(encoding="utf-8"))

    def test_multiple_active_compose_files_are_cleaned_together(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "compose.yml"
            override = root / "compose.dev.yml"
            base.write_text(
                "services:\n  web:\n    image: example/web:latest\n  pgadmin:\n    image: dpage/pgadmin4:latest\n",
                encoding="utf-8",
            )
            override.write_text(
                "services:\n  db_backup:\n    environment:\n      KEEP_DAYS: 7\n",
                encoding="utf-8",
            )
            runner = Mock(side_effect=_successful_lifecycle())

            with patch("composer.stack_cleanup.shutil.which", return_value="/usr/bin/docker"):
                result = remove_obsolete_services(
                    str(root),
                    ["compose.yml", "compose.dev.yml"],
                    command_runner=runner,
                )

            self.assertNotIn("  pgadmin:", base.read_text(encoding="utf-8"))
            self.assertNotIn("  db_backup:", override.read_text(encoding="utf-8"))
            backup = Path(result["backup_root"]) / "original"
            self.assertTrue((backup / "compose.yml").is_file())
            self.assertTrue((backup / "compose.dev.yml").is_file())
            validation = runner.call_args_list[3].args[0]
            self.assertEqual(validation.count("-f"), 2)

    def test_targeted_container_failure_keeps_original_compose(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compose = root / "compose.yml"
            compose.write_text(COMPOSE, encoding="utf-8")
            lifecycle = _successful_lifecycle()
            lifecycle[4] = _completed(1, stderr="container removal failed")
            runner = Mock(side_effect=lifecycle)

            with (
                patch("composer.stack_cleanup.shutil.which", return_value="/usr/bin/docker"),
                self.assertRaisesRegex(StackCleanupError, "container removal failed"),
            ):
                remove_obsolete_services(
                    str(root),
                    ["compose.yml"],
                    command_runner=runner,
                )

            self.assertEqual(compose.read_text(encoding="utf-8"), COMPOSE)
            originals = list(
                (root / ".xpose" / "composer-check").glob("*/original/compose.yml")
            )
            self.assertEqual(len(originals), 1)

    def test_postflight_fails_if_existing_named_volume_disappears(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compose = root / "compose.yml"
            compose.write_text(COMPOSE, encoding="utf-8")
            lifecycle = _successful_lifecycle()
            lifecycle[-1] = _completed(stdout="demo_pgadmin_data\n")
            runner = Mock(side_effect=lifecycle)

            with (
                patch("composer.stack_cleanup.shutil.which", return_value="/usr/bin/docker"),
                self.assertRaisesRegex(StackCleanupError, "demo_database_backups"),
            ):
                remove_obsolete_services(
                    str(root),
                    ["compose.yml"],
                    command_runner=runner,
                )

            self.assertNotIn("  pgadmin:", compose.read_text(encoding="utf-8"))
            originals = list(
                (root / ".xpose" / "composer-check").glob("*/original/compose.yml")
            )
            self.assertEqual(len(originals), 1)

    def test_clean_stack_is_idempotent_without_docker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "compose.yml").write_text(
                "services:\n  web:\n    image: example/web:latest\n",
                encoding="utf-8",
            )
            runner = Mock()

            result = remove_obsolete_services(
                str(root),
                ["compose.yml"],
                command_runner=runner,
            )

            self.assertFalse(result["applied"])
            self.assertEqual(result["removed_services"], [])
            runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
