"""Deploy health: break-it round 2 R2-028, R2-031, R2-057, R2-073, R2-086 (d)."""
import os
import signal
import subprocess
import sys
import unittest
from unittest.mock import patch

from composer import health_monitor, launcher as launcher_module
from composer.launcher import DockerComposeLauncher

from tests.test_deploy_safety import FakeClock, HealthHarness, _running


class SlowCrashLoopTests(HealthHarness, unittest.TestCase):
    """R2-031: web exited 25 s after each start; the 10 s settle passed it."""

    def setUp(self):
        super().setUp()
        self.env = patch.dict(os.environ, {health_monitor.SETTLE_ENV: "10", health_monitor.CONFIRM_ENV: "30"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _markers(self, restart_at):
        def markers(poll):
            started = "1970-01-01T00:16:40Z"  # == FakeClock start (1000 s)
            if self.clock.now - 1000 >= restart_at:
                return f"aaa 1 1970-01-01T00:17:05Z\nccc 0 {started}\n"
            return f"aaa 0 {started}\nccc 0 {started}\n"
        return markers

    def _script(self, restart_at):
        state = {"poll": -1}
        polls = [_running("web", "aaa", "healthy"), _running("celery", "ccc")]
        markers = self._markers(restart_at)

        def ps(include_all=False):
            if not include_all:
                state["poll"] += 1
            return True, polls, ""

        def run_command(argv, timeout=None, env=None):
            if argv[:2] == ["docker", "inspect"]:
                return True, markers(state["poll"]), ""
            return False, "", "unexpected"

        self.launcher.get_compose_ps_entries = ps
        self.launcher.run_command = run_command

    def test_the_settle_window_alone_passes_it(self):
        self._script(restart_at=25)
        self.assertEqual(self.launcher.monitor_health(), (True, ""))

    def test_the_confirmation_after_the_post_start_tasks_fails_it(self):
        self._script(restart_at=25)
        self.assertTrue(self.launcher.monitor_health()[0])
        ok, detail = self.launcher.confirm_stable()
        self.assertFalse(ok)
        self.assertIn("web restarted once after the post-start tasks: it is crash-looping", detail)

    def test_a_stable_stack_waits_only_the_rest_of_the_window(self):
        self._script(restart_at=10_000)
        self.assertTrue(self.launcher.monitor_health()[0])
        before = self.clock.now
        self.assertEqual(self.launcher.confirm_stable(), (True, ""))
        self.assertLessEqual(self.clock.now - 1000, 31)
        self.assertGreater(self.clock.now, before)

    def test_without_a_health_check_there_is_nothing_to_confirm(self):
        self.assertEqual(DockerComposeLauncher().confirm_stable(), (True, ""))

    def test_the_window_is_never_shorter_than_the_settle_window(self):
        with patch.dict(os.environ, {health_monitor.SETTLE_ENV: "40", health_monitor.CONFIRM_ENV: "5"}):
            self.assertEqual(health_monitor.confirm_seconds(), 40.0)
        with patch.dict(os.environ, {health_monitor.SETTLE_ENV: "", health_monitor.CONFIRM_ENV: ""}):
            self.assertEqual(health_monitor.confirm_seconds(), 30.0)


class InterruptedRecreateTests(unittest.TestCase):
    """R2-028: a deploy killed mid-recreate left `<hash>_…` containers; the next one failed."""

    def setUp(self):
        self.launcher = DockerComposeLauncher()
        self.launcher.compose_config_json = lambda: {"name": "stack"}
        self.calls = []
        self.rows = (
            "1111 2fff086ef793_stack-smtp-relay-1 exited\n"
            "2222 stack-smtp-relay-1 created\n"
            "3333 9abc086ef793_stack-web-1 running\n"
            "4444 stack-db-1 running\n"
        )

        def run_command(argv, timeout=None, env=None):
            self.calls.append(argv)
            if argv[:3] == ["docker", "ps", "-a"]:
                return True, self.rows, ""
            return True, "", ""

        self.launcher.run_command = run_command

    def test_a_stopped_leftover_is_removed_and_a_running_one_renamed(self):
        with patch("builtins.print") as printed:
            done = self.launcher.clear_interrupted_recreates()
        self.assertIn(["docker", "rm", "1111"], self.calls)
        self.assertIn(["docker", "rename", "3333", "stack-web-1"], self.calls)
        self.assertNotIn(["docker", "rm", "2222"], self.calls)
        self.assertEqual(len(done), 2)
        self.assertIn("A previous deploy stopped halfway", printed.call_args.args[0])

    def test_a_running_leftover_whose_name_is_taken_is_left(self):
        self.rows = "3333 9abc086ef793_stack-web-1 running\n5555 stack-web-1 created\n"
        self.launcher.clear_interrupted_recreates()
        self.assertFalse(any(argv[:2] == ["docker", "rename"] for argv in self.calls))

    def test_a_clean_project_changes_nothing(self):
        self.rows = "4444 stack-db-1 running\n"
        self.assertEqual(self.launcher.clear_interrupted_recreates(), [])
        self.assertEqual(len(self.calls), 1)


class OcrWithoutEngineTests(unittest.TestCase):
    """R2-057: a project without the `ocr` extra failed `./start.sh` health."""

    CONFIG = {"name": "stack", "services": {
        "web": {"image": "demo:1", "command": ["gunicorn"]},
        "ocr": {"image": "demo:1", "command": ["python", "-m", "dlux.ocr.serve"]},
    }}

    def setUp(self):
        self.launcher = DockerComposeLauncher()
        self.launcher.compose_config_json = lambda: self.CONFIG
        self.launcher.services = ["web", "ocr"]
        self.compose = []
        self.launcher.run_docker_compose = lambda args, timeout=None: (self.compose.append(args) or (True, "", ""))
        self.answer = "ocr-engine: missing indra_ocr,onnxruntime\n"
        self.launcher.run_command = lambda argv, timeout=None, env=None: (True, self.answer, "")

    def test_it_is_left_out_stopped_and_named(self):
        self.assertEqual(self.launcher.leave_out_ocr_without_engine(), ["ocr"])
        self.assertIn("ocr", self.launcher.exclude_services)
        self.assertNotIn("ocr", self.launcher.services)
        self.assertIn(["stop", "ocr"], self.compose)
        self.assertIn("has no OCR engine", self.launcher.deploy_warnings[0])

    def test_an_image_with_the_engine_runs_it(self):
        self.answer = "ocr-engine: present\n"
        self.assertEqual(self.launcher.leave_out_ocr_without_engine(), [])
        self.assertEqual(self.compose, [])

    def test_an_image_that_cannot_be_asked_is_left_to_up(self):
        self.launcher.run_command = lambda argv, timeout=None, env=None: (False, "", "No such image")
        self.assertEqual(self.launcher.leave_out_ocr_without_engine(), [])

    def test_the_probe_runs_for_real(self):
        out = subprocess.run([sys.executable, "-I", "-c", launcher_module.OCR_PROBE, "json", "no_such_module_x"],
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(out.strip(), "ocr-engine: missing no_such_module_x")


class SigtermTests(unittest.TestCase):
    """R2-073: composer-agent and composer-executor ignored SIGTERM as PID 1."""

    def test_sigterm_stops_the_loop_like_ctrl_c(self):
        previous = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        state = launcher_module.stop_on_sigterm()
        with self.assertRaises(KeyboardInterrupt):
            os.kill(os.getpid(), signal.SIGTERM)
            signal.pause() if hasattr(signal, "pause") else None
        self.assertTrue(state["signalled"])


class StatusRecordTests(unittest.TestCase):
    """R2-074: a deploy-status.json with a future mtime was merged into the next refusal."""

    def test_a_future_mtime_is_not_this_request(self):
        import time

        from composer.watcher import _describes_request

        old = {"action": "rollback", "operation_id": "", "run_token": "old"}
        now = time.time()
        self.assertFalse(_describes_request(old, "new", "", now + 3600 * 24 * 365, now - 1))
        self.assertTrue(_describes_request({"status": "failed"}, "new", "", now, now - 1))

    def test_another_operations_record_is_not_this_request(self):
        import time

        from composer.watcher import _describes_request

        now = time.time()
        self.assertFalse(_describes_request({"operation_id": "op-old"}, "t", "op-new", now, now - 1))
        self.assertTrue(_describes_request({"operation_id": "op-new"}, "t", "op-new", now - 100, now))
        self.assertTrue(_describes_request({"run_token": "t"}, "t", "op-new", now + 9999, now))


class RegistryHintTests(unittest.TestCase):
    def test_a_refused_pull_names_the_registry_login(self):
        hint = launcher_module.registry_hint("Error response from daemon: pull access denied for x, repository does not exist")
        self.assertIn("COMPOSER_REGISTRY_USERNAME", hint)
        self.assertEqual(launcher_module.registry_hint("network unreachable"), "")


if __name__ == "__main__":
    unittest.main()
