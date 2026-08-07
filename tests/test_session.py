import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from composer import session
from composer.launcher import TERMINAL_BOUND_COMMANDS, DockerComposeLauncher
from composer.subprocess_runner import SubprocessRunnerMixin


POSIX_ONLY = unittest.skipIf(sys.platform == "win32", "POSIX signal behaviour")

REPO_ROOT = Path(__file__).resolve().parent.parent


class HangupGuardTests(unittest.TestCase):
    def tearDown(self):
        session._reset_for_tests()

    @POSIX_ONLY
    def test_guard_installs_a_hangup_handler(self):
        previous = signal.getsignal(signal.SIGHUP)
        try:
            self.assertTrue(session.install_hangup_guard())
            self.assertIs(signal.getsignal(signal.SIGHUP), session._on_hangup)
            self.assertIs(signal.getsignal(signal.SIGTTOU), signal.SIG_IGN)
        finally:
            signal.signal(signal.SIGHUP, previous)

    @POSIX_ONLY
    def test_hangup_keeps_the_run_alive_and_moves_output_to_a_log(self):
        """The whole point: closing the terminal must not abort the run."""
        script = textwrap.dedent(
            """
            import os, signal, sys
            sys.path.insert(0, sys.argv[1])
            from composer.session import install_hangup_guard, terminal_detached

            install_hangup_guard()
            print("before hangup")
            os.kill(os.getpid(), signal.SIGHUP)
            assert terminal_detached()
            print("after hangup")
            sys.stderr.write("stderr after hangup\\n")
            sys.exit(0)
            """
        )
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "detached.log")
            env = dict(os.environ, COMPOSER_DETACH_LOG=log_path)
            result = subprocess.run(
                [sys.executable, "-c", script, str(REPO_ROOT)],
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("before hangup", result.stdout)
            self.assertNotIn("after hangup", result.stdout)

            logged = Path(log_path).read_text(encoding="utf-8")
            self.assertIn("keeps running detached", logged)
            self.assertIn("after hangup", logged)
            self.assertIn("stderr after hangup", logged)

    @POSIX_ONLY
    def test_ctrl_c_still_cancels_after_the_guard_is_installed(self):
        script = textwrap.dedent(
            """
            import os, signal, sys
            sys.path.insert(0, sys.argv[1])
            from composer.session import install_hangup_guard

            install_hangup_guard()
            try:
                os.kill(os.getpid(), signal.SIGINT)
            except KeyboardInterrupt:
                sys.exit(130)
            sys.exit(1)
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(REPO_ROOT)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 130, result.stderr)

    def test_detach_log_prefers_explicit_override_then_console_log(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(session.resolve_detach_log(), session.DEFAULT_DETACH_LOG)
        with patch.dict(os.environ, {"COMPOSER_LOG_FILE": "deploy-log.txt"}, clear=True):
            self.assertEqual(session.resolve_detach_log(), "deploy-log.txt")
        with patch.dict(
            os.environ,
            {"COMPOSER_LOG_FILE": "deploy-log.txt", "COMPOSER_DETACH_LOG": "run.log"},
            clear=True,
        ):
            self.assertEqual(session.resolve_detach_log(), "run.log")

    def test_terminal_bound_commands_are_not_guarded(self):
        self.assertEqual(TERMINAL_BOUND_COMMANDS, {"run", "migrate", "log", "logs"})

    @POSIX_ONLY
    def test_deploy_installs_the_guard_but_log_does_not(self):
        for argv, guarded in (([], True), (["update"], True), (["log", "-F"], False)):
            with self.subTest(argv=argv):
                launcher = DockerComposeLauncher()
                with (
                    patch.object(sys, "argv", ["composer"] + argv),
                    patch("composer.launcher.install_hangup_guard") as guard,
                    patch("composer.launcher.parse_args", side_effect=SystemExit(0)),
                    patch.object(
                        DockerComposeLauncher, "handle_log", return_value=None
                    ),
                    patch.object(
                        DockerComposeLauncher, "configure_update", side_effect=SystemExit(0)
                    ),
                    patch.object(DockerComposeLauncher, "cleanup", return_value=None),
                ):
                    try:
                        launcher.run()
                    except SystemExit:
                        pass
                self.assertEqual(guard.called, guarded)


class DetachedChildTests(unittest.TestCase):
    def setUp(self):
        self.runner = SubprocessRunnerMixin()

    @POSIX_ONLY
    def test_compose_children_run_in_their_own_session(self):
        self.assertEqual(
            self.runner._detached_child_kwargs(), {"start_new_session": True}
        )

    @POSIX_ONLY
    def test_streaming_child_leaves_the_terminal_process_group(self):
        """A hangup aimed at composer's process group cannot reach Compose."""
        ok, output, err = self.runner.run_command_streaming(
            [sys.executable, "-c", "import os; print(os.getpgrp())"],
            timeout=30,
        )
        self.assertTrue(ok, f"{output}\n{err}")
        self.assertNotEqual(int(output.strip()), os.getpgrp())

    @POSIX_ONLY
    def test_ctrl_c_relays_the_interrupt_to_the_detached_child(self):
        child = textwrap.dedent(
            """
            import sys, time
            print("child started", flush=True)
            try:
                time.sleep(30)
            except KeyboardInterrupt:
                print("child interrupted", flush=True)
                sys.exit(130)
            """
        )

        def interrupt(line):
            if line.strip() == "child started":
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.runner.run_command_streaming(
                [sys.executable, "-c", child],
                timeout=30,
                progress_callback=interrupt,
            )

    def test_run_command_still_captures_output_and_timeouts(self):
        ok, out, err = self.runner.run_command(
            [sys.executable, "-c", "import sys; print('hi'); sys.stderr.write('bye')"],
            timeout=30,
        )
        self.assertTrue(ok)
        self.assertIn("hi", out)
        self.assertIn("bye", err)

        ok, _, err = self.runner.run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=1,
        )
        self.assertFalse(ok)
        self.assertIn("timed out", err)

    def test_missing_executable_is_reported_not_raised(self):
        ok, _, err = self.runner.run_command(["composer-no-such-binary-xyz"])
        self.assertFalse(ok)
        self.assertTrue(err)


class DetachedRenderingTests(unittest.TestCase):
    def tearDown(self):
        session._reset_for_tests()

    def test_progress_lines_drop_cursor_control_when_detached(self):
        launcher = DockerComposeLauncher()
        with patch("builtins.print") as printer:
            launcher.print_progress_line("pull", "downloading")
        self.assertIn("\033[2K", printer.call_args.args[0])

        session._detached = True
        with patch("builtins.print") as printer:
            launcher.print_progress_line("pull", "downloading")
        self.assertNotIn("\033[2K", printer.call_args.args[0])

    def test_render_appends_plain_frames_when_detached(self):
        launcher = DockerComposeLauncher()
        launcher.resolve_active_compose_files()
        session._detached = True
        printed = []
        with patch("builtins.print", side_effect=lambda *a, **k: printed.append(a[0] if a else "")):
            launcher.render()

        self.assertEqual(launcher.last_render_line_count, 0)
        joined = "\n".join(str(line) for line in printed)
        self.assertIn("COMPOSER", joined)
        self.assertNotIn("\033[", joined)


if __name__ == "__main__":
    unittest.main()
