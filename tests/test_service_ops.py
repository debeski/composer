"""Operations-centre requests: list services, restart/recreate one, tail its logs."""

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from composer import executor_ops, executor_protocol
from composer.ops import OpsResponder, _parse_ps
from composer.watcher import WatchRuntime

CONFIG = json.dumps({"services": {
    "web": {"labels": {"org.dlux.restart": "safe"}},
    "celery": {"labels": {"org.dlux.restart": "safe"}},
    "db": {"labels": {"org.dlux.restart": "protected"}},
    "composer-agent": {"labels": {"org.dlux.restart": "protected"}},
}})
PS = "\n".join(json.dumps(row) for row in (
    {"Service": "web", "State": "running", "Health": "healthy", "Status": "Up 2 hours (healthy)"},
    {"Service": "db", "State": "running", "Health": "healthy", "Status": "Up 2 hours"},
))


def _args(trigger):
    return SimpleNamespace(
        trigger_file=str(trigger), package_trigger_file=None, status_file=None, log_file=None,
        interval=2, dev=False, file=None, check_image=[], availability_file=None, check_interval=900,
    )


class FakeCompose:
    def __init__(self, logs="line one\nAPI_KEY=supersecret\n"):
        self.calls = []
        self.logs = logs

    def __call__(self, launcher, args, timeout=None):
        self.calls.append(list(args))
        if args[0] == "config":
            return True, CONFIG, ""
        if args[0] == "ps":
            return True, PS, ""
        if args[0] == "logs":
            return True, self.logs, ""
        return False, "", "unexpected"


class ServiceOpsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name)
        self.responder = OpsResponder(WatchRuntime(_args(self.state / "image-update-request.json")))
        self.compose = FakeCompose()
        patches = [
            patch("composer.launcher.DockerComposeLauncher.run_docker_compose", autospec=True, side_effect=self.compose),
            patch("composer.launcher.DockerComposeLauncher.resolve_active_compose_files", autospec=True),
            patch.dict(os.environ, {"COMPOSER_AGENT_RESTART_SERVICES": "web,celery,db"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _ask(self, operation, **fields):
        (self.state / "ops-request.json").write_text(
            json.dumps({"schema_version": 1, "token": f"t-{operation}-{len(fields)}", "operation": operation, **fields}),
            encoding="utf-8",
        )
        self.responder.answer()
        return json.loads((self.state / "ops-result.json").read_text(encoding="utf-8"))

    def test_services_lists_state_and_who_may_be_restarted(self):
        result = self._ask("services")
        self.assertEqual(result["error"], "")
        rows = {row["name"]: row for row in result["services"]}
        self.assertEqual(rows["web"]["state"], "running")
        self.assertTrue(rows["web"]["can_restart"])
        self.assertEqual(rows["celery"]["state"], "absent")
        self.assertFalse(rows["db"]["can_restart"], "protected even when allow-listed")
        self.assertFalse(rows["composer-agent"]["can_restart"])

    def test_restart_goes_through_the_executor_for_an_allowed_service(self):
        with patch("composer.executor_client.executor_configured", return_value=True), \
                patch("composer.executor_client.run_operation", return_value=(0, "")) as run:
            result = self._ask("service-restart", service="web")
        self.assertEqual(result["error"], "")
        self.assertEqual(run.call_args.args[:2], ("restart", {"service": "web"}))
        with patch("composer.executor_client.executor_configured", return_value=True), \
                patch("composer.executor_client.run_operation", return_value=(0, "")) as run:
            self._ask("service-recreate", service="celery")
        self.assertEqual(run.call_args.args[:2], ("recreate", {"service": "celery"}))

    def test_protected_or_unknown_services_are_refused_before_the_executor(self):
        with patch("composer.executor_client.run_operation") as run:
            protected = self._ask("service-restart", service="db")
            unknown = self._ask("service-recreate", service="rm -rf /")
        run.assert_not_called()
        self.assertIn("may not be restarted", protected["error"])
        self.assertIn("is not a service", unknown["error"])

    def test_logs_are_bounded_and_redacted(self):
        result = self._ask("logs-tail", service="web", lines=999999)
        self.assertEqual(result["error"], "")
        self.assertEqual(result["lines"], 1000)
        self.assertNotIn("supersecret", result["logs"])
        self.assertIn("line one", result["logs"])
        self.assertIn(["logs", "--no-color", "--no-log-prefix", "--tail", "1000", "web"], self.compose.calls)

    def test_all_services_merge_into_one_timeline(self):
        self.compose.logs = (
            "web-1     | 2026-10-21T10:00:02.000Z second\n"
            "celery-1  | 2026-10-21T10:00:01.000Z first\n"
            "celery-1  | 2026-10-21T10:00:03.000Z third TOKEN=supersecret\n"
        )
        result = self._ask("logs-tail", lines=2)
        self.assertEqual(result["error"], "")
        self.assertEqual(result["service"], "")
        self.assertEqual(result["logs"].splitlines()[0], "web-1    | second")
        self.assertIn("third", result["logs"])
        self.assertNotIn("first", result["logs"], "only the newest lines across services")
        self.assertNotIn("supersecret", result["logs"])
        self.assertIn(["logs", "--no-color", "--timestamps", "--tail", "2"], self.compose.calls)

    def test_ps_parsing_accepts_lines_or_an_array(self):
        array = json.dumps([{"Service": "web", "State": "running"}])
        self.assertEqual(_parse_ps(array)["web"]["state"], "running")
        self.assertEqual(_parse_ps(PS)["db"]["health"], "healthy")


class ExecutorRecreateTests(unittest.TestCase):
    def test_the_protocol_needs_one_valid_service(self):
        base = {
            "protocol_version": executor_protocol.EXECUTOR_PROTOCOL_VERSION,
            "operation_id": "11111111-1111-1111-1111-111111111111",
            "op": "recreate",
        }
        with self.assertRaises(executor_protocol.ProtocolError):
            executor_protocol.validate_executor_request({**base, "payload": {"service": ""}})
        ok = executor_protocol.validate_executor_request({**base, "payload": {"service": "web"}})
        self.assertEqual(ok["payload"], {"service": "web"})

    def test_the_executor_enforces_the_allowlist_itself(self):
        with patch.dict(os.environ, {"COMPOSER_AGENT_RESTART_SERVICES": "web"}):
            self.assertEqual(executor_ops._run_recreate("op", "db")[0], 2)
            self.assertEqual(executor_ops._run_recreate("op", "celery")[0], 2)
            with patch("composer.launcher.DockerComposeLauncher.run_docker_compose", autospec=True,
                       return_value=(True, "", "")) as run, \
                    patch("composer.launcher.DockerComposeLauncher.resolve_secrets", autospec=True,
                          return_value=(True, "")), \
                    patch("composer.launcher.DockerComposeLauncher.resolve_active_compose_files", autospec=True):
                self.assertEqual(executor_ops._run_recreate("op", "web"), (0, ""))
            self.assertEqual(run.call_args.args[1], ["up", "-d", "--no-deps", "--force-recreate", "web"])

    def _project(self, secrets):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cwd = os.getcwd()
        os.chdir(tmp.name)
        self.addCleanup(os.chdir, cwd)
        Path("compose.yml").write_text(
            "services:\n  web:\n    image: x\n    environment:\n"
            "      DJANGO_SECRET_KEY: ${DJANGO_SECRET_KEY:-local_secret}\n"
            "      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}\n",
            encoding="utf-8",
        )
        if secrets is not None:
            Path(".secrets").mkdir()
            Path(".secrets/.env").write_text(secrets, encoding="utf-8")

    def test_a_recreate_runs_with_the_deployments_secrets(self):
        # Compose interpolates the secrets into the new container; without them
        # web got the `local_secret` default and never started (1.13.0b5).
        self._project("DJANGO_SECRET_KEY=the-real-key\nPOSTGRES_PASSWORD=pw\n")
        seen = {}

        def compose(launcher, args, **_kwargs):
            seen.update({key: launcher.build_compose_env().get(key) for key in ("DJANGO_SECRET_KEY", "POSTGRES_PASSWORD")})
            return True, "", ""

        with patch.dict(os.environ, {"COMPOSER_AGENT_RESTART_SERVICES": "web"}), \
                patch("composer.launcher.DockerComposeLauncher.run_docker_compose", autospec=True, side_effect=compose):
            os.environ.pop("DJANGO_SECRET_KEY", None)
            os.environ.pop("POSTGRES_PASSWORD", None)
            self.assertEqual(executor_ops._run_recreate("op", "web"), (0, ""))
        self.assertEqual(seen, {"DJANGO_SECRET_KEY": "the-real-key", "POSTGRES_PASSWORD": "pw"})

    def test_without_readable_secrets_nothing_is_recreated(self):
        self._project(None)
        with patch.dict(os.environ, {"COMPOSER_AGENT_RESTART_SERVICES": "web"}), \
                patch("composer.launcher.DockerComposeLauncher.run_docker_compose", autospec=True) as run:
            os.environ.pop("POSTGRES_PASSWORD", None)
            code, message = executor_ops._run_recreate("op", "web")
        self.assertEqual(code, 1)
        self.assertIn("compose defaults", message)
        run.assert_not_called()
