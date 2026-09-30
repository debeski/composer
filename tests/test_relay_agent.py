import json
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from composer.agent import ComposerAgent
from composer.relay import operation_digest
from tests.test_agent import agent_args

PAGE = {
    "name": "finance.rates_page",
    "url": "https://rates.example.com/exchange/",
    "response": {"type": "text", "max_bytes": 4096},
}


class AgentRelayTests(unittest.TestCase):
    def test_the_agent_loop_answers_relay_requests_through_the_responder(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            declared = root / "relay"
            declared.mkdir()
            (declared / "operations.json").write_text(json.dumps({"schema_version": 1, "operations": [PAGE]}))
            (declared / "operations.lock").write_text(json.dumps(
                {"schema_version": 1, "operations": {PAGE["name"]: operation_digest(PAGE)}}))
            with patch.dict(os.environ, {"COMPOSER_RELAY_DIR": str(declared)}):
                agent = ComposerAgent(agent_args(root))
                agent.process_relay()
                agent._relay._perform = lambda op, params, secret=None: {
                    "http_status": 200, "content_type": "text/html", "bytes": 2, "data": "hi"}
                oid = str(uuid.uuid4())
                created = datetime.now(timezone.utc)
                request = agent._relay.requests / f"{oid}.json"
                request.write_text(json.dumps({
                    "schema_version": 1, "operation_id": oid, "op": PAGE["name"], "params": {}, "sealed": None,
                    "created_at": created.isoformat(), "expires_at": (created + timedelta(seconds=30)).isoformat(),
                }))
                agent.process_relay()
            result = json.loads((agent._relay.results / f"{oid}.json").read_text())
            self.assertEqual((result["status"], result["data"]), ("ok", "hi"))
            # The private key lives in the agent's own state directory, not on the shared volume.
            self.assertTrue((root / "state" / "relay-keys.json").exists())
            self.assertFalse(list(agent._relay.root.rglob("relay-keys.json")))

    def test_run_once_calls_process_relay(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            agent = ComposerAgent(agent_args(Path(temp_dir)))
            agent.watch = MagicMock()
            for name in ("process_enroll_request", "process_pending_rotation", "process_local_update",
                         "process_bridge_results", "publish_snapshot"):
                setattr(agent, name, MagicMock())
            agent.process_relay = MagicMock()
            agent.run_once()
            agent.process_relay.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
