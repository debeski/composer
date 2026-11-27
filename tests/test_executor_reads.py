"""The agent's Docker reads travel to composer-executor, which returns only what the
agent uses. The agent has no Docker API of its own: a container inspect through a
proxy would hand it every service's environment (DB password, SECRET_KEY)."""

import json
import os
import shutil
import socket
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from composer import executor_client, executor_ops, executor_protocol as proto, ops, watcher
from composer.executor import Executor

CONFIG = json.dumps({"services": {
    "web": {"labels": {"org.dlux.restart": "safe"}, "environment": {"SECRET_KEY": "s3cret"}},
    "db": {"labels": {"org.dlux.restart": "protected"}, "environment": {"POSTGRES_PASSWORD": "pw"}},
}})
PS = json.dumps({"Service": "web", "State": "running", "Health": "healthy", "Status": "Up 2 hours",
                 "Labels": "com.docker.compose.project=p", "Mounts": "/data"})


def _request(op, payload):
    return {
        "protocol_version": proto.EXECUTOR_PROTOCOL_VERSION,
        "operation_id": str(uuid.uuid4()),
        "op": op,
        "payload": payload,
    }


def _runtime(dev=False, file=None):
    return SimpleNamespace(args=SimpleNamespace(dev=dev, file=file))


class FakeCompose:
    def __init__(self, logs="line one\n"):
        self.calls = []
        self.logs = logs
        self.logs_answers = []

    def __call__(self, launcher, args, timeout=None):
        self.calls.append(list(args))
        if args[0] == "config":
            return True, CONFIG, ""
        if args[0] == "ps":
            return True, PS, ""
        if args[0] == "logs":
            return self.logs_answers.pop(0) if self.logs_answers else (True, self.logs, "")
        return False, "", "unexpected"


class ProtocolTests(unittest.TestCase):
    def test_the_reads_are_executor_ops(self):
        self.assertEqual(proto.READ_OPS, {"services_state", "logs_tail", "image_digests"})
        self.assertTrue(proto.READ_OPS <= proto.EXECUTOR_OPS)

    def test_logs_tail_needs_bounded_lines_and_a_service_name(self):
        ok = proto.validate_executor_request(_request("logs_tail", {"service": "web", "lines": 200}))
        self.assertEqual(ok["payload"], {"dev": False, "file": "", "service": "web", "lines": 200})
        for bad in (
            {"service": "web", "lines": 0},
            {"service": "web", "lines": 1001},
            {"service": "web", "lines": True},
            {"service": "web", "lines": "200"},
            {"service": "web; rm -rf /", "lines": 10},
            {"service": "web", "lines": 10, "file": "../compose.yml"},
            {"service": "web", "lines": 10, "follow": True},
        ):
            with self.assertRaises(proto.ProtocolError, msg=bad):
                proto.validate_executor_request(_request("logs_tail", bad))

    def test_image_digests_takes_one_image_reference_only(self):
        ok = proto.validate_executor_request(_request("image_digests", {"image": "registry.example/app:1.2"}))
        self.assertEqual(ok["payload"], {"image": "registry.example/app:1.2"})
        for bad in ({"image": "--all"}, {"image": "a b"}, {"image": ""}, {"image": 3},
                    {"image": "app", "format": "{{json .Config.Env}}"}):
            with self.assertRaises(proto.ProtocolError, msg=bad):
                proto.validate_executor_request(_request("image_digests", bad))

    def test_services_state_takes_only_the_compose_target(self):
        with self.assertRaises(proto.ProtocolError):
            proto.validate_executor_request(_request("services_state", {"service": "web"}))
        ok = proto.validate_executor_request(_request("services_state", {"dev": True, "file": "compose.yml"}))
        self.assertEqual(ok["payload"], {"dev": True, "file": "compose.yml"})


class ExecutorHandlerTests(unittest.TestCase):
    """What leaves the executor: the fields the agent uses, nothing from inspect."""

    def setUp(self):
        self.compose = FakeCompose()
        for p in (
            patch("composer.launcher.DockerComposeLauncher.run_docker_compose", autospec=True, side_effect=self.compose),
            patch("composer.launcher.DockerComposeLauncher.resolve_active_compose_files", autospec=True),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _handle(self, op, payload):
        return executor_ops.default_operation_handler(proto.validate_executor_request(_request(op, payload)))

    def test_services_state_returns_only_state_fields(self):
        result = self._handle("services_state", {})
        self.assertEqual(result["state"], "succeeded")
        for row in result["services"]:
            self.assertEqual(set(row), {"name", "state", "health", "status", "restart_label"})
        self.assertNotIn("s3cret", json.dumps(result))
        self.assertNotIn("pw", json.dumps(result["services"]))

    def test_logs_tail_refuses_a_service_the_deployment_does_not_have(self):
        result = self._handle("logs_tail", {"service": "stranger", "lines": 5})
        self.assertEqual(result["state"], "rejected")
        self.assertNotIn(["logs", "--no-color", "--no-log-prefix", "--tail", "5", "stranger"], self.compose.calls)

    def test_logs_tail_is_redacted_in_the_executor(self):
        self.compose.logs = "booting\nAPI_KEY=supersecret\n"
        result = self._handle("logs_tail", {"service": "web", "lines": 5})
        self.assertEqual(result["state"], "succeeded")
        self.assertIn("booting", result["logs"])
        self.assertNotIn("supersecret", result["logs"])

    def test_logs_tail_is_plain_text(self):
        # O3 (1.11.0b1 beta channel): the Logs view showed composer-executor's raw ESC[2K.
        self.compose.logs = (
            "\x1b[2K \x1b[1m🛡️  COMPOSER\x1b[0m · Orchestrator\r\n"
            "\x1b[2K   [Compose] Container web Started\x1b[14F\x1b[2K\n\x1b]0;title\x07done\n"
        )
        result = self._handle("logs_tail", {"service": "web", "lines": 5})
        self.assertEqual(result["state"], "succeeded")
        self.assertNotIn("\x1b", result["logs"])
        self.assertNotIn("\r", result["logs"])
        self.assertIn("🛡️  COMPOSER · Orchestrator", result["logs"])
        self.assertIn("[Compose] Container web Started", result["logs"])
        self.assertIn("done", result["logs"])

    def test_a_container_swapped_during_the_read_is_read_again_once(self):
        # 1.11.0b1 live proof E3 (N-9): 2 of ~70 reads during an image update failed here.
        self.compose.logs_answers = [
            (False, "", "Error response from daemon: can not get logs from container which is dead "
                        "or marked for removal"),
            (True, "after the swap\n", ""),
        ]
        with patch("time.sleep") as pause:
            result = self._handle("logs_tail", {"service": "web", "lines": 5})
        self.assertEqual(result["state"], "succeeded")
        self.assertIn("after the swap", result["logs"])
        self.assertEqual(sum(1 for call in self.compose.calls if call[0] == "logs"), 2)
        pause.assert_called_once_with(ops.LOGS_RETRY_DELAY)

    def test_any_other_logs_failure_is_not_retried(self):
        self.compose.logs_answers = [(False, "", "Cannot connect to the Docker daemon")] * 2
        with patch("time.sleep") as pause, self.assertRaises(Exception) as caught:
            self._handle("logs_tail", {"service": "web", "lines": 5})
        self.assertIn("Cannot connect", str(caught.exception))
        self.assertEqual(sum(1 for call in self.compose.calls if call[0] == "logs"), 1)
        pause.assert_not_called()

    def test_a_non_ascii_tail_still_fits_one_frame(self):
        # JSON escapes each of these to six bytes: 60 000 of them is ~360 KB.
        self.compose.logs = "\n".join("سطر " * 12 for _ in range(1300))
        result = self._handle("logs_tail", {"service": "web", "lines": 1000})
        self.assertEqual(result["state"], "succeeded")
        self.assertLessEqual(proto.encoded_size(result), proto.MAX_EXECUTOR_MESSAGE_BYTES)
        self.assertTrue(result["logs"], "the newest lines are kept")
        self.assertTrue(ops.redact_text(self.compose.logs).endswith(result["logs"]))

    def test_image_digests_only_for_the_images_the_deployment_checks(self):
        with patch.dict(os.environ, {"WEB_IMAGE": "acme/app:latest"}), \
                patch("composer.watcher._compose_check_images", return_value=[]), \
                patch("composer.watcher._docker_repo_digests", return_value=["acme/app@sha256:" + "a" * 64]) as docker:
            allowed = self._handle("image_digests", {"image": "acme/app:latest"})
            refused = self._handle("image_digests", {"image": "postgres:17"})
        self.assertEqual(allowed["state"], "succeeded")
        self.assertEqual(allowed["repo_digests"], ["acme/app@sha256:" + "a" * 64])
        self.assertEqual(refused["state"], "rejected")
        docker.assert_called_once_with("acme/app:latest")

    def test_a_compose_file_outside_the_project_is_refused(self):
        result = self._handle("services_state", {"file": "other.yml"})
        self.assertEqual(result["state"], "succeeded")  # a sibling file in the project is fine
        with patch("composer.executor_ops._compose_runtime", return_value=None):
            refused = self._handle("logs_tail", {"service": "web", "lines": 5})
        self.assertEqual(refused["state"], "rejected")


class AgentReadsTests(unittest.TestCase):
    """In an executor deployment the agent never runs a Docker read itself."""

    def setUp(self):
        self.docker = patch("composer.launcher.DockerComposeLauncher.run_docker_compose", autospec=True,
                            side_effect=AssertionError("the agent ran docker compose"))
        self.docker.start()
        self.addCleanup(self.docker.stop)
        for p in (
            patch("composer.launcher.DockerComposeLauncher.resolve_active_compose_files", autospec=True),
            patch.dict(os.environ, {"COMPOSER_EXECUTOR_SOCKET": "/run/composer-exec/composer-exec.sock",
                                    "COMPOSER_AGENT_RESTART_SERVICES": "web"}),
        ):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("COMPOSER_IN_EXECUTOR", None)

    def test_the_services_table_comes_from_the_executor(self):
        rows = [{"name": "web", "state": "running", "health": "healthy", "status": "Up", "restart_label": "safe",
                 "Config": {"Env": ["SECRET_KEY=x"]}}]
        with patch("composer.executor_client.read", return_value={"state": "succeeded", "services": rows}) as read:
            table = ops._service_table(_runtime(dev=True, file="compose.yml"))
        read.assert_called_once_with("services_state", {"dev": True, "file": "compose.yml"})
        self.assertEqual(table[0]["name"], "web")
        self.assertTrue(table[0]["can_restart"])
        self.assertNotIn("Config", table[0], "only the known fields are kept")

    def test_logs_come_from_the_executor(self):
        services = {"state": "succeeded", "services": [
            {"name": "web", "state": "running", "health": "", "status": "", "restart_label": "safe"}]}
        logs = {"state": "succeeded", "logs": "hello\nAPI_KEY=supersecret\n"}
        with patch("composer.executor_client.read", side_effect=[services, logs]) as read:
            result = ops._tail_logs(_runtime(), {"service": "web", "lines": 50})
        self.assertEqual(read.call_args.args, ("logs_tail", {"dev": False, "file": "", "service": "web", "lines": 50}))
        self.assertIn("hello", result["logs"])
        self.assertNotIn("supersecret", result["logs"])

    def test_logs_from_an_older_executor_are_plain_text_too(self):
        services = {"state": "succeeded", "services": [
            {"name": "web", "state": "running", "health": "", "status": "", "restart_label": "safe"}]}
        logs = {"state": "succeeded", "logs": "\x1b[2Kone\r\x1b[32mtwo\x1b[0m\n"}
        with patch("composer.executor_client.read", side_effect=[services, logs]):
            result = ops._tail_logs(_runtime(), {"service": "web", "lines": 50})
        self.assertEqual(result["logs"].rstrip("\n"), "one\ntwo")

    def test_the_digest_probe_asks_the_executor_not_docker(self):
        digest = "sha256:" + "b" * 64
        with patch("composer.watcher.subprocess.run", side_effect=AssertionError("the agent ran docker")), \
                patch("composer.executor_client.read",
                      return_value={"state": "succeeded", "repo_digests": [f"acme/app@{digest}"]}) as read:
            self.assertEqual(watcher._local_repo_digest("acme/app:latest"), digest)
        read.assert_called_once_with("image_digests", {"image": "acme/app:latest"})

    def test_an_unanswered_digest_probe_is_unknown_not_a_change(self):
        with patch("composer.watcher.subprocess.run", side_effect=AssertionError("the agent ran docker")), \
                patch("composer.executor_client.read", side_effect=ValueError("busy")):
            self.assertIsNone(watcher._local_repo_digest("acme/app:latest"))

    def test_the_executor_itself_reads_docker_directly(self):
        with patch.dict(os.environ, {"COMPOSER_IN_EXECUTOR": "1"}), \
                patch("composer.executor_client.read", side_effect=AssertionError("asked itself")), \
                patch("composer.watcher.subprocess.run",
                      return_value=SimpleNamespace(returncode=0, stdout='["acme/app@sha256:' + "c" * 64 + '"]')):
            self.assertEqual(watcher._local_repo_digest("acme/app:latest"), "sha256:" + "c" * 64)

    def test_an_executor_without_the_read_says_to_update_the_pair(self):
        rejected = proto.build_result(str(uuid.uuid4()), "rejected", exit_code=2,
                                      detail="Executor: Unsupported executor op: services_state.")
        with patch("composer.executor_client.send_request", return_value=rejected):
            with self.assertRaises(ValueError) as caught:
                executor_client.read("services_state", {"dev": False, "file": ""})
        self.assertIn("Update both Composer resident services", str(caught.exception))


class ReadLaneTests(unittest.TestCase):
    """Reads do not wait on the write lease, and their own concurrency is capped."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.socket_path = os.path.join(self.tmp, "exec.sock")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _serve(self, handler):
        executor = Executor(self.socket_path, handler)
        threading.Thread(target=executor.serve_forever, daemon=True).start()
        self.addCleanup(executor.stop)
        self.assertTrue(executor.wait_until_listening(timeout=5))
        return executor

    def _ask(self, op, payload, timeout=6):
        return executor_client.send_request(op, payload, socket_path=self.socket_path, timeout=timeout)

    def test_a_read_is_answered_while_a_deploy_holds_the_lease(self):
        executor = self._serve(lambda request: proto.build_result(request["operation_id"], "succeeded"))
        with executor.op_lease:
            read = self._ask("services_state", {"dev": False, "file": ""})
            write = self._ask("restart", {"service": "web"})
        self.assertEqual(read["state"], "succeeded")
        self.assertEqual(write["state"], "rejected")
        self.assertIn("another operation", write["detail"])

    def test_a_third_concurrent_read_is_refused(self):
        started = threading.Semaphore(0)
        release = threading.Event()

        def handler(request):
            started.release()
            release.wait(5)
            return proto.build_result(request["operation_id"], "succeeded")

        self._serve(handler)
        results = []
        threads = [threading.Thread(target=lambda: results.append(self._ask("services_state", {})), daemon=True)
                   for _ in range(2)]
        for thread in threads:
            thread.start()
        for _ in threads:
            self.assertTrue(started.acquire(timeout=3))
        third = self._ask("services_state", {})
        release.set()
        for thread in threads:
            thread.join(5)
        self.assertEqual(third["state"], "rejected")
        self.assertIn("busy with other reads", third["detail"])
        self.assertEqual([r["state"] for r in results], ["succeeded", "succeeded"])


if __name__ == "__main__":
    unittest.main()
