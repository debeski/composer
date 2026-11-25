"""How Composer starts child processes: an argv, never a shell string."""

import sys
import unittest
from unittest.mock import MagicMock, patch

from composer import subprocess_runner
from composer.subprocess_runner import SubprocessRunnerMixin


class WindowsArgvTests(unittest.TestCase):
    """Windows rebuilt each command with list2cmdline and ran it with shell=True,
    so cmd.exe re-parsed every argument: `&`, `|` or `^` in a service name, an
    image ref or a path ran a second command."""

    def setUp(self):
        self.runner = SubprocessRunnerMixin()

    def _popen_call(self, method, *args, **kwargs):
        process = MagicMock()
        process.communicate.return_value = ("", "")
        process.returncode = 0
        process.stdout.readline.return_value = ""
        process.stdout.read.return_value = ""
        process.poll.return_value = 0
        with patch.object(subprocess_runner.sys, "platform", "win32"), \
                patch.object(subprocess_runner.shutil, "which", return_value=r"C:\Docker\docker.exe"), \
                patch.object(subprocess_runner.subprocess, "Popen", return_value=process) as popen, \
                patch.object(subprocess_runner.subprocess, "run", return_value=MagicMock(returncode=0)) as run:
            getattr(self.runner, method)(*args, **kwargs)
        return popen.call_args or run.call_args

    def test_every_runner_passes_an_argv_without_a_shell(self):
        hostile = ["docker", "compose", "logs", "web & calc.exe"]
        for method in ("run_command", "run_command_streaming", "run_command_interactive"):
            with self.subTest(method=method):
                call = self._popen_call(method, hostile)
                self.assertEqual(call.args[0], [r"C:\Docker\docker.exe", "compose", "logs", "web & calc.exe"])
                self.assertFalse(call.kwargs.get("shell", False))

    def test_a_batch_file_is_refused(self):
        with patch.object(subprocess_runner.sys, "platform", "win32"), \
                patch.object(subprocess_runner.shutil, "which", return_value=r"C:\bin\docker.cmd"):
            ok, _out, err = self.runner.run_command(["docker", "ps"])
        self.assertFalse(ok)
        self.assertIn("batch file", err)

    def test_posix_argv_is_unchanged(self):
        if sys.platform == "win32":
            self.skipTest("POSIX only")
        self.assertEqual(self.runner._prepare_command(["docker", "ps"]), ["docker", "ps"])


class StreamedOutputBoundTests(unittest.TestCase):
    def test_only_the_tail_is_kept_but_every_line_is_reported(self):
        seen = []
        code = "for i in range(50): print(i)"
        with patch.object(subprocess_runner, "MAX_STREAMED_LINES", 10):
            ok, output, _err = SubprocessRunnerMixin().run_command_streaming(
                [sys.executable, "-c", code], timeout=30, progress_callback=seen.append,
            )
        self.assertTrue(ok)
        self.assertEqual(output.splitlines(), [str(i) for i in range(40, 50)])
        self.assertEqual(len(seen), 50)


if __name__ == "__main__":
    unittest.main()
