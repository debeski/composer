import io
import os
import sys
import unittest
from unittest.mock import patch

from composer.cli import parse_log_args, parse_stop_args
from composer.confirmation import confirm
from composer.launcher import DockerComposeLauncher


class ConfirmationTests(unittest.TestCase):
    def test_only_y_or_yes_confirms(self):
        for answer, expected in (
            ("y", True),
            ("YES", True),
            ("  yes  ", True),
            ("n", False),
            ("", False),
            ("yeah", False),
        ):
            with self.subTest(answer=answer):
                self.assertIs(
                    confirm(
                        "destroy",
                        reader=lambda _: answer,
                        interactive=True,
                        stream=io.StringIO(),
                    ),
                    expected,
                )

    def test_assume_yes_and_env_skip_the_prompt(self):
        def reader(_):
            raise AssertionError("should not prompt")

        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(confirm("destroy", assume_yes=True, reader=reader))
        with patch.dict(os.environ, {"COMPOSER_ASSUME_YES": "1"}, clear=True):
            self.assertTrue(confirm("destroy", reader=reader))

    def test_non_interactive_stdin_fails_closed(self):
        stream = io.StringIO()
        with patch.dict(os.environ, {}, clear=True):
            result = confirm("destroy", interactive=False, stream=stream)
        self.assertFalse(result)
        self.assertIn("-y/--yes", stream.getvalue())


class StopCommandTests(unittest.TestCase):
    def test_parser_accepts_options_and_multiple_services(self):
        args = parse_stop_args(["-d", "-f", "compose.alt.yml", "-y", "web", "celery"])

        self.assertTrue(args.dev)
        self.assertTrue(args.yes)
        self.assertEqual(args.file, "compose.alt.yml")
        self.assertEqual(args.service, ["web", "celery"])

    def test_configuration_uses_command_arguments(self):
        launcher = DockerComposeLauncher()
        launcher.configure_stop(["-d", "-f", "compose.alt.yml", "web"])

        self.assertTrue(launcher.down_mode)
        self.assertEqual(launcher.down_services, ["web"])
        self.assertEqual(launcher.active_compose_files, ["compose.alt.yml"])
        self.assertTrue(launcher.dev_mode)

    def test_destructive_flags_cannot_be_scoped_to_services(self):
        for flag in ("-v", "-p"):
            with self.subTest(flag=flag):
                launcher = DockerComposeLauncher()
                with (
                    patch("sys.stderr", new_callable=io.StringIO),
                    self.assertRaises(SystemExit) as caught,
                ):
                    launcher.configure_stop([flag, "web"])
                self.assertEqual(caught.exception.code, 2)

    def test_named_services_are_passed_to_compose_down(self):
        launcher = DockerComposeLauncher()
        launcher.configure_stop(["web", "celery"])

        with patch.object(launcher, "run_docker_compose", return_value=(True, "", "")) as run:
            launcher.down_containers()

        self.assertEqual(run.call_args.args[0], ["down", "web", "celery"])

    def test_purge_confirmation_is_required_and_bypassable(self):
        launcher = DockerComposeLauncher()
        launcher.configure_stop(["-p"])
        stream = io.StringIO()

        with patch.dict(os.environ, {}, clear=True):
            with patch("composer.launcher.confirm", return_value=False) as prompt:
                self.assertFalse(launcher.confirm_stop())
            self.assertFalse(prompt.call_args.kwargs["assume_yes"])

            launcher.configure_stop(["-p", "-y"])
            self.assertTrue(launcher.confirm_stop())

        self.assertEqual(stream.getvalue(), "")

    def test_prompt_names_the_command_the_user_typed(self):
        for command, expected in (("stop", "composer stop"), ("down", "composer down")):
            with self.subTest(command=command):
                launcher = DockerComposeLauncher()
                launcher.configure_stop(["-v"], command=command)
                with patch("composer.launcher.confirm", return_value=True) as prompt:
                    launcher.confirm_stop()
                self.assertEqual(prompt.call_args.args[0].split(" --")[0], expected)

    def test_plain_stop_is_not_gated(self):
        launcher = DockerComposeLauncher()
        launcher.configure_stop([])

        def reader(_):
            raise AssertionError("should not prompt")

        with patch("composer.confirmation.input", reader, create=True):
            self.assertTrue(launcher.confirm_stop())

    def test_stop_and_its_down_alias_are_dispatched_before_flat_arguments(self):
        for command in ("stop", "down"):
            with self.subTest(command=command):
                launcher = DockerComposeLauncher()
                with (
                    patch.object(sys, "argv", ["composer", command, "-v"]),
                    patch.object(
                        launcher, "configure_stop", side_effect=SystemExit(31)
                    ) as configure,
                    self.assertRaisesRegex(SystemExit, "31"),
                ):
                    launcher.run()

                configure.assert_called_once_with(["-v"], command=command)

    def test_flat_down_flag_still_honours_yes(self):
        launcher = DockerComposeLauncher()
        with (
            patch.object(sys, "argv", ["composer", "--down", "-v", "-y"]),
            patch.object(launcher, "extract_config"),
            patch.object(launcher, "discover_services", return_value=True),
            patch.object(launcher, "down_containers", return_value=(True, "")) as down,
        ):
            launcher.run()

        self.assertTrue(launcher.assume_yes)
        down.assert_called_once()


class LogCommandTests(unittest.TestCase):
    def test_parser_defaults_to_fifty_lines_for_the_whole_stack(self):
        args = parse_log_args([])

        self.assertEqual(args.tail, "50")
        self.assertEqual(args.service, [])
        self.assertFalse(args.follow)

    def test_tail_all_and_zero_lift_the_limit(self):
        for value in ("all", "0"):
            with self.subTest(value=value):
                launcher = DockerComposeLauncher()
                with (
                    patch.object(launcher, "stream_service_logs", return_value=0) as stream,
                    self.assertRaises(SystemExit),
                ):
                    launcher.handle_log(["-n", value])
                self.assertEqual(stream.call_args.kwargs["tail"], "all")

    def test_invalid_tail_is_rejected(self):
        launcher = DockerComposeLauncher()
        with (
            patch("sys.stderr", new_callable=io.StringIO),
            self.assertRaises(SystemExit) as caught,
        ):
            launcher.handle_log(["-n", "lots"])
        self.assertEqual(caught.exception.code, 2)

    def test_named_services_and_options_reach_compose_logs(self):
        launcher = DockerComposeLauncher()
        launcher.resolve_active_compose_files()

        with (
            patch.object(launcher, "resolve_compose_cli", return_value=["docker", "compose"]),
            patch.object(launcher, "run_command_interactive", return_value=0) as interactive,
            patch.object(sys.stdout, "isatty", return_value=True),
        ):
            launcher.stream_service_logs(["web"], tail="50", follow=True, timestamps=True)

        argv = interactive.call_args.args[0]
        self.assertEqual(argv[-6:], ["logs", "--tail", "50", "--follow", "--timestamps", "web"])

    def test_log_and_logs_are_dispatched_before_flat_arguments(self):
        for command in ("log", "logs"):
            with self.subTest(command=command):
                launcher = DockerComposeLauncher()
                with (
                    patch.object(sys, "argv", ["composer", command, "web"]),
                    patch.object(launcher, "handle_log", side_effect=SystemExit(17)) as handle,
                    self.assertRaisesRegex(SystemExit, "17"),
                ):
                    launcher.run()

                handle.assert_called_once_with(["web"])


if __name__ == "__main__":
    unittest.main()
