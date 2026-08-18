"""The plumbing: a package-update trigger file drives `composer dlux-update`.

Mirrors the image-update trigger contract — a request is processed once, its
token is acked whatever happens, and a re-run of the loop does not repeat it.
Failure to *start* the child must still ack, or a wedged request blocks every
later update.
"""

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from composer.watcher import WatchRuntime


def _args(trigger, **overrides):
    base = dict(
        trigger_file=str(trigger),
        package_trigger_file=None,
        status_file=None,
        log_file=None,
        interval=2,
        dev=False,
        file=None,
        check_image=[],
        availability_file=None,
        check_interval=3600,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _Completed:
    def __init__(self, returncode=0):
        self.returncode = returncode


class PackageTriggerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.state = Path(self._tmp.name)
        self.trigger = self.state / "image-update-request.json"
        self.package = self.state / "package-update-request.json"
        self.addCleanup(self._tmp.cleanup)

    def _runtime(self, **overrides):
        return WatchRuntime(_args(self.trigger, **overrides))

    def _write_request(self, **payload):
        request = {"token": "tok-1", "operation_id": "op-1", "payload": payload}
        self.package.write_text(json.dumps(request), encoding="utf-8")

    def test_the_package_trigger_defaults_beside_the_image_trigger(self):
        runtime = self._runtime()
        self.assertEqual(runtime.package_trigger, self.package)
        self.assertEqual(runtime.package_ack, Path(f"{self.package}.ack"))

    def test_an_explicit_package_trigger_is_honoured(self):
        custom = self.state / "elsewhere.json"
        runtime = self._runtime(package_trigger_file=str(custom))
        self.assertEqual(runtime.package_trigger, custom)

    def test_a_request_is_translated_into_a_dlux_update_child(self):
        self._write_request(mode="apply", target_version="1.8.0", backup_mode="data")
        runtime = self._runtime()
        request = runtime.pending_package_request()
        self.assertIsNotNone(request)

        with patch("composer.watcher.subprocess.run", return_value=_Completed(0)) as run:
            exit_code = runtime.process_package(request)

        argv = run.call_args[0][0]
        self.assertEqual(exit_code, 0)
        self.assertIn("dlux-update", argv)
        self.assertIn("apply", argv)
        self.assertIn("--version", argv)
        self.assertIn("1.8.0", argv)

    def test_a_rollback_request_runs_rollback_without_a_version(self):
        self._write_request(mode="rollback", target_version="", backup_mode="skip")
        runtime = self._runtime()

        with patch("composer.watcher.subprocess.run", return_value=_Completed(0)) as run:
            runtime.process_package(runtime.pending_package_request())

        argv = run.call_args[0][0]
        self.assertIn("rollback", argv)
        self.assertNotIn("--version", argv)

    def test_an_unknown_mode_falls_back_to_apply(self):
        self._write_request(mode="destroy", target_version="", backup_mode="data")
        runtime = self._runtime()

        with patch("composer.watcher.subprocess.run", return_value=_Completed(0)) as run:
            runtime.process_package(runtime.pending_package_request())

        argv = run.call_args[0][0]
        self.assertIn("apply", argv)
        self.assertNotIn("destroy", argv)

    def test_a_processed_token_is_acked_and_not_repeated(self):
        self._write_request(mode="apply", target_version="1.8.0")
        runtime = self._runtime()

        with patch("composer.watcher.subprocess.run", return_value=_Completed(0)):
            runtime.process_package(runtime.pending_package_request())

        ack = json.loads(Path(f"{self.package}.ack").read_text(encoding="utf-8"))
        self.assertEqual(ack["token"], "tok-1")
        self.assertEqual(ack["exit_code"], 0)
        self.assertIsNone(runtime.pending_package_request(), "the same token must not re-run")

    def test_a_failed_child_still_acks_so_the_loop_cannot_wedge(self):
        self._write_request(mode="apply", target_version="1.8.0")
        runtime = self._runtime()

        with patch("composer.watcher.subprocess.run", return_value=_Completed(1)):
            exit_code = runtime.process_package(runtime.pending_package_request())

        self.assertEqual(exit_code, 1)
        ack = json.loads(Path(f"{self.package}.ack").read_text(encoding="utf-8"))
        self.assertEqual(ack["exit_code"], 1)
        self.assertIsNone(runtime.pending_package_request())

    def test_a_child_that_cannot_start_is_acked_too(self):
        self._write_request(mode="apply", target_version="1.8.0")
        runtime = self._runtime()

        with patch("composer.watcher.subprocess.run", side_effect=OSError("no exec")):
            exit_code = runtime.process_package(runtime.pending_package_request())

        self.assertEqual(exit_code, 127)
        self.assertEqual(
            json.loads(Path(f"{self.package}.ack").read_text(encoding="utf-8"))["exit_code"], 127
        )

    def test_the_critical_exit_code_is_reported_distinctly(self):
        """exit 3 means the rollback was also unhealthy — not a routine failure."""
        self._write_request(mode="apply", target_version="1.8.0")
        runtime = self._runtime()

        with patch("composer.watcher.subprocess.run", return_value=_Completed(3)):
            with patch("builtins.print") as printed:
                runtime.process_package(runtime.pending_package_request())

        messages = " ".join(str(call.args[0]) for call in printed.call_args_list if call.args)
        self.assertIn("NEEDS ATTENTION", messages)

    def test_a_prior_ack_is_respected_across_restarts(self):
        Path(f"{self.package}.ack").write_text(
            json.dumps({"token": "tok-1", "exit_code": 0}), encoding="utf-8")
        self._write_request(mode="apply", target_version="1.8.0")

        self.assertIsNone(self._runtime().pending_package_request())

    def test_the_image_trigger_is_untouched_by_a_package_request(self):
        self._write_request(mode="apply", target_version="1.8.0")
        runtime = self._runtime()

        self.assertIsNone(runtime.pending_request(), "an image deploy must not be triggered")


class PackageAckObservationTests(unittest.TestCase):
    """The agent reports completion by reading the ack; it holds no Docker authority."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.state = Path(self._tmp.name)
        self.trigger = self.state / "image-update-request.json"
        self.addCleanup(self._tmp.cleanup)

    def test_package_and_image_acks_are_read_independently(self):
        runtime = WatchRuntime(_args(self.trigger))
        Path(f"{self.trigger}.ack").write_text(
            json.dumps({"token": "image-1", "exit_code": 0}), encoding="utf-8")
        Path(f"{runtime.package_trigger}.ack").write_text(
            json.dumps({"token": "package-1", "exit_code": 3}), encoding="utf-8")

        self.assertEqual(runtime.read_ack()["token"], "image-1")
        self.assertEqual(runtime.read_package_ack()["token"], "package-1")
        self.assertEqual(runtime.read_package_ack()["exit_code"], 3)

    def test_a_missing_package_ack_reads_as_empty(self):
        self.assertEqual(WatchRuntime(_args(self.trigger)).read_package_ack(), {})



class PackageAvailabilityPublicationTests(unittest.TestCase):
    """DjangoLux 1.8.0 no longer polls PyPI; if nobody publishes, its tile is blind."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.state = Path(self._tmp.name)
        self.trigger = self.state / "image-update-request.json"
        self.report = self.state / "package-available.json"
        self.addCleanup(self._tmp.cleanup)

    def _runtime(self, **overrides):
        return WatchRuntime(_args(self.trigger, **overrides))

    def _describe(self, **payload):
        base = {"version": "1.9.0", "inline_safe": True, "reason": "", "manifest": {}}
        base.update(payload)
        return patch("composer.dlux_release_source.describe", return_value=base)

    def test_the_report_lands_where_djangolux_reads_it(self):
        with self._describe():
            self._runtime().maybe_check_package_availability()

        published = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(published["version"], "1.9.0")
        self.assertTrue(published["available"])

    def test_a_failed_check_is_published_rather_than_swallowed(self):
        with patch("composer.dlux_release_source.describe", side_effect=RuntimeError("PyPI down")):
            self._runtime().maybe_check_package_availability()

        published = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertFalse(published["available"])
        self.assertIn("PyPI down", published["error"])

    def test_it_does_not_run_again_before_the_interval(self):
        runtime = self._runtime()
        with self._describe() as described:
            runtime.maybe_check_package_availability()
            runtime.maybe_check_package_availability()

        self.assertEqual(described.call_count, 1)

    def test_force_overrides_the_interval(self):
        runtime = self._runtime()
        with self._describe() as described:
            runtime.maybe_check_package_availability()
            runtime.maybe_check_package_availability(force=True)

        self.assertEqual(described.call_count, 2)

    def test_no_runtime_volume_means_no_publication(self):
        """A stack whose DjangoLux does not use the volume gets no stray file."""
        missing = self.state / "absent" / "image-update-request.json"
        runtime = WatchRuntime(_args(missing))
        with self._describe() as described:
            runtime.maybe_check_package_availability()

        self.assertEqual(described.call_count, 0)

    def test_a_publication_failure_does_not_escape_the_loop(self):
        runtime = self._runtime()
        with self._describe(), patch(
            "composer.dlux_package_cli.write_availability", side_effect=OSError("read-only")
        ):
            runtime.maybe_check_package_availability()  # must not raise


if __name__ == "__main__":
    unittest.main()
