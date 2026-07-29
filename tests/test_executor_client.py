import os
import shutil
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from composer import executor_client
from composer import executor_protocol as proto
from composer.agent import ComposerAgent
from composer.executor import Executor


def _agent_args(root):
    return SimpleNamespace(
        control_url=None,
        enrollment_token=None,
        state_dir=str(root / "state"),
        bridge_dir=str(root / "bridge"),
        trigger_file=str(root / "image-update-request.json"),
        status_file=str(root / "deploy-status.json"),
        log_file=str(root / "deploy-log.txt"),
        interval=2,
        dev=False,
        file=None,
        check_image=[],
        check_interval=3600,
        availability_file=None,
        allow_http_localhost=False,
        once=True,
    )


class _ServedExecutor:
    """Start a real executor server with an injected handler on a temp socket."""

    def __init__(self, test, handler):
        self.tmp = tempfile.mkdtemp()
        test.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.socket_path = os.path.join(self.tmp, "exec.sock")
        self.executor = Executor(self.socket_path, handler)
        threading.Thread(target=self.executor.serve_forever, daemon=True).start()
        test.addCleanup(self.executor.stop)
        for _ in range(100):
            if os.path.exists(self.socket_path):
                break
            time.sleep(0.02)


def _ok_handler(request):
    return proto.build_result(request["operation_id"], "succeeded", exit_code=0, detail="done")


class ExecutorClientTests(unittest.TestCase):
    def test_executor_configured_reads_env(self):
        with patch.dict(os.environ, {"COMPOSER_EXECUTOR_SOCKET": "/x.sock"}, clear=True):
            self.assertTrue(executor_client.executor_configured())
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(executor_client.executor_configured())

    def test_run_operation_success(self):
        served = _ServedExecutor(self, _ok_handler)
        with patch.dict(os.environ, {"COMPOSER_EXECUTOR_SOCKET": served.socket_path}):
            code, detail = executor_client.run_operation(
                "restart", {"service": "web"}, str(uuid.uuid4())
            )
        self.assertEqual(code, 0)
        self.assertEqual(detail, "done")

    def test_run_operation_passes_typed_request(self):
        seen = {}

        def handler(request):
            seen.update(request)
            return _ok_handler(request)

        served = _ServedExecutor(self, handler)
        op_id = str(uuid.uuid4())
        with patch.dict(os.environ, {"COMPOSER_EXECUTOR_SOCKET": served.socket_path}):
            executor_client.run_operation("restart", {"service": "web"}, op_id)
        self.assertEqual(seen["op"], "restart")
        self.assertEqual(seen["operation_id"], op_id)
        self.assertEqual(seen["payload"], {"service": "web"})

    def test_run_operation_rejected_yields_nonzero(self):
        def handler(request):
            return proto.build_result(request["operation_id"], "rejected", exit_code=2, detail="nope")

        served = _ServedExecutor(self, handler)
        with patch.dict(os.environ, {"COMPOSER_EXECUTOR_SOCKET": served.socket_path}):
            code, detail = executor_client.run_operation("restart", {"service": "web"}, str(uuid.uuid4()))
        self.assertEqual(code, 2)
        self.assertIn("nope", detail)

    def test_run_operation_executor_down_is_reported_not_swallowed(self):
        # Point at a socket that does not exist: no Docker fallback in exec mode.
        with patch("composer.executor_client.resolve_socket_path", return_value="/nonexistent/exec.sock"):
            code, detail = executor_client.run_operation("restart", {"service": "web"}, str(uuid.uuid4()))
        self.assertEqual(code, 127)
        self.assertIn("failed", detail.lower())


class AgentDelegationTests(unittest.TestCase):
    def _command(self, action, payload):
        from composer.agent_protocol import validate_command

        return validate_command(
            {
                "schema_version": 1,
                "operation_id": str(uuid.uuid4()),
                "action": action,
                "created_at": "2026-07-23T10:00:00+00:00",
                "deadline_at": "2099-07-23T10:00:00+00:00",
                "actor": {"id": "7", "display": "Admin"},
                "payload": payload,
            }
        )

    def test_restart_delegates_to_executor_when_configured(self):
        seen = {}

        def handler(request):
            seen.update(request)
            return proto.build_result(request["operation_id"], "succeeded", exit_code=0, detail="restarted")

        served = _ServedExecutor(self, handler)
        with tempfile.TemporaryDirectory() as tmp:
            agent = ComposerAgent(_agent_args(Path(tmp)))
            cmd = self._command("composer.restart", {"service": "web"})
            with patch.dict(os.environ, {"COMPOSER_EXECUTOR_SOCKET": served.socket_path}):
                with patch("composer.agent.subprocess.run") as legacy_run:
                    code, detail = agent._run_child(cmd)
        self.assertEqual(code, 0)
        self.assertEqual(seen["op"], "restart")
        legacy_run.assert_not_called()  # no in-process Docker path taken

    def test_recovery_delegates_as_recovery_deploy(self):
        seen = {}

        def handler(request):
            seen.update(request)
            return proto.build_result(request["operation_id"], "succeeded", exit_code=0)

        served = _ServedExecutor(self, handler)
        with tempfile.TemporaryDirectory() as tmp:
            agent = ComposerAgent(_agent_args(Path(tmp)))
            cmd = self._command("composer.recovery_deploy", {"force": True, "reason": "disk full"})
            with patch.dict(os.environ, {"COMPOSER_EXECUTOR_SOCKET": served.socket_path}):
                code, _ = agent._run_child(cmd)
        self.assertEqual(code, 0)
        self.assertEqual(seen["op"], "recovery_deploy")
        self.assertEqual(seen["payload"], {"force": True, "reason": "disk full"})

    def test_legacy_path_used_when_no_executor_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = ComposerAgent(_agent_args(Path(tmp)))
            cmd = self._command("composer.restart", {"service": "web"})
            env_without_socket = {k: v for k, v in os.environ.items() if k != "COMPOSER_EXECUTOR_SOCKET"}
            with patch.dict(os.environ, env_without_socket, clear=True):
                os.environ["COMPOSER_AGENT_RESTART_SERVICES"] = "web"
                with patch(
                    "composer.agent.subprocess.run",
                    return_value=SimpleNamespace(returncode=0),
                ) as legacy_run:
                    code, _ = agent._run_child(cmd)
        self.assertEqual(code, 0)
        legacy_run.assert_called_once()  # in-process legacy path


if __name__ == "__main__":
    unittest.main()
