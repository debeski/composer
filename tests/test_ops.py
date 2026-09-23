"""Answering DjangoLux's Operations requests.

The request names an operation and nothing else: no command, no path, no flags.
These tests hold that boundary, the token matching that keeps a stale answer
from being read as the current one, and the rule that one request is answered
exactly once — including across a restart of this process.
"""

import json
import uuid
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from composer.ops import MAX_FINDINGS, OpsResponder, _trim, compose_digest
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
             patch.object(ops_module, "compose_digest", return_value="b" * 64):
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
             patch("composer.executor_client.executor_configured", return_value=False), \
             patch.object(ops_module, "compose_digest", return_value="c" * 64):
            self.responder.answer()
        self.assertEqual(applied, [True], "the apply must run check --fix, not a dry run")
        self.assertEqual(self._ack()["error"], "")
        self.assertEqual(self._result()["repairs"][0]["name"], "fix:resident-block")

    def test_the_apply_is_delegated_to_the_executor_when_there_is_one(self):
        # Both resident services mount the project read-only; only the executor
        # can start a container that writes it.
        from composer import ops as ops_module

        self._request("check-fix-apply", compose_digest="d" * 64)
        launcher = SimpleNamespace(
            active_compose_files=["compose.yml"], composer_version="1.6.0",
            compose_file=None, dev_mode=False,
            resolve_active_compose_files=lambda: None,
            collect_checkup=lambda args: ([], []),
        )
        with patch("composer.launcher.DockerComposeLauncher", return_value=launcher), \
             patch.object(ops_module, "compose_digest", return_value="d" * 64), \
             patch("composer.executor_client.executor_configured", return_value=True), \
             patch("composer.executor_client.run_operation", return_value=(0, "")) as delegated:
            self.responder.answer()
        self.assertEqual(delegated.call_args[0][0], "check_fix")
        self.assertEqual(delegated.call_args[0][1], {"compose_digest": "d" * 64})
        self.assertEqual(self._ack()["error"], "")

    def test_a_failing_executor_apply_is_reported_not_swallowed(self):
        from composer import ops as ops_module

        self._request("check-fix-apply", compose_digest="e" * 64)
        launcher = SimpleNamespace(
            active_compose_files=["compose.yml"], composer_version="1.6.0",
            compose_file=None, dev_mode=False,
            resolve_active_compose_files=lambda: None,
            collect_checkup=lambda args: ([], []),
        )
        with patch("composer.launcher.DockerComposeLauncher", return_value=launcher), \
             patch.object(ops_module, "compose_digest", return_value="e" * 64), \
             patch("composer.executor_client.executor_configured", return_value=True), \
             patch("composer.executor_client.run_operation", return_value=(2, "executor said no")):
            self.responder.answer()
        self.assertIn("executor said no", self._ack()["error"])

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
        before = compose_digest(launcher)
        first.write_text("services: {web: {}}\n", encoding="utf-8")
        self.assertNotEqual(before, compose_digest(launcher))


class CheckCarriesTheRepairPreviewTests(unittest.TestCase):
    """One operation answers "what is wrong" and "what would fix it"."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.state = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.runtime = WatchRuntime(_args(self.state / "image-update-request.json"))
        self.responder = OpsResponder(self.runtime)
        (self.state / "ops-request.json").write_text(
            json.dumps({"schema_version": 1, "token": "run-1", "operation": "check"}), encoding="utf-8")

    def _answer(self, repairs_diff="--- a/compose.yml\n+++ b/compose.yml\n+      - run\n"):
        launcher = SimpleNamespace(
            active_compose_files=[], composer_version="1.5.2",
            collect_checkup=lambda args: ([{"level": "fail", "name": "resident-commands", "message": "flat"}], []),
        )
        with patch("composer.launcher.DockerComposeLauncher", return_value=launcher), \
             patch("composer.agent_installer.enable_agent", return_value={"files": ["compose.yml"], "diff": repairs_diff}), \
             patch("composer.agent_installer.enable_executor", return_value={"files": []}), \
             patch("composer.agent_installer.enable_post_start_label", return_value={"files": []}), \
             patch("composer.agent_installer.migrate_dlux_updater", return_value={"files": []}), \
             patch("composer.agent_installer.normalize_restart_labels", return_value={"files": []}):
            self.responder.answer()
        return json.loads((self.state / "ops-result.json").read_text(encoding="utf-8"))

    def test_the_check_returns_findings_and_the_repairs_together(self):
        result = self._answer()
        self.assertEqual(result["findings"][0]["name"], "resident-commands")
        self.assertIn("+      - run", result["repairs"][0]["diff"])
        self.assertEqual(len(result["compose_digest"]), 64, "the apply needs this digest")

    def test_a_clean_stack_offers_no_repairs(self):
        launcher = SimpleNamespace(
            active_compose_files=[], composer_version="1.5.2",
            collect_checkup=lambda args: ([{"level": "ok", "name": "docker", "message": "fine"}], []),
        )
        with patch("composer.launcher.DockerComposeLauncher", return_value=launcher), \
             patch("composer.agent_installer.enable_agent", return_value={"files": []}), \
             patch("composer.agent_installer.enable_executor", return_value={"files": []}), \
             patch("composer.agent_installer.enable_post_start_label", return_value={"files": []}), \
             patch("composer.agent_installer.migrate_dlux_updater", return_value={"files": []}), \
             patch("composer.agent_installer.normalize_restart_labels", return_value={"files": []}):
            self.responder.answer()
        result = json.loads((self.state / "ops-result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["repairs"], [])


class ResidentUpdateTests(unittest.TestCase):
    """Updating the pair ends this process; the helper answers the run."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.state = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.runtime = WatchRuntime(_args(self.state / "image-update-request.json"))
        self.responder = OpsResponder(self.runtime)
        (self.state / "ops-request.json").write_text(
            json.dumps({"schema_version": 1, "token": "run-7", "operation": "agent-update"}), encoding="utf-8")
        # Every update first asks the registry whether there is one; pin that
        # answer so no test here reaches the network for it.
        published = patch("composer.registry.remote_image_version", return_value="99.0.0")
        published.start()
        self.addCleanup(published.stop)

    def test_the_update_is_delegated_and_left_unacked(self):
        with patch("composer.executor_client.executor_configured", return_value=True), \
             patch("composer.executor_client.run_operation", return_value=(0, "")) as delegated:
            self.assertEqual(self.responder.answer(), "run-7")
        self.assertEqual(delegated.call_args[0][0], "agent_update")
        self.assertEqual(delegated.call_args[0][1], {"token": "run-7"})
        # The helper writes the ack after it has recreated this container.
        self.assertFalse((self.state / "ops-request.json.ack").exists())

    def test_the_request_is_taken_off_the_volume_for_the_helper(self):
        # The update recreates this container. Whatever agent comes up next must
        # not find the request still pending and answer it — a 1.5.1 agent did
        # exactly that on the first live run, replying "does not perform the
        # operation" over a run that was proceeding normally.
        with patch("composer.executor_client.executor_configured", return_value=True), \
             patch("composer.executor_client.run_operation", return_value=(0, "")):
            self.responder.answer()
        self.assertFalse((self.state / "ops-request.json").exists())
        restarted = OpsResponder(WatchRuntime(_args(self.state / "image-update-request.json")))
        self.assertIsNone(restarted.pending(), "a fresh agent must find nothing to answer")

    def test_a_refused_update_leaves_the_request_alone(self):
        with patch("composer.executor_client.executor_configured", return_value=False):
            self.responder.answer()
        self.assertTrue((self.state / "ops-request.json").exists())

    def test_it_is_not_started_twice_while_the_helper_runs(self):
        with patch("composer.executor_client.executor_configured", return_value=True), \
             patch("composer.executor_client.run_operation", return_value=(0, "")) as delegated:
            self.responder.answer()
            self.responder.answer()
        self.assertEqual(delegated.call_count, 1)

    def test_a_stack_without_an_executor_is_told_to_use_the_host(self):
        with patch("composer.executor_client.executor_configured", return_value=False):
            self.responder.answer()
        ack = json.loads((self.state / "ops-request.json.ack").read_text(encoding="utf-8"))
        self.assertIn("./start.sh agent update", ack["error"])

    def test_a_delegation_failure_is_reported(self):
        with patch("composer.executor_client.executor_configured", return_value=True), \
             patch("composer.executor_client.run_operation", return_value=(1, "executor is down")):
            self.responder.answer()
        ack = json.loads((self.state / "ops-request.json.ack").read_text(encoding="utf-8"))
        self.assertIn("executor is down", ack["error"])


class ResidentCheckTests(unittest.TestCase):
    """`agent-check` answers "is there one?" without touching the deployment."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.state = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.runtime = WatchRuntime(_args(self.state / "image-update-request.json"))
        self.responder = OpsResponder(self.runtime)
        (self.state / "ops-request.json").write_text(
            json.dumps({"schema_version": 1, "token": "run-9", "operation": "agent-check"}), encoding="utf-8")

    def _answer(self, published, current="1.5.2"):
        # A fresh token each time: one responder answers a token once, so a
        # second question asked under the first would read the first's answer.
        token = f"run-{uuid.uuid4().hex[:8]}"
        (self.state / "ops-request.json").write_text(
            json.dumps({"schema_version": 1, "token": token, "operation": "agent-check"}), encoding="utf-8")
        with patch("composer.registry.remote_image_version", return_value=published), \
             patch("composer.version.read_composer_version", return_value=current):
            self.responder.answer()
        return json.loads((self.state / "ops-result.json").read_text(encoding="utf-8"))

    def test_a_newer_published_version_is_an_available_update(self):
        result = self._answer("1.6.0")
        self.assertTrue(result["resident"]["update_available"])
        self.assertEqual(result["resident"]["published_version"], "1.6.0")
        self.assertEqual(result["exit_code"], 0, "a pending update is news, not a failure")

    def test_the_same_version_is_not_an_update(self):
        self.assertFalse(self._answer("1.5.2")["resident"]["update_available"])

    def test_an_older_published_version_is_not_an_update(self):
        # A pin or a channel switch can leave the resident pair AHEAD of the tag.
        self.assertFalse(self._answer("1.5.1")["resident"]["update_available"])

    def test_prereleases_order_by_pep440_not_by_string(self):
        self.assertFalse(self._answer("1.5.2b1")["resident"]["update_available"])
        self.assertTrue(self._answer("1.5.3b1")["resident"]["update_available"])

    def test_a_pair_ahead_of_its_channel_is_not_called_current(self):
        # A pin, or a channel switched after an update, leaves the resident pair
        # ahead of the tag; calling that "the channel's current version" is false.
        message = self._answer("1.5.1")["findings"][0]["message"]
        self.assertIn("channel publishes 1.5.1", message)
        self.assertNotIn("current version", message)

    def test_a_registry_it_cannot_read_is_unknown_not_latest(self):
        resident = self._answer(None)["resident"]
        self.assertFalse(resident["checked"])
        self.assertFalse(resident["update_available"], "unknown must never be offered as an update")

    def test_nothing_is_run_against_the_deployment(self):
        with patch("composer.executor_client.run_operation") as delegated, \
             patch("composer.registry.remote_image_version", return_value="1.6.0"):
            self.responder.answer()
        delegated.assert_not_called()


class ResidentUpToDateTests(unittest.TestCase):
    """An update with nothing to update is refused before the executor runs."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.state = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.responder = OpsResponder(WatchRuntime(_args(self.state / "image-update-request.json")))
        (self.state / "ops-request.json").write_text(
            json.dumps({"schema_version": 1, "token": "run-8", "operation": "agent-update"}), encoding="utf-8")

    def test_an_up_to_date_pair_is_not_recreated(self):
        with patch("composer.version.read_composer_version", return_value="1.5.2"), \
             patch("composer.registry.remote_image_version", return_value="1.5.2"), \
             patch("composer.executor_client.executor_configured", return_value=True), \
             patch("composer.executor_client.run_operation") as delegated:
            self.responder.answer()
        delegated.assert_not_called()
        ack = json.loads((self.state / "ops-request.json.ack").read_text(encoding="utf-8"))
        self.assertIn("Nothing to update", ack["error"])

    def test_an_unreadable_registry_does_not_block_the_update(self):
        # Refusing here would strand a deployment whose registry is unreachable
        # but whose pair genuinely needs replacing.
        with patch("composer.registry.remote_image_version", return_value=None), \
             patch("composer.executor_client.executor_configured", return_value=True), \
             patch("composer.executor_client.run_operation", return_value=(0, "")) as delegated:
            self.responder.answer()
        delegated.assert_called_once()


class HelperScriptTests(unittest.TestCase):
    """The helper's script is source for a CHILD process, not for this file.

    Written as a plain string, `"\\n"` became a real newline inside a Python
    string literal, and the helper died with a SyntaxError *after* updating the
    pair — so the run it was supposed to answer hung until its timeout.
    """

    def test_escapes_survive_into_the_child(self):
        from composer.executor_ops import _AGENT_UPDATE_SCRIPT as script

        self.assertIn("\\n", script, "the newline escape must reach the child verbatim")
        self.assertNotIn('+ "\n', script, "a raw newline here breaks the child's string literal")

    def test_the_script_is_valid_python_for_the_child(self):
        import ast

        from composer.executor_ops import _AGENT_UPDATE_SCRIPT as script

        body = script.split("<<'PY" + "EOF'", 1)[1].rsplit("PY" + "EOF", 1)[0]
        ast.parse(body)

    def test_the_child_writes_both_documents_under_the_token(self):
        from composer.executor_ops import _AGENT_UPDATE_SCRIPT as script

        self.assertIn("ops-result.json", script)
        self.assertIn("ops-request.json.ack", script)
        self.assertIn("os.replace", script)


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
