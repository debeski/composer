import json
import os
import stat
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from composer.agent import ComposerAgent
from composer.agent_protocol import ProtocolError, redact_text, validate_command
from composer.agent_store import AgentStore
from composer.control_client import ControlPlaneClient, ControlPlaneError


def agent_args(root):
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


def command(action="dlux.image_update", payload=None):
    return validate_command({
        "schema_version": 1,
        "operation_id": str(uuid.uuid4()),
        "action": action,
        "created_at": "2026-07-23T10:00:00+00:00",
        "deadline_at": "2099-07-23T10:00:00+00:00",
        "actor": {"id": "7", "display": "Fleet Admin"},
        "payload": payload or {},
    })


class AgentProtocolTests(unittest.TestCase):
    def test_shared_protocol_fixtures(self):
        fixtures = Path(__file__).parent / "fixtures" / "agent-protocol-v1"
        valid = json.loads((fixtures / "command.image_update.json").read_text(encoding="utf-8"))
        invalid = json.loads((fixtures / "invalid.command.shell.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_command(valid)["payload"], {"backup_mode": "data"})
        with self.assertRaises(ProtocolError):
            validate_command(invalid)

    def test_rejects_unknown_actions_and_non_uuid_operations(self):
        with self.assertRaises(ProtocolError):
            validate_command({
                "schema_version": 1,
                "operation_id": "not-a-uuid",
                "action": "composer.purge",
            })

    def test_backup_create_is_typed_and_restore_is_rejected(self):
        value = command("dlux.backup.create", {"backup_mode": "full"})
        self.assertEqual(value["payload"], {"backup_mode": "full"})
        with self.assertRaises(ProtocolError):
            command("dlux.backup.restore", {})

    def test_rejects_unknown_fields_and_invalid_command_timestamps(self):
        with self.assertRaises(ProtocolError):
            command("dlux.image_update", {"backup_mode": "data", "command": "id"})
        value = {
            "schema_version": 1,
            "operation_id": str(uuid.uuid4()),
            "action": "composer.restart",
            "created_at": "not-a-date",
            "deadline_at": "2099-07-23T10:00:00+00:00",
            "actor": {},
            "payload": {},
        }
        with self.assertRaises(ProtocolError):
            validate_command(value)

    def test_redacts_inherited_values_and_sensitive_assignments(self):
        with patch.dict(
            os.environ,
            {"COMPOSER_INHERITED_SECRET_KEYS": "DB_PASSWORD", "DB_PASSWORD": "very-secret"},
            clear=True,
        ):
            value = redact_text("password=plain DB_PASSWORD=very-secret authorization: bearer-value")
        self.assertNotIn("plain", value)
        self.assertNotIn("very-secret", value)
        self.assertNotIn("bearer-value", value)
        self.assertIn("[REDACTED]", value)

    def test_control_url_requires_https_except_explicit_localhost(self):
        with self.assertRaises(ValueError):
            ControlPlaneClient("http://control.example.com")
        ControlPlaneClient("http://localhost:8000", allow_http_localhost=True)
        ControlPlaneClient("https://control.example.com")


class AgentStoreTests(unittest.TestCase):
    def test_credentials_are_private_and_commands_are_deduplicated(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AgentStore(temp_dir)
            store.save_credentials("agent-id", "agent-secret")
            self.assertEqual(store.load_credentials()["agent_id"], "agent-id")
            mode = stat.S_IMODE(Path(store.db_path).stat().st_mode)
            self.assertEqual(mode, 0o600)

            store.stage_credentials("operation-id", "agent-id", "new-secret", "rotation-id")
            self.assertEqual(store.load_credentials()["secret"], "agent-secret")
            self.assertEqual(store.pending_credentials()["secret"], "new-secret")
            store.promote_pending_credentials()
            self.assertEqual(store.load_credentials()["secret"], "new-secret")
            self.assertIsNone(store.pending_credentials())

            value = command()
            self.assertTrue(store.enqueue_command(value))
            self.assertFalse(store.enqueue_command(value))
            store.transition(value["operation_id"], "accepted")
            store.transition(value["operation_id"], "running")
            outbox = store.pending_outbox()
            self.assertEqual([item["sequence"] for item in outbox], [1, 2])


class ComposerAgentTests(unittest.TestCase):
    def test_revoked_agent_can_reenroll_with_a_fresh_token(self):
        class Client:
            def enroll(self, token, capabilities):
                self.token = token
                return {"agent_id": "new-agent", "secret": "new-secret"}

        with tempfile.TemporaryDirectory() as temp_dir:
            args = agent_args(Path(temp_dir))
            args.enrollment_token = "fresh-token"
            agent = ComposerAgent(args)
            agent.client = Client()
            agent.store.save_credentials("old-agent", "old-secret")
            agent.store.set_meta("revoked", "2026-07-23T10:00:00+00:00")

            credentials = agent.ensure_enrolled()

            self.assertEqual(credentials, {"agent_id": "new-agent", "secret": "new-secret"})
            self.assertEqual(agent.store.load_credentials(), credentials)
            self.assertEqual(agent.store.get_meta("revoked"), "")

    def test_delivered_cancellation_is_honored_before_execution(self):
        class Client:
            def post_event(self, credentials, operation_id, event):
                raise ControlPlaneError("operation cancelled", status=409)

        with tempfile.TemporaryDirectory() as temp_dir:
            agent = ComposerAgent(agent_args(Path(temp_dir)))
            agent.client = Client()
            agent.store.save_credentials("agent-id", "agent-secret")
            value = command("composer.restart", {"service": "web"})
            agent.store.enqueue_command(value)
            agent.execute_received_command()
            with patch("composer.agent.subprocess.run") as run:
                agent.flush_outbox()
                agent.execute_received_command()
            run.assert_not_called()
            self.assertEqual(agent.store.command_state(value["operation_id"]), "cancelled")

    def test_capabilities_are_queued_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            agent = ComposerAgent(agent_args(Path(temp_dir)))
            agent.publish_capabilities()
            agent.publish_capabilities()
            queued = [item for item in agent.store.pending_outbox() if item["kind"] == "capabilities"]
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0]["body"]["schema_version"], 1)
            self.assertIn("agent.rotate_credentials", queued[0]["body"]["capabilities"])

    def test_rotation_keeps_old_credential_until_confirmation_retries(self):
        class Client:
            attempts = 0

            def begin_rotation(self, credentials):
                return {"agent_id": credentials["agent_id"], "secret": "new-secret", "rotation_id": "rotation-id"}

            def confirm_rotation(self, credentials, rotation_id):
                self.attempts += 1
                if self.attempts == 1:
                    raise ControlPlaneError("offline")

        with tempfile.TemporaryDirectory() as temp_dir:
            agent = ComposerAgent(agent_args(Path(temp_dir)))
            agent.client = Client()
            agent.store.save_credentials("agent-id", "old-secret")
            value = command("agent.rotate_credentials")
            agent.store.enqueue_command(value)
            agent.execute_received_command()
            accepted = next(item for item in agent.store.pending_outbox() if item["body"].get("state") == "accepted")
            agent.store.acknowledge_outbox(accepted["id"])
            agent.execute_received_command()
            self.assertEqual(agent.store.load_credentials()["secret"], "old-secret")
            self.assertEqual(agent.store.command_state(value["operation_id"]), "running")
            agent.process_pending_rotation()
            self.assertEqual(agent.store.load_credentials()["secret"], "new-secret")
            self.assertEqual(agent.store.command_state(value["operation_id"]), "succeeded")

    def test_dlux_command_is_spooled_and_finalized_idempotently(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            agent = ComposerAgent(agent_args(root))
            value = command(payload={"backup_mode": "full"})
            agent.store.enqueue_command(value)

            agent.execute_received_command()

            request_path = root / "bridge" / "requests" / f"{value['operation_id']}.json"
            request = json.loads(request_path.read_text(encoding="utf-8"))
            self.assertEqual(request["payload"]["backup_mode"], "full")
            self.assertEqual(agent.store.command_state(value["operation_id"]), "running")

            result_path = root / "bridge" / "results" / f"{value['operation_id']}.json"
            result_path.write_text(json.dumps({
                "operation_id": value["operation_id"],
                "status": "completed",
                "target_version": "1.5.0",
            }), encoding="utf-8")
            agent.process_bridge_results()
            agent.process_bridge_results()

            self.assertEqual(agent.store.command_state(value["operation_id"]), "succeeded")
            events = [item for item in agent.store.pending_outbox() if item["kind"] == "event"]
            self.assertEqual(sum(item["body"]["state"] == "succeeded" for item in events), 1)

    def test_restart_rejects_service_outside_allowlist_without_spawning(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            agent = ComposerAgent(agent_args(root))
            value = command("composer.restart", {"service": "db"})
            agent.store.enqueue_command(value)
            with patch.dict(os.environ, {"COMPOSER_AGENT_RESTART_SERVICES": "web,db"}, clear=True), patch(
                "composer.agent.subprocess.run"
            ) as run:
                agent.execute_received_command()
            run.assert_not_called()
            self.assertEqual(agent.store.command_state(value["operation_id"]), "failed")

    def test_project_restart_filters_protected_services_from_allowlist(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            agent = ComposerAgent(agent_args(Path(temp_dir)))
            value = command("composer.restart", {})
            agent.store.enqueue_command(value)
            with patch.dict(
                os.environ,
                {"COMPOSER_AGENT_RESTART_SERVICES": "web,db,dlux-updater"},
                clear=True,
            ), patch(
                "composer.agent.subprocess.run",
                return_value=SimpleNamespace(returncode=0),
            ) as run:
                agent.execute_received_command()

            child_env = run.call_args.kwargs["env"]
            self.assertEqual(child_env["COMPOSER_RESTART_SERVICES"], "web")
            self.assertEqual(agent.store.command_state(value["operation_id"]), "succeeded")

    def test_recovery_deploy_is_typed_and_runs_composer_update(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            agent = ComposerAgent(agent_args(root))
            value = command(
                "composer.recovery_deploy",
                {"force": True, "reason": "DLUX cannot start"},
            )
            agent.store.enqueue_command(value)
            with patch("composer.agent.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
                agent.execute_received_command()
            argv = run.call_args.args[0]
            self.assertIn("-u", argv)
            self.assertIn("--force", argv)
            self.assertEqual(agent.store.command_state(value["operation_id"]), "succeeded")


class AgentPairingTests(unittest.TestCase):
    """UI-driven pairing: DjangoLux delivers control URL + one-use code over the
    bridge; the agent redeems it via the standard enroll endpoint."""

    def _write_request(self, agent, operation_id, code, url="https://panel.test"):
        agent._atomic_json(agent.enroll_request_path, {
            "schema_version": 1,
            "operation_id": operation_id,
            "control_url": url,
            "pairing_code": code,
            "requested_at": "2026-07-23T10:00:00+00:00",
        })

    def test_bridge_pairing_enrolls_persists_url_and_is_idempotent(self):
        class Client:
            calls = 0

            def enroll(self, token, capabilities):
                Client.calls += 1
                self.token = token
                return {"agent_id": "paired-agent", "secret": "paired-secret"}

        with tempfile.TemporaryDirectory() as temp_dir:
            agent = ComposerAgent(agent_args(Path(temp_dir)))
            op = str(uuid.uuid4())
            with patch.object(agent, "_build_client", return_value=Client()):
                self._write_request(agent, op, "GOOD-CODE")
                agent.process_enroll_request()
                # idempotent: a repeat of the same operation must not re-enroll
                agent.process_enroll_request()

            self.assertEqual(Client.calls, 1)
            self.assertEqual(
                agent.store.load_credentials(),
                {"agent_id": "paired-agent", "secret": "paired-secret"},
            )
            self.assertEqual(agent.store.get_meta("control_url"), "https://panel.test")
            self.assertEqual(agent.control_url, "https://panel.test")
            self.assertIsNotNone(agent.client)

            status = json.loads(agent.agent_status_path.read_text())
            self.assertTrue(status["enrolled"])
            self.assertEqual(status["control_url"], "https://panel.test")
            self.assertEqual(status["last_enroll"]["state"], "ok")
            self.assertEqual(status["last_enroll"]["operation_id"], op)

    def test_persisted_control_url_rebuilds_client_after_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first = ComposerAgent(agent_args(Path(temp_dir)))
            first.store.set_meta("control_url", "https://panel.test")
            # A fresh agent over the same durable store (no env control URL).
            second = ComposerAgent(agent_args(Path(temp_dir)))
            self.assertEqual(second.control_url, "https://panel.test")
            self.assertIsNotNone(second.client)

    def test_pairing_failure_reports_error_and_leaves_agent_unenrolled(self):
        class Client:
            def enroll(self, token, capabilities):
                raise ControlPlaneError("invalid or expired pairing code", status=400)

        with tempfile.TemporaryDirectory() as temp_dir:
            agent = ComposerAgent(agent_args(Path(temp_dir)))
            op = str(uuid.uuid4())
            with patch.object(agent, "_build_client", return_value=Client()):
                self._write_request(agent, op, "BAD-CODE")
                agent.process_enroll_request()

            self.assertIsNone(agent.store.load_credentials())
            status = json.loads(agent.agent_status_path.read_text())
            self.assertFalse(status["enrolled"])
            self.assertEqual(status["last_enroll"]["state"], "error")


if __name__ == "__main__":
    unittest.main()
