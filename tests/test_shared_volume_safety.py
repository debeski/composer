"""Files on the shared runtime volume, and the agent's queues, against a hostile neighbour.

The app services can write the runtime volume. A writer that opens a predictable
temp name follows a symlink planted there and clobbers its target; a reader that
reads whatever is there can be fed a symlink or an endless file; and a queue
another party can grow must have a ceiling.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from composer import dlux_channel, dlux_release_source, ops, relay, safe_files, watcher
from composer.agent import ComposerAgent
from composer.agent_store import (
    MAX_EVENTS_PER_OPERATION,
    MAX_FINISHED_COMMANDS,
    MAX_QUEUED_COMMANDS,
    AgentStore,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_agent import agent_args, command  # noqa: E402


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.victim = self.root / "victim.txt"
        self.victim.write_text("precious\n")

    def plant(self, target: Path):
        """Every fixed temp name a writer of ``target`` has ever used, as a symlink."""
        for name in (f".{target.name}.tmp", f".{target.name}.{os.getpid()}.tmp", f".{target.name}.part"):
            (target.parent / name).symlink_to(self.victim)

    def assertNotClobbered(self, target: Path):
        self.assertEqual(self.victim.read_text(), "precious\n")
        self.assertFalse(target.is_symlink())
        self.assertTrue(target.is_file())


class PlantedSymlinkTests(_Tmp):
    def test_agent_bridge_documents(self):
        target = self.root / "agent-status.json"
        self.plant(target)
        ComposerAgent._atomic_json(None, target, {"ok": True})
        self.assertNotClobbered(target)

    def test_watcher_availability(self):
        target = self.root / "image-available.json"
        self.plant(target)
        watcher._write_availability_payload(str(target), {"available": False})
        self.assertNotClobbered(target)

    def test_dlux_package_availability(self):
        from composer import dlux_package_cli

        target = self.root / ".package-available.json"
        self.plant(target)
        dlux_package_cli.write_availability(None, {"available": False}, path=target)
        self.assertNotClobbered(target)

    def test_dlux_package_status_is_replaced_not_followed(self):
        from types import SimpleNamespace

        from composer import dlux_package_cli

        target = self.root / "deploy-status.json"
        target.symlink_to(self.victim)
        dlux_package_cli._publish(str(target), SimpleNamespace(as_dict=lambda: {"status": "completed"}))
        self.assertNotClobbered(target)

    def test_watcher_ack(self):
        target = self.root / "image-update-request.json.ack"
        self.plant(target)
        watcher._write_ack(target, "tok", 0)
        self.assertNotClobbered(target)

    def test_ops_documents(self):
        target = self.root / "ops-result.json"
        self.plant(target)
        ops._atomic_json(target, {"ok": True})
        self.assertNotClobbered(target)

    def test_channel_request(self):
        target = self.root / "channel-request.json"
        self.plant(target)
        dlux_channel._atomic_json(target, {"ok": True})
        self.assertNotClobbered(target)

    def test_relay_documents(self):
        target = self.root / "stats.json"
        self.plant(target)
        relay._atomic_json(target, {"ok": True})
        self.assertNotClobbered(target)

    def test_downloaded_wheel(self):
        payload = b"wheel bytes"
        import hashlib
        candidate = dlux_release_source.ReleaseCandidate(
            "1.8.0", "django_lux-1.8.0-py3-none-any.whl", "https://files.pythonhosted.org/x.whl",
            hashlib.sha256(payload).hexdigest(),
        )
        target = self.root / candidate.filename
        self.plant(target)

        class _Response:
            headers = {}

            def __init__(self):
                self._data = payload

            def geturl(self):
                return candidate.url

            def read(self, _n):
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        dlux_release_source.download_wheel(candidate, target, opener=lambda *_a, **_k: _Response())
        self.assertNotClobbered(target)
        self.assertEqual(target.read_bytes(), payload)

    def test_the_mode_is_the_shared_contract(self):
        target = self.root / "doc.json"
        safe_files.atomic_write_json(target, {})
        self.assertEqual(target.stat().st_mode & 0o777, 0o664)
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.endswith(".tmp")], [])


class BoundedReadTests(_Tmp):
    def test_a_symlink_is_refused(self):
        link = self.root / "result.json"
        link.symlink_to(self.victim)
        with self.assertRaises(OSError):
            safe_files.read_bounded(link)

    def test_an_oversized_file_is_refused(self):
        big = self.root / "big.json"
        big.write_bytes(b"x" * 100)
        with self.assertRaises(OSError):
            safe_files.read_bounded(big, limit=99)
        self.assertEqual(len(safe_files.read_bounded(big, limit=100)), 100)

    def test_a_fifo_is_refused_without_blocking(self):
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        with self.assertRaises(OSError):
            safe_files.read_bounded(fifo)

    def test_the_tail_is_bounded(self):
        log = self.root / "log"
        log.write_bytes(b"a" * 1000 + b"END")
        self.assertEqual(safe_files.read_tail(log, 3), b"END")


class AgentBridgeReadTests(_Tmp):
    def test_a_bridge_result_symlink_is_not_followed(self):
        agent = ComposerAgent(agent_args(self.root))
        cmd = command()
        agent.store.enqueue_command(cmd)
        agent.store.transition(cmd["operation_id"], "running")
        secret = self.root / "outside.json"
        secret.write_text(json.dumps({"operation_id": cmd["operation_id"], "status": "completed"}))
        (agent.bridge_results / f"{cmd['operation_id']}.json").symlink_to(secret)
        agent.process_bridge_results()
        self.assertEqual(agent.store.command_state(cmd["operation_id"]), "running")

    def test_the_event_log_is_a_bounded_tail(self):
        from composer.agent_protocol import MAX_EVENT_TEXT
        args = agent_args(self.root)
        Path(args.log_file).write_text("x" * (MAX_EVENT_TEXT * 4) + "LAST LINE")
        agent = ComposerAgent(args)
        cmd = command("composer.restart", {"service": ""})
        agent.store.enqueue_command(cmd)
        with patch.object(agent, "_run_child", return_value=(0, "")):
            agent.execute_received_command()
            agent.execute_received_command()
        events = [item["body"] for item in agent.store.pending_outbox(100) if item["kind"] == "event"]
        log = events[-1]["detail"]["log"]
        self.assertTrue(log.endswith("LAST LINE"))
        self.assertLessEqual(len(log), MAX_EVENT_TEXT + 64)


class QueueBoundTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = AgentStore(self._tmp.name)

    def _events(self, operation_id):
        return [
            item["body"] for item in self.store.pending_outbox(10_000)
            if item["kind"] == "event" and item["operation_id"] == operation_id
        ]

    def test_a_full_command_queue_fails_the_excess_with_a_reason(self):
        for _ in range(MAX_QUEUED_COMMANDS):
            self.assertTrue(self.store.enqueue_command(command()))
        extra = command()
        self.assertTrue(self.store.enqueue_command(extra))
        self.assertEqual(self.store.command_state(extra["operation_id"]), "failed")
        self.assertIn("commands queued", self._events(extra["operation_id"])[-1]["detail"]["error"])
        self.assertEqual(len([c for c in self.store.running_commands()]), 0)

    def test_progress_events_per_operation_are_capped_and_gapless(self):
        cmd = command()
        self.store.enqueue_command(cmd)
        operation_id = cmd["operation_id"]
        for index in range(MAX_EVENTS_PER_OPERATION * 3):
            self.store.transition(operation_id, "running", {"n": index})
        self.store.transition(operation_id, "succeeded")
        events = self._events(operation_id)
        self.assertEqual(len(events), MAX_EVENTS_PER_OPERATION)
        self.assertEqual([e["sequence"] for e in events], list(range(1, MAX_EVENTS_PER_OPERATION + 1)))
        self.assertEqual(events[-1]["state"], "succeeded")

    def test_nothing_follows_a_terminal_event(self):
        cmd = command()
        self.store.enqueue_command(cmd)
        self.store.transition(cmd["operation_id"], "failed")
        self.store.transition(cmd["operation_id"], "running")
        self.assertEqual([e["state"] for e in self._events(cmd["operation_id"])], ["failed"])

    def test_old_finished_commands_are_pruned_once_reported(self):
        with patch("composer.agent_store.MAX_FINISHED_COMMANDS", 3):
            ids = []
            for _ in range(6):
                cmd = command()
                self.store.enqueue_command(cmd)
                self.store.transition(cmd["operation_id"], "succeeded")
                self.store.set_meta(f"bridge_result:{cmd['operation_id']}", "x")
                for item in self.store.pending_outbox(100):
                    self.store.acknowledge_outbox(item["id"])
                ids.append(cmd["operation_id"])
            self.store.enqueue_command(command())
        kept = [i for i in ids if self.store.command_state(i)]
        self.assertEqual(kept, ids[-3:])
        self.assertEqual(self.store.get_meta(f"bridge_result:{ids[0]}"), "")
        self.assertGreater(MAX_FINISHED_COMMANDS, MAX_QUEUED_COMMANDS)

    def test_unreported_finished_commands_are_kept(self):
        with patch("composer.agent_store.MAX_FINISHED_COMMANDS", 1):
            ids = []
            for _ in range(3):
                cmd = command()
                self.store.enqueue_command(cmd)
                self.store.transition(cmd["operation_id"], "succeeded")
                ids.append(cmd["operation_id"])
        self.assertTrue(all(self.store.command_state(i) for i in ids))


if __name__ == "__main__":
    unittest.main()
