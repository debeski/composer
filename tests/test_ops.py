"""Answering DjangoLux's Operations requests.

The request names an operation and nothing else: no command, no path, no flags.
These tests hold that boundary, the token matching that keeps a stale answer
from being read as the current one, and the rule that one request is answered
exactly once — including across a restart of this process.
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

from composer.ops import MAX_FINDINGS, OpsResponder, _compose_digest, _trim
from composer.watcher import WatchRuntime


def _args(trigger, **overrides):
    base = dict(
        trigger_file=str(trigger), package_trigger_file=None, status_file=None,
        log_file=None, interval=2, dev=False, file=None, check_image=[],
        availability_file=None, check_interval=900,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class OpsResponderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.state = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.runtime = WatchRuntime(_args(self.state / "image-update-request.json"))
        self.responder = OpsResponder(self.runtime)

    def _request(self, operation="check", token="run-1"):
        (self.state / "ops-request.json").write_text(
            json.dumps({"schema_version": 1, "token": token, "operation": operation}),
            encoding="utf-8",
        )

    def _result(self):
        return json.loads((self.state / "ops-result.json").read_text(encoding="utf-8"))

    def _ack(self):
        return json.loads((self.state / "ops-request.json.ack").read_text(encoding="utf-8"))

    def _findings(self, findings, exit_code=0):
        return patch.dict(
            "composer.ops.HANDLERS",
            {"check": lambda runtime, request: {
                "exit_code": exit_code, "composer_version": "1.5.0", "findings": findings,
            }},
        )

    def test_a_request_is_answered_with_a_token_matched_result(self):
        self._request()
        with self._findings([{"level": "ok", "name": "docker", "message": "reachable"}]):
            self.assertEqual(self.responder.answer(), "run-1")
        self.assertEqual(self._result()["token"], "run-1")
        self.assertEqual(self._result()["findings"][0]["name"], "docker")
        self.assertEqual(self._ack()["token"], "run-1")
        self.assertEqual(self._ack()["error"], "")

    def test_a_check_that_finds_problems_still_answers(self):
        self._request()
        with self._findings([{"level": "fail", "name": "topology", "message": "none"}], exit_code=1):
            self.responder.answer()
        self.assertEqual(self._ack()["exit_code"], 1)
        self.assertEqual(self._ack()["error"], "", "a finding is not an operation failure")

    def test_the_same_request_is_not_answered_twice(self):
        self._request()
        calls = []
        with patch.dict("composer.ops.HANDLERS", {"check": lambda runtime, request: (calls.append(1), {"findings": []})[1]}):
            self.responder.answer()
            self.responder.answer()
        self.assertEqual(len(calls), 1)

    def test_an_answered_request_is_not_repeated_after_a_restart(self):
        self._request()
        with self._findings([]):
            self.responder.answer()
        restarted = OpsResponder(WatchRuntime(_args(self.state / "image-update-request.json")))
        self.assertIsNone(restarted.pending())

    def test_an_unknown_operation_is_refused_not_attempted(self):
        self._request(operation="restart-everything")
        self.assertEqual(self.responder.answer(), "run-1")
        self.assertIn("does not perform the operation", self._ack()["error"])
        self.assertEqual(self._result()["findings"], [])

    def test_a_handler_that_raises_becomes_an_error_not_a_crash(self):
        self._request()

        def boom(runtime, request):
            raise RuntimeError("docker exploded")

        with patch.dict("composer.ops.HANDLERS", {"check": boom}):
            self.assertEqual(self.responder.answer(), "run-1")
        self.assertIn("docker exploded", self._ack()["error"])

    def test_a_handler_that_returns_nothing_usable_is_reported(self):
        self._request()
        with patch.dict("composer.ops.HANDLERS", {"check": lambda runtime, request: None}):
            self.responder.answer()
        self.assertIn("no usable result", self._ack()["error"])

    def test_no_request_is_a_no_op(self):
        self.assertIsNone(self.responder.answer())
        self.assertFalse((self.state / "ops-result.json").exists())

    def test_the_watch_loop_never_dies_on_an_operation(self):
        self._request()
        with patch("composer.ops.OpsResponder.answer", side_effect=RuntimeError("boom")):
            self.runtime.maybe_answer_ops_request()  # must not raise

    def test_the_request_carries_no_command_surface(self):
        # Everything except the operation name is ignored: a request cannot
        # smuggle a command, a path or a flag into what runs.
        (self.state / "ops-request.json").write_text(json.dumps({
            "token": "run-9", "operation": "check",
            "command": ["rm", "-rf", "/"], "args": ["--fix"], "service": "db",
        }), encoding="utf-8")
        seen = []
        with patch.dict("composer.ops.HANDLERS", {"check": lambda runtime, request: (seen.append(runtime), {"findings": []})[1]}):
            self.responder.answer()
        self.assertEqual(len(seen), 1)
        self.assertEqual(self._result()["operation"], "check")
        self.assertNotIn("command", self._result())


class FixPreviewAndApplyTests(unittest.TestCase):
    """The repair an operator confirms must be the repair that is written."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.state = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.runtime = WatchRuntime(_args(self.state / "image-update-request.json"))
        self.responder = OpsResponder(self.runtime)

    def _request(self, operation, token="run-1", **extra):
        payload = {"schema_version": 1, "token": token, "operation": operation}
        payload.update(extra)
        (self.state / "ops-request.json").write_text(json.dumps(payload), encoding="utf-8")

    def _ack(self):
        return json.loads((self.state / "ops-request.json.ack").read_text(encoding="utf-8"))

    def _result(self):
        return json.loads((self.state / "ops-result.json").read_text(encoding="utf-8"))

    def test_an_apply_without_a_preview_digest_is_refused(self):
        self._request("check-fix-apply")
        self.responder.answer()
        self.assertIn("previewed", self._ack()["error"])

    def test_an_apply_with_a_malformed_digest_is_refused(self):
        self._request("check-fix-apply", compose_digest="../../etc/passwd")
        self.responder.answer()
        self.assertIn("previewed", self._ack()["error"])

    def test_an_apply_whose_files_changed_since_the_preview_is_refused(self):
        from composer import ops as ops_module

        self._request("check-fix-apply", compose_digest="a" * 64)
        launcher = SimpleNamespace(
            active_compose_files=["compose.yml"], composer_version="1.6.0",
            compose_file=None, dev_mode=False,
            resolve_active_compose_files=lambda: None,
            collect_checkup=lambda args: ([], []),
        )
        with patch("composer.launcher.DockerComposeLauncher", return_value=launcher), \
             patch.object(ops_module, "_compose_digest", return_value="b" * 64):
            self.responder.answer()
        self.assertIn("changed since the preview", self._ack()["error"])
        self.assertEqual(self._result()["findings"], [], "nothing was applied")

    def test_a_matching_digest_lets_the_repair_run(self):
        from composer import ops as ops_module

        self._request("check-fix-apply", compose_digest="c" * 64)
        applied = []
        launcher = SimpleNamespace(
            active_compose_files=["compose.yml"], composer_version="1.6.0",
            compose_file=None, dev_mode=False,
            resolve_active_compose_files=lambda: None,
            collect_checkup=lambda args: (applied.append(args.fix) or ([], [{"name": "fix:resident-block", "message": "done"}])),
        )
        with patch("composer.launcher.DockerComposeLauncher", return_value=launcher), \
             patch.object(ops_module, "_compose_digest", return_value="c" * 64):
            self.responder.answer()
        self.assertEqual(applied, [True], "the apply must run check --fix, not a dry run")
        self.assertEqual(self._ack()["error"], "")
        self.assertEqual(self._result()["repairs"][0]["name"], "fix:resident-block")

    def test_a_preview_writes_nothing_and_carries_a_digest(self):
        self._request("check-fix-preview")
        launcher = SimpleNamespace(
            active_compose_files=[], composer_version="1.6.0",
            collect_checkup=lambda args: ([{"level": "fail", "name": "resident-commands", "message": "flat"}], []),
        )
        with patch("composer.launcher.DockerComposeLauncher", return_value=launcher), \
             patch("composer.agent_installer.enable_agent", return_value={"files": []}), \
             patch("composer.agent_installer.enable_executor", return_value={
                 "files": ["compose.yml"], "diff": "--- a/compose.yml\n+++ b/compose.yml\n+      - run\n"}), \
             patch("composer.agent_installer.enable_post_start_label", return_value={"files": []}), \
             patch("composer.agent_installer.migrate_dlux_updater", return_value={"files": []}), \
             patch("composer.agent_installer.normalize_restart_labels", return_value={"files": []}):
            self.responder.answer()
        result = self._result()
        self.assertEqual(len(result["repairs"]), 1, "the same repair must not be listed twice")
        self.assertIn("+      - run", result["repairs"][0]["diff"])
        self.assertEqual(len(result["compose_digest"]), 64)

    def test_a_transform_that_refuses_is_reported_not_raised(self):
        self._request("check-fix-preview")
        launcher = SimpleNamespace(
            active_compose_files=[], composer_version="1.6.0",
            collect_checkup=lambda args: ([], []),
        )
        with patch("composer.launcher.DockerComposeLauncher", return_value=launcher), \
             patch("composer.agent_installer.enable_agent", side_effect=RuntimeError("mixed topology")), \
             patch("composer.agent_installer.enable_executor", return_value={"files": []}), \
             patch("composer.agent_installer.enable_post_start_label", return_value={"files": []}), \
             patch("composer.agent_installer.migrate_dlux_updater", return_value={"files": []}), \
             patch("composer.agent_installer.normalize_restart_labels", return_value={"files": []}):
            self.responder.answer()
        self.assertEqual(self._ack()["error"], "")
        self.assertIn("mixed topology", self._result()["repairs"][0]["note"])

    def test_the_digest_follows_the_files(self):
        first = self.state / "compose.yml"
        first.write_text("services: {}\n", encoding="utf-8")
        launcher = SimpleNamespace(active_compose_files=[str(first)])
        before = _compose_digest(launcher)
        first.write_text("services: {web: {}}\n", encoding="utf-8")
        self.assertNotEqual(before, _compose_digest(launcher))


class ResultTrimmingTests(unittest.TestCase):
    def test_findings_are_bounded(self):
        trimmed = _trim([{"level": "ok", "name": f"n{i}", "message": "m"} for i in range(MAX_FINDINGS + 50)])
        self.assertEqual(len(trimmed), MAX_FINDINGS)

    def test_a_long_message_is_cut(self):
        trimmed = _trim([{"level": "ok", "name": "x", "message": "m" * 5000}])
        self.assertLessEqual(len(trimmed[0]["message"]), 2000)

    def test_non_dict_entries_are_dropped(self):
        self.assertEqual(_trim(["nope", None, 3]), [])

    def test_a_secret_in_a_message_is_redacted(self):
        trimmed = _trim([{"level": "fail", "name": "secrets", "message": "POSTGRES_PASSWORD=hunter2 leaked"}])
        self.assertNotIn("hunter2", trimmed[0]["message"])


if __name__ == "__main__":
    unittest.main()
