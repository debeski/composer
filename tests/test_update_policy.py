"""DLUX_UPDATE_POLICY on the resident pair: the refusals and the compose mapping."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from composer import executor_ops, ops, update_policy, update_policy_env
from composer.agent import ComposerAgent
from composer.checkup import CheckupMixin
from composer.watcher import WatchRuntime, run_watch

from tests.test_agent import agent_args
from tests.test_watcher import watch_args

LINE = 'DLUX_UPDATE_POLICY: "${DLUX_UPDATE_POLICY:-all}"'


def policy(value):
    return patch.dict(os.environ, {"DLUX_UPDATE_POLICY": value})


class PolicyValueTests(unittest.TestCase):
    def test_empty_means_all_and_unknown_means_none(self):
        self.assertEqual(update_policy.normalize(""), ("all", True))
        self.assertEqual(update_policy.normalize(" NONE "), ("none", True))
        self.assertEqual(update_policy.normalize("off"), ("none", False))
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(update_policy.current(), "all")

    def test_inline_allows_only_inline_updates(self):
        with policy("inline"):
            self.assertTrue(update_policy.allows(update_policy.INLINE_UPDATE))
            self.assertFalse(update_policy.allows(update_policy.IMAGE_UPDATE))
            self.assertFalse(update_policy.allows(update_policy.COMPOSER_UPDATE))


class ImageTriggerTests(unittest.TestCase):
    """A trigger web wrote is answered, never deployed, when the policy refuses it."""

    def _run(self, value):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = watch_args(root)
            Path(args.trigger_file).write_text(json.dumps({"token": "t-1"}), encoding="utf-8")
            with policy(value), patch(
                "composer.watcher.subprocess.run", return_value=SimpleNamespace(returncode=0),
            ) as child:
                code = run_watch(args)
            status = json.loads(Path(args.status_file).read_text(encoding="utf-8")) if Path(args.status_file).exists() else {}
            ack = json.loads(Path(f"{args.trigger_file}.ack").read_text(encoding="utf-8"))
            return code, child, status, ack

    def test_inline_refuses_the_image_update(self):
        code, child, status, ack = self._run("inline")
        self.assertEqual(code, update_policy.REFUSED_EXIT)
        child.assert_not_called()
        self.assertEqual(status["status"], "failed")
        self.assertIn("DLUX_UPDATE_POLICY=inline", status["error"])
        self.assertEqual(ack["token"], "t-1")

    def test_an_unknown_value_refuses_too(self):
        code, child, _status, _ack = self._run("sometimes")
        self.assertEqual(code, update_policy.REFUSED_EXIT)
        child.assert_not_called()

    def test_all_deploys(self):
        code, child, _status, _ack = self._run("all")
        self.assertEqual(code, 0)
        child.assert_called_once()


class PackageTriggerTests(unittest.TestCase):
    def _process(self, value, mode):
        with tempfile.TemporaryDirectory() as temp_dir:
            watch = WatchRuntime(watch_args(Path(temp_dir)))
            calls = []

            def runner(*args):
                calls.append(args)
                return 0, ""

            with policy(value), patch.object(watch, "maybe_check_package_availability"):
                code = watch.process_package({"token": "p-1", "payload": {"mode": mode}}, runner=runner)
            ack = json.loads(watch.package_ack.read_text(encoding="utf-8"))
            return code, calls, ack

    def test_none_refuses_an_inline_apply(self):
        code, calls, ack = self._process("none", "apply")
        self.assertEqual(code, update_policy.REFUSED_EXIT)
        self.assertEqual(calls, [])
        self.assertEqual(ack["token"], "p-1")

    def test_none_still_rolls_back(self):
        code, calls, _ack = self._process("none", "rollback")
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)

    def test_inline_applies(self):
        code, calls, _ack = self._process("inline", "apply")
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)


class ComposerUpdateTests(unittest.TestCase):
    def test_the_agent_refuses_agent_update_before_asking_the_executor(self):
        with policy("inline"), patch("composer.ops._resident_update_status") as status, \
                patch("composer.executor_client.run_operation") as run:
            with self.assertRaisesRegex(ValueError, "DLUX_UPDATE_POLICY=inline"):
                ops._update_resident_pair(SimpleNamespace(), {"token": "o-1"})
        status.assert_not_called()
        run.assert_not_called()

    def test_the_agent_still_checks(self):
        self.assertIn("agent-check", ops.HANDLERS)
        with policy("none"), patch("composer.ops._resident_update_status",
                                   return_value={"checked": True, "update_available": False,
                                                 "version": "1.11.0", "published_version": "1.11.0",
                                                 "channel": "stable", "detail": ""}):
            with patch("composer.executor_client.executor_configured", return_value=True):
                result = ops._check_resident(SimpleNamespace())
        self.assertEqual(result["exit_code"], 0)


class ExecutorTests(unittest.TestCase):
    """The executor refuses on its own reading of the policy, whatever the agent asks."""

    def _handle(self, op, payload=None):
        return executor_ops.default_operation_handler({"operation_id": "e-1", "op": op, "payload": payload or {}})

    def test_inline_refuses_agent_update_and_recovery(self):
        with policy("inline"), patch.object(executor_ops, "_run_agent_update") as agent_update, \
                patch.object(executor_ops, "_run_recovery") as recovery:
            for op in ("agent_update", "recovery_deploy"):
                result = self._handle(op, {"token": "x"})
                self.assertEqual(result["state"], "rejected", op)
                self.assertIn("DLUX_UPDATE_POLICY=inline", result["detail"])
        agent_update.assert_not_called()
        recovery.assert_not_called()

    def test_none_refuses_a_package_apply_but_not_its_rollback(self):
        with policy("none"), patch.object(executor_ops, "_run_dlux_package_apply") as apply, \
                patch.object(executor_ops, "_run_dlux_package_rollback", return_value=(0, "")) as rollback:
            self.assertEqual(self._handle("dlux_package_apply", {"version": "1", "filename": "f", "sha256": "s"})["state"],
                             "rejected")
            self.assertEqual(self._handle("dlux_package_rollback")["state"], "succeeded")
        apply.assert_not_called()
        rollback.assert_called_once()

    def test_inline_applies_a_package(self):
        with policy("inline"), patch.object(executor_ops, "_run_dlux_package_apply", return_value=(0, "")) as apply:
            self.assertEqual(self._handle("dlux_package_apply", {"version": "1"})["state"], "succeeded")
        apply.assert_called_once()


class AgentTests(unittest.TestCase):
    def test_a_control_link_recovery_deploy_is_refused(self):
        with policy("inline"):
            code, error = ComposerAgent._run_child(
                SimpleNamespace(), {"operation_id": "c-1", "action": "composer.recovery_deploy", "payload": {}},
            )
        self.assertEqual(code, update_policy.REFUSED_EXIT)
        self.assertIn("DLUX_UPDATE_POLICY=inline", error)

    def test_the_status_reports_the_enforced_policy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            agent = ComposerAgent(agent_args(Path(temp_dir)))
            with policy("Inline"):
                agent.publish_agent_status(force=True)
            self.assertEqual(json.loads(agent.agent_status_path.read_text())["update_policy"], "inline")


LEGACY = """name: demo

x-environment:
  &de
  DLUX_INLINE_UPDATES_ENABLED: "True"
  DLUX_UPDATE_CHECK_INTERVAL: "${DLUX_UPDATE_CHECK_INTERVAL:-86400}"

services:
  web:
    environment:
      <<: *de
  composer-executor:
    image: debeski/composer:latest
    environment:
      WEB_IMAGE: "demo:latest"
      COMPOSER_REGISTRY_HOST: "${COMPOSER_REGISTRY_HOST:-}"
      COMPOSER_EXECUTOR_SOCKET: "/run/composer-exec/composer-exec.sock"
  composer-agent:
    image: debeski/composer:latest
    environment:
      WEB_IMAGE: "demo:latest"
      COMPOSER_AGENT_STATE_DIR: "/var/lib/composer-agent"
"""


class ComposeMappingTests(unittest.TestCase):
    def test_the_transform_adds_one_line_per_block_and_is_idempotent(self):
        updated = update_policy_env.transform(LEGACY)
        self.assertIn(f'  DLUX_INLINE_UPDATES_ENABLED: "True"\n  {LINE}\n', updated)
        self.assertIn(f'      COMPOSER_REGISTRY_HOST: "${{COMPOSER_REGISTRY_HOST:-}}"\n      {LINE}\n', updated)
        self.assertIn(f'      WEB_IMAGE: "demo:latest"\n      {LINE}\n', updated)
        self.assertEqual(updated.count(LINE), 3)
        self.assertEqual(update_policy_env.transform(updated), updated)

    def test_migrate_archives_the_original(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "compose.yml").write_text(LEGACY, encoding="utf-8")
            preview = update_policy_env.preview(root, ["compose.yml"])
            self.assertEqual(preview["files"], ["compose.yml"])
            self.assertIn(f"+  {LINE}", preview["diff"])
            outcome = update_policy_env.migrate(root, ["compose.yml"])
            self.assertTrue(outcome["applied"])
            self.assertEqual((root / "compose.yml").read_text(encoding="utf-8").count(LINE), 3)
            archived = Path(outcome["backup_root"]) / "update-policy" / "compose.yml"
            self.assertEqual(archived.read_text(encoding="utf-8"), LEGACY)
            self.assertEqual(update_policy_env.preview(root, ["compose.yml"])["files"], [])

    def test_missing_services_reads_the_resolved_model(self):
        model = {"services": {
            "web": {"environment": {"DLUX_UPDATE_POLICY": "all"}},
            "worker": {"environment": ["DLUX_UPDATE_POLICY=inline"]},
            "composer-agent": {"environment": {}},
            "db": {},
        }}
        self.assertEqual(update_policy_env.missing_services(model), ["composer-agent"])


class CheckTests(unittest.TestCase):
    def _check(self, secrets, model):
        stub = SimpleNamespace(
            _compose_env_with_secrets=lambda: secrets,
            compose_config_json=lambda: model,
        )
        return CheckupMixin._check_update_policy(stub)

    FULL = {"services": {name: {"environment": {"DLUX_UPDATE_POLICY": "x"}} for name in update_policy_env.READERS}}

    def test_reports_the_policy(self):
        result = self._check({"DLUX_UPDATE_POLICY": "inline"}, self.FULL)
        self.assertEqual(result["level"], "ok")
        self.assertIn("Update policy inline", result["message"])

    def test_the_default_is_all(self):
        self.assertIn("Update policy all", self._check({}, self.FULL)["message"])

    def test_a_missing_resident_mapping_warns_with_the_fix(self):
        model = {"services": {"web": {"environment": {"DLUX_UPDATE_POLICY": "x"}}, "composer-agent": {}}}
        result = self._check({"DLUX_UPDATE_POLICY": "none"}, model)
        self.assertEqual(result["level"], "warn")
        self.assertIn("composer-agent", result["message"])
        self.assertIn("cannot enforce", result["message"])
        self.assertIn("check --fix", result["fix"])

    def test_an_unknown_value_warns(self):
        result = self._check({"DLUX_UPDATE_POLICY": "maybe"}, self.FULL)
        self.assertEqual(result["level"], "warn")
        self.assertIn("counts as none", result["message"])


if __name__ == "__main__":
    unittest.main()
