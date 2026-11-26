import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from composer.registry import remote_image_version
from composer.watcher import check_availability, run_watch


def watch_args(root):
    return SimpleNamespace(
        trigger_file=str(root / "image-update-request.json"),
        status_file=str(root / "deploy-status.json"),
        log_file=str(root / "deploy-log.txt"),
        interval=2,
        dev=False,
        file=None,
        once=True,
        check_image=[],
        availability_file=None,
        check_interval=3600,
    )


class WatcherTerminalStatusTests(unittest.TestCase):
    def test_operation_id_is_forwarded_to_status_and_ack(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = watch_args(root)
            operation_id = "aa074a4b-d996-4c4c-a2ee-fbc245844ca0"
            Path(args.trigger_file).write_text(
                json.dumps({"token": "request-operation", "operation_id": operation_id}),
                encoding="utf-8",
            )

            def fail_with_operation(*child_args, **kwargs):
                self.assertIn("update", child_args[0])
                self.assertNotIn("-u", child_args[0])
                self.assertEqual(kwargs["env"]["COMPOSER_OPERATION_ID"], operation_id)
                return SimpleNamespace(returncode=1)

            with patch("composer.watcher.subprocess.run", side_effect=fail_with_operation):
                self.assertEqual(run_watch(args), 1)

            status = json.loads(Path(args.status_file).read_text(encoding="utf-8"))
            ack = json.loads(Path(f"{args.trigger_file}.ack").read_text(encoding="utf-8"))
            self.assertEqual(status["operation_id"], operation_id)
            self.assertEqual(ack["operation_id"], operation_id)

    def test_failed_child_guarantees_status_ack_and_console_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = watch_args(root)
            token = "request-123"
            Path(args.trigger_file).write_text(json.dumps({"token": token}), encoding="utf-8")
            Path(args.status_file).write_text(
                json.dumps({"status": "preparing", "updated_at": "old"}),
                encoding="utf-8",
            )

            with patch(
                "composer.watcher.subprocess.run",
                return_value=SimpleNamespace(returncode=1),
            ):
                self.assertEqual(run_watch(args), 1)

            status = json.loads(Path(args.status_file).read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["request_token"], token)
            self.assertEqual(status["exit_code"], 1)
            self.assertIn("exited with status 1", status["error"])

            ack = json.loads(Path(f"{args.trigger_file}.ack").read_text(encoding="utf-8"))
            self.assertEqual(ack["token"], token)
            self.assertEqual(ack["exit_code"], 1)
            self.assertIn("Update failed", Path(args.log_file).read_text(encoding="utf-8"))

    def test_failed_child_preserves_its_detailed_terminal_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = watch_args(root)
            token = "request-detail"
            Path(args.trigger_file).write_text(json.dumps({"token": token}), encoding="utf-8")

            def fail_with_status(*_args, **_kwargs):
                Path(args.status_file).write_text(
                    json.dumps({"status": "failed", "error": "Health check failed for web."}),
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=1)

            with patch("composer.watcher.subprocess.run", side_effect=fail_with_status):
                self.assertEqual(run_watch(args), 1)

            status = json.loads(Path(args.status_file).read_text(encoding="utf-8"))
            self.assertEqual(status["error"], "Health check failed for web.")
            self.assertEqual(status["request_token"], token)

    def test_child_launch_error_is_terminalized_instead_of_crashing_watcher(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = watch_args(root)
            Path(args.trigger_file).write_text(
                json.dumps({"token": "request-spawn"}),
                encoding="utf-8",
            )

            with patch(
                "composer.watcher.subprocess.run",
                side_effect=OSError("python executable unavailable"),
            ):
                self.assertEqual(run_watch(args), 127)

            status = json.loads(Path(args.status_file).read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["exit_code"], 127)
            self.assertIn("could not start", status["error"])


def _stale_status(path, record):
    """An earlier run's deploy-status.json, written well before this request."""
    import os
    import time

    Path(path).write_text(json.dumps(record), encoding="utf-8")
    then = time.time() - 600
    os.utime(path, (then, then))


class TerminalStatusDescribesOnlyItsRequestTests(unittest.TestCase):
    """1.11.0b1 live proof F4 / N-8: a refused or failed request kept the previous run's fields."""

    PREVIOUS_IMAGE_RUN = {
        "status": "ready", "updated_at": "2026-11-26T14:44:58+00:00", "request_token": "e3-run-3",
        "active_version": "1.17.0b1", "target_version": "1.17.0b1", "composer_version": "1.11.0b1",
        "target_images": ["testbed-dlux-app:2.2.2-dlux-1.17.0b1"], "compose_files": ["compose.yml"],
    }
    PREVIOUS_ROLLBACK = {
        "status": "completed", "updated_at": "2026-11-26T15:10:00+00:00", "kind": "inline",
        "run_token": "JYhs", "action": "rollback", "error": "",
    }

    def test_a_refused_image_request_writes_only_itself(self):
        import os

        with tempfile.TemporaryDirectory() as temp_dir:
            args = watch_args(Path(temp_dir))
            _stale_status(args.status_file, self.PREVIOUS_IMAGE_RUN)
            Path(args.trigger_file).write_text(json.dumps({"token": "f4"}), encoding="utf-8")
            with patch.dict(os.environ, {"DLUX_UPDATE_POLICY": "inline"}), \
                    patch("composer.watcher.subprocess.run") as child:
                self.assertEqual(run_watch(args), 2)
            child.assert_not_called()
            status = json.loads(Path(args.status_file).read_text(encoding="utf-8"))
        self.assertEqual(set(status), {"status", "updated_at", "error", "request_token", "exit_code"})
        self.assertEqual(status["request_token"], "f4")
        self.assertIn("DLUX_UPDATE_POLICY=inline", status["error"])

    def _package(self, previous, runner, token="f3pkg"):
        from composer.watcher import WatchRuntime

        with tempfile.TemporaryDirectory() as temp_dir:
            watch = WatchRuntime(watch_args(Path(temp_dir)))
            if previous is not None:
                _stale_status(watch.args.status_file, previous)
            with patch.object(watch, "maybe_check_package_availability"):
                code = watch.process_package({"token": token, "payload": {"mode": "apply"}}, runner=runner)
            return code, json.loads(Path(watch.args.status_file).read_text(encoding="utf-8"))

    def test_a_failed_package_request_is_not_merged_into_an_earlier_rollback(self):
        code, status = self._package(
            self.PREVIOUS_ROLLBACK,
            lambda *args: (1, "Could not stage the DjangoLux release: DjangoLux 1.17.0b1 is not published."),
        )
        self.assertEqual(code, 1)
        self.assertNotIn("run_token", status)
        self.assertNotIn("action", status)
        self.assertNotIn("kind", status)
        self.assertEqual(status["request_token"], "f3pkg")
        self.assertIn("not published", status["error"])

    def test_the_djangolux_run_that_wrote_the_request_is_kept(self):
        mine = dict(self.PREVIOUS_ROLLBACK, status="maintenance", run_token="run-9", action="apply")
        _code, status = self._package(mine, lambda *args: (1, "Could not stage."), token="run-9")
        self.assertEqual((status["kind"], status["run_token"], status["action"]), ("inline", "run-9", "apply"))
        self.assertEqual(status["status"], "failed")

    def test_what_the_child_published_during_the_request_is_kept(self):
        from composer.watcher import WatchRuntime

        with tempfile.TemporaryDirectory() as temp_dir:
            watch = WatchRuntime(watch_args(Path(temp_dir)))
            _stale_status(watch.args.status_file, self.PREVIOUS_ROLLBACK)

            def child(*args):
                Path(watch.args.status_file).write_text(
                    json.dumps({"ok": False, "message": "Health gate failed; rolled back."}), encoding="utf-8")
                return 1, ""

            with patch.object(watch, "maybe_check_package_availability"):
                watch.process_package({"token": "p-2", "payload": {"mode": "apply"}}, runner=child)
            status = json.loads(Path(watch.args.status_file).read_text(encoding="utf-8"))
        self.assertEqual(status["message"], "Health gate failed; rolled back.")
        self.assertNotIn("run_token", status)


class WatcherAvailabilityTests(unittest.TestCase):
    @patch("composer.registry.remote_image_labels")
    def test_existing_remote_image_version_api_uses_shared_label_lookup(self, labels):
        labels.return_value = {
            "org.opencontainers.image.version": "2.4.0",
            "org.example.version": "2026.7",
        }

        self.assertEqual(remote_image_version("example/app:latest"), "2.4.0")
        self.assertEqual(
            remote_image_version("example/app:latest", label="org.example.version"),
            "2026.7",
        )

    @patch("composer.watcher._local_repo_digest", return_value="sha256:old")
    @patch("composer.watcher.tag_digest", return_value=("sha256:new", ""))
    @patch("composer.watcher.remote_image_labels")
    def test_project_manifest_and_version_are_published_independently(
        self, labels, _remote, _local
    ):
        manifest = json.dumps({
            "schema_version": 1,
            "version": "2026.7",
            "summary": "Project release",
            "highlights": ["New report", "Faster imports"],
            "release_url": "https://example.com/releases/2026.7",
        }, separators=(",", ":"))
        labels.return_value = {
            "org.example.version": "2.4.0",
            "org.example.manifest": "base64:" + base64.urlsafe_b64encode(
                manifest.encode("utf-8")
            ).decode("ascii"),
        }
        with patch.dict(
            "os.environ",
            {
                "COMPOSER_VERSION_LABEL": "org.example.version",
                "COMPOSER_RELEASE_MANIFEST_LABEL": "org.example.manifest",
            },
            clear=True,
        ):
            available, images = check_availability(["example/app:latest"])

        self.assertTrue(available)
        self.assertEqual(images[0]["version"], "2.4.0")
        self.assertEqual(images[0]["manifest"]["version"], "2026.7")
        self.assertEqual(images[0]["manifest"]["highlights"], ["New report", "Faster imports"])

    @patch("composer.watcher._local_repo_digest", return_value="sha256:old")
    @patch("composer.watcher.tag_digest", return_value=("sha256:new", ""))
    @patch("composer.watcher.remote_image_labels")
    def test_manifest_publishes_baked_dlux_version(self, labels, _remote, _local):
        labels.return_value = {
            "org.dlux.project.release-manifest": json.dumps({
                "schema_version": 1,
                "version": "0.1.2",
                "baked_dlux_version": "1.5.3",
            }, separators=(",", ":")),
        }
        with patch.dict("os.environ", {}, clear=True):
            _available, images = check_availability(["example/app:latest"])

        self.assertEqual(images[0]["manifest"]["baked_dlux_version"], "1.5.3")

    @patch("composer.watcher._local_repo_digest", return_value="sha256:old")
    @patch("composer.watcher.tag_digest", return_value=("sha256:new", ""))
    @patch("composer.watcher.remote_image_labels")
    def test_manifest_without_baked_dlux_version_omits_it(self, labels, _remote, _local):
        labels.return_value = {
            "org.dlux.project.release-manifest": json.dumps({
                "schema_version": 1,
                "version": "0.1.2",
            }, separators=(",", ":")),
        }
        with patch.dict("os.environ", {}, clear=True):
            _available, images = check_availability(["example/app:latest"])

        self.assertNotIn("baked_dlux_version", images[0]["manifest"])

    @patch("composer.watcher._local_repo_digest", return_value="sha256:old")
    @patch("composer.watcher.tag_digest", return_value=("sha256:new", ""))
    @patch("composer.watcher.remote_image_labels")
    def test_raw_json_manifest_remains_supported(self, labels, _remote, _local):
        labels.return_value = {
            "org.dlux.project.release-manifest": json.dumps({
                "schema_version": 1,
                "version": "2.4.0",
                "highlights": ["Legacy raw JSON label"],
            }),
        }

        available, images = check_availability(["example/app:latest"])

        self.assertTrue(available)
        self.assertEqual(images[0]["manifest"]["version"], "2.4.0")

    @patch("composer.watcher._local_repo_digest", return_value="sha256:old")
    @patch("composer.watcher.tag_digest", return_value=("sha256:new", ""))
    @patch("composer.watcher.remote_image_labels")
    def test_invalid_manifest_does_not_hide_version_or_digest_update(
        self, labels, _remote, _local
    ):
        labels.return_value = {
            "org.opencontainers.image.version": "2.4.0",
            "org.dlux.project.release-manifest": json.dumps({
                "schema_version": True,
                "version": "not-a-supported-schema",
            }),
        }

        available, images = check_availability(["example/app:latest"])

        self.assertTrue(available)
        self.assertEqual(images[0]["version"], "2.4.0")
        self.assertNotIn("manifest", images[0])

    @patch("composer.watcher._local_repo_digest", return_value="sha256:old")
    @patch("composer.watcher.tag_digest", return_value=("sha256:new", ""))
    @patch("composer.watcher.remote_image_labels")
    def test_invalid_base64_manifest_does_not_hide_digest_update(
        self, labels, _remote, _local
    ):
        labels.return_value = {
            "org.dlux.project.release-manifest": "base64:not*valid",
        }

        available, images = check_availability(["example/app:latest"])

        self.assertTrue(available)
        self.assertNotIn("manifest", images[0])

    @patch("composer.watcher._local_repo_digest", return_value="sha256:old")
    @patch("composer.watcher.tag_digest", return_value=("sha256:new", ""))
    @patch("composer.watcher.remote_image_labels", return_value=None)
    def test_missing_all_metadata_still_publishes_digest_update(
        self, _labels, _remote, _local
    ):
        available, images = check_availability(["example/app:latest"])

        self.assertTrue(available)
        self.assertEqual(images[0]["remote_digest"], "sha256:new")
        self.assertNotIn("version", images[0])
        self.assertNotIn("manifest", images[0])

    @patch("composer.watcher._local_repo_digest", return_value="sha256:same")
    @patch("composer.watcher.tag_digest", return_value=("sha256:same", ""))
    @patch("composer.watcher.remote_image_labels")
    def test_unchanged_digest_does_not_fetch_optional_metadata(
        self, labels, _remote, _local
    ):
        available, images = check_availability(["example/app:latest"])

        self.assertFalse(available)
        self.assertFalse(images[0]["update_available"])
        labels.assert_not_called()
