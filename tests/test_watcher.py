import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from composer.watcher import run_watch


def watch_args(root):
    return SimpleNamespace(
        trigger_file=str(root / "image-update-request.json"),
        status_file=str(root / "deploy-status.json"),
        log_file=str(root / "deploy-log.txt"),
        interval=2,
        dev=False,
        file=None,
        once=True,
        check_image=[],
        availability_file=None,
        check_interval=3600,
    )


class WatcherTerminalStatusTests(unittest.TestCase):
    def test_failed_child_guarantees_status_ack_and_console_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = watch_args(root)
            token = "request-123"
            Path(args.trigger_file).write_text(json.dumps({"token": token}), encoding="utf-8")
            Path(args.status_file).write_text(
                json.dumps({"status": "preparing", "updated_at": "old"}),
                encoding="utf-8",
            )

            with patch(
                "composer.watcher.subprocess.run",
                return_value=SimpleNamespace(returncode=1),
            ):
                self.assertEqual(run_watch(args), 1)

            status = json.loads(Path(args.status_file).read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["request_token"], token)
            self.assertEqual(status["exit_code"], 1)
            self.assertIn("exited with status 1", status["error"])

            ack = json.loads(Path(f"{args.trigger_file}.ack").read_text(encoding="utf-8"))
            self.assertEqual(ack["token"], token)
            self.assertEqual(ack["exit_code"], 1)
            self.assertIn("Update failed", Path(args.log_file).read_text(encoding="utf-8"))

    def test_failed_child_preserves_its_detailed_terminal_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = watch_args(root)
            token = "request-detail"
            Path(args.trigger_file).write_text(json.dumps({"token": token}), encoding="utf-8")

            def fail_with_status(*_args, **_kwargs):
                Path(args.status_file).write_text(
                    json.dumps({"status": "failed", "error": "Health check failed for web."}),
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=1)

            with patch("composer.watcher.subprocess.run", side_effect=fail_with_status):
                self.assertEqual(run_watch(args), 1)

            status = json.loads(Path(args.status_file).read_text(encoding="utf-8"))
            self.assertEqual(status["error"], "Health check failed for web.")
            self.assertEqual(status["request_token"], token)

    def test_child_launch_error_is_terminalized_instead_of_crashing_watcher(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = watch_args(root)
            Path(args.trigger_file).write_text(
                json.dumps({"token": "request-spawn"}),
                encoding="utf-8",
            )

            with patch(
                "composer.watcher.subprocess.run",
                side_effect=OSError("python executable unavailable"),
            ):
                self.assertEqual(run_watch(args), 127)

            status = json.loads(Path(args.status_file).read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["exit_code"], 127)
            self.assertIn("could not start", status["error"])
