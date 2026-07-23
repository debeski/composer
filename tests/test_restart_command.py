import os
import sys
import unittest
from unittest.mock import patch

from composer.cli import parse_restart_args
from composer.launcher import DockerComposeLauncher


class RestartCommandTests(unittest.TestCase):
    def test_parser_accepts_restart_options_and_optional_service(self):
        args = parse_restart_args(
            ["-d", "-f", "compose.alt.yml", "--status-file", "restart.json", "web"]
        )

        self.assertTrue(args.dev)
        self.assertEqual(args.file, "compose.alt.yml")
        self.assertEqual(args.status_file, "restart.json")
        self.assertEqual(args.service, "web")

    def test_restart_configuration_uses_command_arguments(self):
        launcher = DockerComposeLauncher()
        with patch.dict(os.environ, {}, clear=True):
            launcher.configure_restart(
                ["-d", "-f", "compose.alt.yml", "--status-file", "restart.json", "web"]
            )

        self.assertTrue(launcher.restart_mode)
        self.assertEqual(launcher.restart_service, "web")
        self.assertEqual(launcher.active_compose_files, ["compose.alt.yml"])
        self.assertTrue(launcher.dev_mode)
        self.assertEqual(launcher.status_file, "restart.json")

    def test_unscoped_restart_uses_internal_safe_service_list(self):
        launcher = DockerComposeLauncher()
        with patch.dict(
            os.environ,
            {"COMPOSER_RESTART_SERVICES": "web,celery"},
            clear=True,
        ):
            launcher.configure_restart([])
        self.assertEqual(launcher.restart_services, ["web", "celery"])

        with patch.object(
            launcher,
            "run_docker_compose_streaming",
            return_value=(True, "", ""),
        ) as run:
            launcher.restart_containers()
        self.assertEqual(run.call_args.args[0], ["restart", "web", "celery"])

    def test_unscoped_restart_filters_explicit_exclusions_from_safe_list(self):
        launcher = DockerComposeLauncher()
        with patch.dict(
            os.environ,
            {
                "COMPOSER_RESTART_SERVICES": "web,db,celery",
                "COMPOSER_EXCLUDE_SERVICES": "db",
            },
            clear=True,
        ):
            launcher.configure_restart([])

        self.assertEqual(launcher.restart_services, ["web", "celery"])

    def test_restart_and_short_alias_are_dispatched_before_flat_arguments(self):
        for command in ("restart", "-r", "--restart"):
            with self.subTest(command=command):
                launcher = DockerComposeLauncher()
                with (
                    patch.object(sys, "argv", ["composer", command, "web"]),
                    patch.object(
                        launcher,
                        "configure_restart",
                        side_effect=SystemExit(23),
                    ) as configure_restart,
                    self.assertRaisesRegex(SystemExit, "23"),
                ):
                    launcher.run()

                configure_restart.assert_called_once_with(["web"])


if __name__ == "__main__":
    unittest.main()
