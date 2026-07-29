import contextlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from composer.cli import parse_agent_check_args
from composer.watcher import run_agent_check


AGENT_COMPOSE = '''name: demo
services:
  web:
    image: "${WEB_IMAGE:-demo:latest}"
  composer-agent:
    image: debeski/composer:latest
    command:
      - agent
      - --trigger-file
      - /opt/dlux-runtime/state/image-update-request.json
      - --check-image
      - registry.example/dlux/app:latest
      - --availability-file
      - /opt/dlux-runtime/state/image-available.json
    environment:
      WEB_IMAGE: "registry.example/dlux/app:latest"
'''


@contextlib.contextmanager
def workdir(path):
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield Path(path)
    finally:
        os.chdir(previous)


def payload(*, available=False, remote="sha256:same", local="sha256:same"):
    return {
        "available": available,
        "checked_at": "2026-07-26T12:00:00+00:00",
        "images": [
            {
                "image": "registry.example/dlux/app:latest",
                "remote_digest": remote,
                "local_digest": local,
                "update_available": available,
            }
        ],
    }


class AgentCheckCommandTests(unittest.TestCase):
    def test_parser_accepts_machine_output_and_multiple_images(self):
        args = parse_agent_check_args(
            [
                "--json",
                "--availability-file",
                "/tmp/image-available.json",
                "example/web:latest",
                "example/worker:latest",
            ]
        )

        self.assertTrue(args.json)
        self.assertEqual(args.availability_file, "/tmp/image-available.json")
        self.assertEqual(
            args.image,
            ["example/web:latest", "example/worker:latest"],
        )

    @patch("composer.watcher.availability_payload")
    def test_json_output_reports_update_without_using_failure_exit(self, build):
        build.return_value = payload(
            available=True,
            remote="sha256:new",
            local="sha256:old",
        )
        args = parse_agent_check_args(
            ["--json", "registry.example/dlux/app:latest"]
        )
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            code = run_agent_check(args)

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), build.return_value)
        build.assert_called_once_with(["registry.example/dlux/app:latest"])

    @patch("composer.watcher.availability_payload")
    def test_web_image_is_the_inline_dlux_default(self, build):
        build.return_value = payload()
        args = parse_agent_check_args(["--json"])

        with patch.dict(
            "os.environ",
            {"WEB_IMAGE": "registry.example/dlux/app:latest"},
            clear=True,
        ), redirect_stdout(io.StringIO()):
            code = run_agent_check(args)

        self.assertEqual(code, 0)
        build.assert_called_once_with(["registry.example/dlux/app:latest"])

    def test_missing_image_is_usage_error(self):
        args = parse_agent_check_args([])
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as temp_dir, workdir(temp_dir), patch.dict(
            "os.environ", {}, clear=True
        ), redirect_stderr(stderr):
            code = run_agent_check(args)

        self.assertEqual(code, 2)
        self.assertIn("no image to check", stderr.getvalue())

    @patch("composer.watcher.availability_payload")
    def test_compose_agent_block_is_the_deployment_default(self, build):
        build.return_value = payload()
        args = parse_agent_check_args(["--json"])

        with tempfile.TemporaryDirectory() as temp_dir, workdir(temp_dir) as root:
            (root / "compose.yml").write_text(AGENT_COMPOSE, encoding="utf-8")
            with patch.dict("os.environ", {}, clear=True), redirect_stdout(
                io.StringIO()
            ):
                code = run_agent_check(args)

        self.assertEqual(code, 0)
        build.assert_called_once_with(["registry.example/dlux/app:latest"])

    @patch("composer.watcher.availability_payload")
    def test_compose_interpolation_resolves_defaults_and_environment(self, build):
        build.return_value = payload()
        compose = (
            "name: demo\n"
            "services:\n"
            "  composer-updater:\n"
            "    environment:\n"
            '      WEB_IMAGE: "${WEB_IMAGE:-registry.example/dlux/app:latest}"\n'
        )
        args = parse_agent_check_args(["--json"])

        with tempfile.TemporaryDirectory() as temp_dir, workdir(temp_dir) as root:
            (root / "docker-compose.yml").write_text(compose, encoding="utf-8")
            with patch.dict("os.environ", {}, clear=True), redirect_stdout(
                io.StringIO()
            ):
                code = run_agent_check(args)

        self.assertEqual(code, 0)
        build.assert_called_once_with(["registry.example/dlux/app:latest"])

    def test_unresolvable_compose_interpolation_is_not_checked(self):
        compose = (
            "name: demo\n"
            "services:\n"
            "  composer-agent:\n"
            "    environment:\n"
            '      WEB_IMAGE: "${WEB_IMAGE}"\n'
        )
        args = parse_agent_check_args([])
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as temp_dir, workdir(temp_dir) as root:
            (root / "compose.yml").write_text(compose, encoding="utf-8")
            with patch.dict("os.environ", {}, clear=True), redirect_stderr(stderr):
                code = run_agent_check(args)

        self.assertEqual(code, 2)
        self.assertIn("no image to check", stderr.getvalue())

    @patch("composer.watcher.availability_payload")
    def test_alternate_compose_file_flag_scopes_discovery(self, build):
        build.return_value = payload()

        with tempfile.TemporaryDirectory() as temp_dir, workdir(temp_dir) as root:
            (root / "compose.prod.yml").write_text(AGENT_COMPOSE, encoding="utf-8")
            args = parse_agent_check_args(["--json", "-f", "compose.prod.yml"])
            with patch.dict("os.environ", {}, clear=True), redirect_stdout(
                io.StringIO()
            ):
                code = run_agent_check(args)

        self.assertEqual(code, 0)
        build.assert_called_once_with(["registry.example/dlux/app:latest"])

    def test_digest_pinned_reference_is_rejected(self):
        args = parse_agent_check_args(["example/app@sha256:abc"])
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            code = run_agent_check(args)

        self.assertEqual(code, 2)
        self.assertIn("mutable tag references", stderr.getvalue())

    @patch("composer.watcher.availability_payload")
    def test_unknown_registry_result_is_not_reported_as_success(self, build):
        build.return_value = payload(remote=None, local="sha256:local")
        args = parse_agent_check_args(
            ["--json", "registry.example/dlux/app:latest"]
        )

        with redirect_stdout(io.StringIO()):
            code = run_agent_check(args)

        self.assertEqual(code, 1)

    @patch("composer.watcher.availability_payload")
    def test_availability_file_matches_stdout_document(self, build):
        build.return_value = payload()
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "state" / "image-available.json"
            args = parse_agent_check_args(
                [
                    "--json",
                    "--availability-file",
                    str(target),
                    "registry.example/dlux/app:latest",
                ]
            )
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                code = run_agent_check(args)

            self.assertEqual(code, 0)
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")),
                json.loads(stdout.getvalue()),
            )
            self.assertFalse(target.with_name(f".{target.name}.tmp").exists())
