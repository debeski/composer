import io
import os
import sys
import unittest
from unittest.mock import patch

from composer.cli import parse_pull_args, parse_update_args
from composer.launcher import DockerComposeLauncher


class UpdateCommandTests(unittest.TestCase):
    def test_parser_accepts_options_and_multiple_services(self):
        args = parse_update_args(
            ["-d", "-b", "--force", "-f", "compose.alt.yml", "web", "celery"]
        )

        self.assertTrue(args.dev)
        self.assertTrue(args.build)
        self.assertTrue(args.force)
        self.assertEqual(args.file, "compose.alt.yml")
        self.assertEqual(args.service, ["web", "celery"])

    def test_default_update_pulls_then_recreates_every_service(self):
        launcher = DockerComposeLauncher()
        with patch.dict(os.environ, {}, clear=True):
            launcher.configure_update([])

        self.assertTrue(launcher.update_images)
        self.assertFalse(launcher.pull_only_mode)
        self.assertIsNone(launcher.pull_service)
        self.assertIsNone(launcher.up_service)

    def test_named_services_scope_both_the_pull_and_the_recreate(self):
        launcher = DockerComposeLauncher()
        with patch.dict(os.environ, {}, clear=True):
            launcher.configure_update(["web", "celery"])

        self.assertEqual(launcher.pull_service, ["web", "celery"])
        self.assertEqual(launcher.up_service, ["web", "celery"])

        with patch.object(
            launcher, "run_docker_compose_streaming", return_value=(True, "", "")
        ) as run:
            launcher.pull_images()
            self.assertEqual(run.call_args.args[0], ["pull", "web", "celery"])
            launcher.launch_containers()
            self.assertEqual(run.call_args.args[0], ["up", "-d", "web", "celery"])

    def test_pull_subcommand_only_pulls_without_recreating(self):
        launcher = DockerComposeLauncher()
        with patch.dict(os.environ, {}, clear=True):
            launcher.configure_pull(["web"])

        self.assertTrue(launcher.pull_only_mode)
        self.assertEqual(launcher.pull_service, ["web"])
        self.assertIsNone(launcher.up_service)

    def test_pull_parser_accepts_options_and_multiple_services(self):
        args = parse_pull_args(
            ["-d", "-f", "compose.alt.yml", "--status-file", "pull.json", "web", "celery"]
        )

        self.assertTrue(args.dev)
        self.assertEqual(args.file, "compose.alt.yml")
        self.assertEqual(args.status_file, "pull.json")
        self.assertEqual(args.service, ["web", "celery"])

    def test_a_single_service_string_scope_still_works(self):
        launcher = DockerComposeLauncher()
        launcher.pull_service = "web"
        launcher.up_service = "web"

        with patch.object(
            launcher, "run_docker_compose_streaming", return_value=(True, "", "")
        ) as run:
            launcher.pull_images()
            self.assertEqual(run.call_args.args[0], ["pull", "web"])
            launcher.launch_containers()
            self.assertEqual(run.call_args.args[0], ["up", "-d", "web"])

    def test_status_file_flag_overrides_the_environment(self):
        launcher = DockerComposeLauncher()
        with patch.dict(os.environ, {"COMPOSER_STATUS_FILE": "env.json"}, clear=True):
            launcher.configure_update(["--status-file", "flag.json"])
        self.assertEqual(launcher.status_file, "flag.json")

        launcher = DockerComposeLauncher()
        with patch.dict(os.environ, {"COMPOSER_STATUS_FILE": "env.json"}, clear=True):
            launcher.configure_update([])
        self.assertEqual(launcher.status_file, "env.json")

    def test_version_gate_scope_follows_the_named_services(self):
        launcher = DockerComposeLauncher()
        with patch.dict(os.environ, {}, clear=True):
            launcher.configure_update(["web", "celery"])

        with patch.object(
            launcher, "run_docker_compose", return_value=(True, "", "")
        ) as run:
            launcher.compose_config_images()

        self.assertEqual(
            run.call_args.args[0], ["config", "--images", "web", "celery"]
        )

    def test_update_is_dispatched_before_flat_arguments(self):
        launcher = DockerComposeLauncher()
        with (
            patch.object(sys, "argv", ["composer", "update", "web"]),
            patch.object(launcher, "configure_update", side_effect=SystemExit(41)) as configure,
            self.assertRaisesRegex(SystemExit, "41"),
        ):
            launcher.run()

        configure.assert_called_once_with(["web"])

    def test_pull_is_dispatched_before_flat_arguments(self):
        launcher = DockerComposeLauncher()
        with (
            patch.object(sys, "argv", ["composer", "pull", "web"]),
            patch.object(launcher, "configure_pull", side_effect=SystemExit(42)) as configure,
            self.assertRaisesRegex(SystemExit, "42"),
        ):
            launcher.run()

        configure.assert_called_once_with(["web"])

    def test_retired_update_only_flag_points_to_pull(self):
        for argv in (
            ["composer", "-uo"],
            ["composer", "-d", "--update-only", "web"],
        ):
            with self.subTest(argv=argv):
                launcher = DockerComposeLauncher()
                with (
                    patch.object(sys, "argv", argv),
                    patch("sys.stderr", new_callable=io.StringIO) as stderr,
                    self.assertRaises(SystemExit) as caught,
                ):
                    launcher.run()

                self.assertEqual(caught.exception.code, 2)
                self.assertIn("composer pull", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
