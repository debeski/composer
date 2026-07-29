import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from composer.agent import ComposerAgent
from composer.agent_protocol import validate_command
from composer.executor import _build_watch_runtime, _run_watch_loop


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


def _write_ack(trigger_file, token, exit_code=0, operation_id=""):
    payload = {"token": token, "exit_code": exit_code, "finished_at": "now"}
    if operation_id:
        payload["operation_id"] = operation_id
    Path(f"{trigger_file}.ack").write_text(json.dumps(payload), encoding="utf-8")


class ExecutorWatchBuildTests(unittest.TestCase):
    def test_no_trigger_file_means_no_watch_loop(self):
        self.assertIsNone(_build_watch_runtime(SimpleNamespace(trigger_file=None)))

    def test_watch_runtime_has_availability_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(
                trigger_file=str(Path(tmp) / "trigger.json"),
                status_file=None,
                log_file=None,
                interval=2,
                dev=False,
                file=None,
            )
            watch = _build_watch_runtime(args)
            self.assertIsNotNone(watch)
            self.assertFalse(watch.availability_enabled)  # reads stay with the agent


class ExecutorWatchLoopTests(unittest.TestCase):
    def test_loop_processes_a_trigger_under_the_lease_and_writes_ack(self):
        with tempfile.TemporaryDirectory() as tmp:
            trigger = Path(tmp) / "image-update-request.json"
            args = SimpleNamespace(
                trigger_file=str(trigger),
                status_file=str(Path(tmp) / "deploy-status.json"),
                log_file=None,
                interval=2,
                dev=False,
                file=None,
            )
            watch = _build_watch_runtime(args)
            lease = threading.Lock()
            stop = threading.Event()

            # The update child is Docker work; stub it so the loop is testable.
            with patch(
                "composer.watcher.subprocess.run",
                return_value=SimpleNamespace(returncode=0),
            ) as child:
                t = threading.Thread(target=_run_watch_loop, args=(watch, lease, stop), daemon=True)
                t.start()
                trigger.write_text(json.dumps({"token": "tok-1", "operation_id": ""}), encoding="utf-8")
                ack = Path(f"{trigger}.ack")
                for _ in range(100):
                    if ack.exists():
                        break
                    time.sleep(0.02)
                stop.set()
                t.join(2)

            self.assertTrue(ack.exists())
            self.assertEqual(json.loads(ack.read_text())["token"], "tok-1")
            child.assert_called()  # the update pipeline was invoked by the executor


class AgentObserveAckTests(unittest.TestCase):
    def _command(self, op_id):
        return validate_command(
            {
                "schema_version": 1,
                "operation_id": op_id,
                "action": "dlux.image_update",
                "created_at": "2026-07-23T10:00:00+00:00",
                "deadline_at": "2099-07-23T10:00:00+00:00",
                "actor": {"id": "7", "display": "Admin"},
                "payload": {"backup_mode": "data"},
            }
        )

    def test_first_sight_seeds_marker_without_reporting(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = ComposerAgent(_agent_args(Path(tmp)))
            _write_ack(agent.args.trigger_file, "tok-1", exit_code=0)
            agent._observe_executor_update()
            self.assertEqual(agent.store.get_meta("last_reported_ack_token"), "tok-1")
            local = [i for i in agent.store.pending_outbox() if i["kind"] == "local_operation"]
            self.assertEqual(local, [])  # pre-existing ack is not re-reported

    def test_new_local_update_is_reported_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = ComposerAgent(_agent_args(Path(tmp)))
            agent.store.set_meta("last_reported_ack_token", "tok-0")  # already seeded
            _write_ack(agent.args.trigger_file, "tok-1", exit_code=0)
            agent._observe_executor_update()
            agent._observe_executor_update()  # dedup: second call is a no-op
            local = [i for i in agent.store.pending_outbox() if i["kind"] == "local_operation"]
            self.assertEqual(len(local), 1)
            self.assertEqual(local[0]["body"]["request_token"], "tok-1")
            self.assertEqual(local[0]["body"]["state"], "succeeded")
            self.assertEqual(agent.store.get_meta("last_reported_ack_token"), "tok-1")

    def test_failed_local_update_reports_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = ComposerAgent(_agent_args(Path(tmp)))
            agent.store.set_meta("last_reported_ack_token", "tok-0")
            _write_ack(agent.args.trigger_file, "tok-2", exit_code=1)
            agent._observe_executor_update()
            local = [i for i in agent.store.pending_outbox() if i["kind"] == "local_operation"]
            self.assertEqual(local[0]["body"]["state"], "failed")

    def test_command_ack_transitions_command_not_local_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = ComposerAgent(_agent_args(Path(tmp)))
            op_id = str(uuid.uuid4())
            agent.store.enqueue_command(self._command(op_id))
            agent.store.transition(op_id, "running")
            agent.store.set_meta("last_reported_ack_token", "tok-0")
            _write_ack(agent.args.trigger_file, "tok-3", exit_code=0, operation_id=op_id)
            agent._observe_executor_update()
            # A command update transitions the command; it does NOT queue a
            # separate local_operation.
            local = [i for i in agent.store.pending_outbox() if i["kind"] == "local_operation"]
            self.assertEqual(local, [])
            self.assertEqual(agent.store.command_state(op_id), "running")
            events = [i for i in agent.store.pending_outbox() if i.get("operation_id") == op_id]
            self.assertTrue(
                any(e["body"].get("detail", {}).get("phase") == "awaiting_dlux_finalization" for e in events)
            )

    def test_process_local_update_observes_in_executor_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = ComposerAgent(_agent_args(Path(tmp)))
            with patch.dict(os.environ, {"COMPOSER_EXECUTOR_SOCKET": "/x.sock"}):
                with patch.object(agent, "_observe_executor_update") as observe:
                    with patch.object(agent.watch, "process") as process:
                        agent.process_local_update()
            observe.assert_called_once()
            process.assert_not_called()  # agent never performs the update in exec mode


if __name__ == "__main__":
    unittest.main()
