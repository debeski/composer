import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from composer.agent_check_publication import deployment_publication
from composer.cli import parse_agent_check_args
from composer.watcher import run_agent_check


class AgentCheckPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.compose = self.root / "compose.prod.yml"
        self.compose.write_text("services: {}\n")
        self.target = self.root / "image-available.json"
        self.target.write_text('{"available": false}')
        self.config = {"services": {"composer-agent": {"command": [
            "agent", "run", "--check-image", "app:latest",
            "--check-image", "worker:latest", "--availability-file", str(self.target),
        ]}}}
        self.launcher_patch = patch("composer.launcher.DockerComposeLauncher")
        self.launcher = self.launcher_patch.start().return_value
        self.addCleanup(self.launcher_patch.stop)
        self.launcher.compose_config_json.return_value = self.config
        self.launcher.run_docker_compose.return_value = (True, "", "")
        self.payload = {"available": True, "checked_at": "2026-09-19T00:00:00Z", "images": [
            {"image": image, "remote_digest": "sha256:new", "local_digest": "sha256:old",
             "update_available": True} for image in ("app:latest", "worker:latest")
        ]}

    def args(self, *extra):
        return parse_agent_check_args(["-f", str(self.compose), "--json", *extra])

    def run_check(self, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("composer.watcher.availability_payload", return_value=self.payload) as build:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = run_agent_check(self.args(*extra))
        return code, stdout.getvalue(), stderr.getvalue(), build

    def test_manual_check_publishes_the_same_document_without_restart(self):
        def execute(command, timeout):
            self.assertEqual(command[:5], ["exec", "-T", "composer-agent", "python", "-c"])
            result = subprocess.run([sys.executable, *command[4:]], capture_output=True, text=True)
            return result.returncode == 0, result.stdout, result.stderr

        self.launcher.run_docker_compose.side_effect = execute
        code, stdout, stderr, build = self.run_check()
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout), self.payload)
        self.assertEqual(json.loads(self.target.read_text()), self.payload)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o644)
        self.assertEqual(list(self.root.glob(".image-available.json.*")), [])
        build.assert_called_once_with(["app:latest", "worker:latest"])
        self.launcher.run_docker_compose.assert_called_once()
        self.assertEqual(self.launcher.compose_file, str(self.compose))

    def test_stopped_service_fails_but_preserves_machine_readable_result(self):
        self.launcher.run_docker_compose.return_value = (False, "", "service is not running")
        code, stdout, stderr, _ = self.run_check()
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout), self.payload)
        self.assertIn("service is not running", stderr)
        self.assertFalse(json.loads(self.target.read_text())["available"])

    def test_unknown_registry_check_publishes_unknown_and_returns_failure(self):
        self.payload["images"][0]["remote_digest"] = None
        code, stdout, _, _ = self.run_check()
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout), self.payload)
        self.launcher.run_docker_compose.assert_called_once()

    def test_explicit_images_and_no_publish_do_not_touch_deployment(self):
        for extra in (("other:latest",), ("--no-publish", "other:latest")):
            with self.subTest(extra=extra):
                self.assertEqual(self.run_check(*extra)[0], 0)
        with patch.dict(os.environ, {"WEB_IMAGE": "app:latest"}):
            self.assertEqual(self.run_check("--no-publish")[0], 0)
        self.launcher.compose_config_json.assert_not_called()
        self.launcher.run_docker_compose.assert_not_called()

    def test_explicit_output_path_overrides_deployment_publication(self):
        target = self.root / "explicit.json"
        self.assertEqual(self.run_check("--availability-file", str(target), "app:latest")[0], 0)
        self.assertEqual(json.loads(target.read_text()), self.payload)
        self.launcher.compose_config_json.assert_not_called()

    def test_config_failure_does_not_silently_skip_publication(self):
        self.launcher.compose_config_json.return_value = None
        code, _, stderr, build = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("Could not resolve Compose", stderr)
        build.assert_not_called()

    def test_config_diagnostics_do_not_corrupt_json_stdout(self):
        self.launcher.resolve_secrets.side_effect = lambda: print("secrets resolved")
        code, stdout, stderr, _ = self.run_check()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout), self.payload)
        self.assertIn("secrets resolved", stderr)

    def test_string_commands_and_legacy_publisher_use_configured_path(self):
        self.config["services"] = {"composer-updater": {"command":
            "agent watch --check-image=legacy:latest --availability-file=/runtime/state/image.json"}}
        publication = deployment_publication(self.args("-d"))
        self.assertEqual(publication.service, "composer-updater")
        self.assertEqual(publication.path, "/runtime/state/image.json")
        self.assertEqual(publication.images, ["legacy:latest"])
        self.assertTrue(self.launcher.dev_mode)

    def test_agent_implicit_output_path_is_derived_from_trigger(self):
        self.config["services"]["composer-agent"]["command"] = [
            "agent", "run", "--check-image", "app:latest",
            "--trigger-file", "/custom/state/request.json",
        ]
        self.assertEqual(deployment_publication(self.args()).path, "/custom/state/image-available.json")

    def test_no_publisher_keeps_standalone_check_behavior(self):
        self.config["services"] = {"web": {}}
        self.assertIsNone(deployment_publication(self.args()))
