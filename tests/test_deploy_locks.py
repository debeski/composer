"""One operation at a time per project: break-it round 2 R2-016, R2-017, R2-078, R2-079, R2-127."""
import errno
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from composer import check_lock, confirmation, executor_ops, guidance
from composer.launcher import DockerComposeLauncher


class _InTemp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, cwd)
        self.root = Path(self.tmp.name)


class DeployLockTests(_InTemp):
    """R2-079: two `./start.sh` at once; R2-016: a `check --fix` during a deploy."""

    def test_a_deploy_holds_the_lock_check_fix_takes(self):
        deploy = DockerComposeLauncher()
        deploy.take_project_lock()
        try:
            with self.assertRaises(check_lock.CheckFixLockError) as raised:
                with check_lock.hold(".", command=check_lock.CHECK_FIX):
                    pass
        finally:
            deploy.release_project_lock()
        message = str(raised.exception)
        self.assertIn("'composer up (./start.sh)'", message)
        self.assertIn("this 'check --fix' was refused and nothing was changed", message)
        with check_lock.hold(".", command=check_lock.CHECK_FIX):
            pass

    def test_a_second_deploy_is_refused_naming_the_first(self):
        first = DockerComposeLauncher()
        first.update_images = True
        first.take_project_lock()
        self.addCleanup(first.release_project_lock)
        second = DockerComposeLauncher()
        with patch("builtins.print") as printed, self.assertRaises(SystemExit) as exited:
            second.take_project_lock()
        self.assertEqual(exited.exception.code, 1)
        text = " ".join(str(call.args[0]) for call in printed.call_args_list)
        self.assertIn("'composer update'", text)
        self.assertIn("this 'composer up (./start.sh)' was refused", text)

    def test_a_deploy_waits_for_nothing_while_a_check_fix_runs(self):
        with check_lock.hold(".", command=check_lock.CHECK_FIX):
            with patch("builtins.print"), self.assertRaises(SystemExit):
                DockerComposeLauncher().take_project_lock()

    def test_a_pull_takes_no_lock(self):
        with check_lock.hold("."):
            launcher = DockerComposeLauncher()
            launcher.pull_only_mode = True
            launcher.take_project_lock()

    def test_a_killed_deploy_is_reported_by_the_next(self):
        lock = self.root / check_lock.LOCK_PATH
        lock.parent.mkdir(parents=True)
        lock.write_text(json.dumps({"pid": 999999, "host": "gone", "started": 1764056000,
                                    "command": "composer up (./start.sh)"}))
        launcher = DockerComposeLauncher()
        launcher.take_project_lock()
        launcher.release_project_lock()
        self.assertIn("'composer up (./start.sh)'", launcher.deploy_warnings[0])
        self.assertIn("stopped before it finished", launcher.deploy_warnings[0])

    def test_the_refusal_names_the_holders_container(self):
        lock = self.root / check_lock.LOCK_PATH
        with patch("composer.check_lock._container_id", return_value="3f2a9c1b7d4e"):
            with check_lock.hold(".", command=check_lock.CHECK_FIX):
                record = json.loads(lock.read_text())
                with self.assertRaises(check_lock.CheckFixLockError) as raised:
                    with check_lock.hold("."):
                        pass
        self.assertEqual(record["container"], "3f2a9c1b7d4e")
        self.assertIn("docker stop 3f2a9c1b7d4e", str(raised.exception))

    def test_a_read_only_project_still_shares_the_lock(self):
        """The resident pair mounts the project read-only."""
        lock = self.root / check_lock.LOCK_PATH
        lock.parent.mkdir(parents=True)
        lock.write_text("")
        real_open = os.open

        def read_only(path, flags, *args):
            if flags & os.O_RDWR:
                raise OSError(errno.EROFS, "Read-only file system")
            return real_open(path, flags, *args)

        with check_lock.hold(".", command="composer up (./start.sh)"):
            with patch("composer.check_lock.os.open", side_effect=read_only):
                with self.assertRaises(check_lock.CheckFixLockError):
                    with check_lock.hold(".", command="composer update"):
                        pass
        with patch("composer.check_lock.os.open", side_effect=read_only):
            with check_lock.hold(".", command="composer update") as previous:
                self.assertIsNone(previous)
                self.assertEqual(lock.read_text(), "")

    def test_a_read_only_project_with_no_lock_file_yet_runs(self):
        def read_only(path, flags, *args):
            if flags & os.O_RDWR:
                raise OSError(errno.EROFS, "Read-only file system")
            raise FileNotFoundError(errno.ENOENT, "missing")

        with patch("composer.check_lock.os.open", side_effect=read_only), \
                patch("composer.check_lock.Path.mkdir"):
            with check_lock.hold(".") as previous:
                self.assertIsNone(previous)


class ExecutorForwardsTheRefusalTests(unittest.TestCase):
    """R2-078 (a): the panel showed only "executor operation failed."."""

    def test_the_lock_refusal_is_the_failure_detail(self):
        script = (
            "print('checking'); "
            "print('✖ check-fix: Another Composer operation is running for this project (pid 1); this'"
            " ' check --fix was refused and nothing was changed.'); raise SystemExit(1)"
        )
        with patch("sys.stdout", new=io.StringIO()) as log:
            code, detail = executor_ops._run([sys.executable, "-c", script], dict(os.environ))
        self.assertEqual(code, 1)
        self.assertTrue(detail.startswith("Another Composer operation is running for this project"))
        self.assertIn("checking", log.getvalue())

    def test_other_output_is_not_forwarded(self):
        with patch("sys.stdout", new=io.StringIO()):
            code, detail = executor_ops._run(
                [sys.executable, "-c", "print('secret-ish line'); raise SystemExit(3)"], dict(os.environ))
        self.assertEqual((code, detail), (3, ""))


class QuestionTimeoutTests(unittest.TestCase):
    """R2-017 / R2-078 (b): a question whose terminal went away waited for good, holding the lock."""

    def _silent_pipe(self):
        read, write = os.pipe()
        self.addCleanup(os.close, write)
        stream = os.fdopen(read)
        self.addCleanup(stream.close)
        return stream

    def test_no_answer_in_time_is_a_skip(self):
        stdin = self._silent_pipe()
        out = io.StringIO()
        with patch.dict(os.environ, {guidance.QUESTION_TIMEOUT_ENV: "1"}), patch("sys.stdout", new=io.StringIO()):
            choice = guidance.ask(
                "Q?", [("1", "a"), ("2", "b")], "2",
                reader=lambda prompt: guidance.timed_input(prompt, stdin=stdin), stream=out,
            )
        self.assertIsNone(choice)
        self.assertIn("No answer in 1s: skipped, nothing changed.", out.getvalue())

    def test_the_default_reader_times_out(self):
        stdin = self._silent_pipe()
        with patch("sys.stdin", new=stdin), patch("sys.stdout", new=io.StringIO()), \
                patch.dict(os.environ, {guidance.QUESTION_TIMEOUT_ENV: "1"}):
            self.assertIsNone(guidance.ask("Q?", [("1", "a")], "1", reader=input, stream=io.StringIO()))
            self.assertIsNone(guidance.ask_text("Value: ", lambda value: "", stream=io.StringIO()))

    def test_an_answer_arrives(self):
        read, write = os.pipe()
        os.write(write, b"2\n")
        os.close(write)
        with os.fdopen(read) as stdin, patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(guidance.timed_input("? ", stdin=stdin, timeout=5), "2")

    def test_an_unanswered_confirmation_is_not_confirmed(self):
        stdin = self._silent_pipe()
        with patch("sys.stdin", new=stdin), patch("sys.stdout", new=io.StringIO()), \
                patch.dict(os.environ, {guidance.QUESTION_TIMEOUT_ENV: "1"}):
            self.assertFalse(confirmation.confirm("Apply?", interactive=True, stream=io.StringIO()))

    def test_the_timeout_is_configurable(self):
        with patch.dict(os.environ, {guidance.QUESTION_TIMEOUT_ENV: ""}):
            self.assertEqual(guidance.question_timeout(), 600.0)
        with patch.dict(os.environ, {guidance.QUESTION_TIMEOUT_ENV: "30"}):
            self.assertEqual(guidance.question_timeout(), 30.0)


class YesNoAnswersTests(unittest.TestCase):
    """R2-127: the Redis question answered "Answer y, n." to `yes`."""

    def test_yes_and_no_answer_the_redis_question(self):
        for typed, expected in (("yes", True), ("no", False), ("YES", True)):
            answers = iter([typed])
            self.assertIs(guidance.ask_redis(reader=lambda prompt: next(answers), stream=io.StringIO()), expected)

    def test_skip_is_a_word_too(self):
        self.assertIsNone(guidance.ask("Q?", [("1", "a")], "1", reader=lambda prompt: "skip", stream=io.StringIO()))


if __name__ == "__main__":
    unittest.main()
