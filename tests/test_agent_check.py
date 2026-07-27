import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from composer.cli import parse_agent_check_args
from composer.watcher import run_agent_check


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

        with patch.dict("os.environ", {}, clear=True), redirect_stderr(stderr):
            code = run_agent_check(args)

        self.assertEqual(code, 2)
        self.assertIn("provide at least one IMAGE", stderr.getvalue())

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
