import sys
import unittest
from unittest.mock import patch

from composer.cli import parse_migrate_args
from composer.constants import DEFAULT_MIGRATOR_COMMAND
from composer.launcher import DockerComposeLauncher


class MigrateParserTests(unittest.TestCase):
    def test_defaults_to_web_and_forwards_migrator_options(self):
        args = parse_migrate_args(["-mm", "-a", "documents"])
        self.assertEqual(args.service, "web")
        self.assertEqual(args.migrator_args, ["-mm", "-a", "documents"])

    def test_composer_options_and_separator_are_consumed(self):
        args = parse_migrate_args(
            ["-d", "-f", "compose.alt.yml", "--service", "worker", "--", "-nm"]
        )
        self.assertTrue(args.dev)
        self.assertEqual(args.file, "compose.alt.yml")
        self.assertEqual(args.service, "worker")
        self.assertEqual(args.migrator_args, ["-nm"])


class MigrateCommandTests(unittest.TestCase):
    def test_missing_label_uses_default_supervised_migrator(self):
        launcher = DockerComposeLauncher()
        with (
            patch.object(launcher, "compose_config_json", return_value={"services": {"web": {}}}),
            patch.object(launcher, "exec_in_service", return_value=0) as execute,
            self.assertRaises(SystemExit) as caught,
        ):
            launcher.handle_migrate(["-mm", "-a", "documents"])

        self.assertEqual(caught.exception.code, 0)
        execute.assert_called_once_with(
            "web", DEFAULT_MIGRATOR_COMMAND.split() + ["-mm", "-a", "documents"]
        )

    def test_service_label_command_is_preferred(self):
        launcher = DockerComposeLauncher()
        configured = "python manage.py migrator"
        config = {
            "services": {
                "worker": {"labels": {"org.dlux.post-start": configured}},
            }
        }
        with (
            patch.object(launcher, "compose_config_json", return_value=config),
            patch.object(launcher, "exec_in_service", return_value=7) as execute,
            self.assertRaises(SystemExit) as caught,
        ):
            launcher.handle_migrate(["--service", "worker", "-nm"])

        self.assertEqual(caught.exception.code, 7)
        execute.assert_called_once_with("worker", ["python", "manage.py", "migrator", "-nm"])

    def test_migrate_is_dispatched_before_flat_arguments(self):
        launcher = DockerComposeLauncher()
        with (
            patch.object(sys, "argv", ["composer", "migrate", "-mm"]),
            patch.object(launcher, "handle_migrate", side_effect=SystemExit(23)) as handle,
            self.assertRaisesRegex(SystemExit, "23"),
        ):
            launcher.run()
        handle.assert_called_once_with(["-mm"])


if __name__ == "__main__":
    unittest.main()
