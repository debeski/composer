import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from composer.agent import ComposerAgent
from composer.watcher import LOCAL_DIGEST_PROBE_SECONDS, WatchRuntime


IMAGE = "registry.example/dlux/app:latest"


def _watch_args(root, **overrides):
    args = SimpleNamespace(
        trigger_file=str(root / "image-update-request.json"),
        status_file=None,
        log_file=None,
        interval=2,
        dev=False,
        file=None,
        check_image=[IMAGE],
        check_interval=3600,
        availability_file=str(root / "image-available.json"),
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class StaleAvailabilityTests(unittest.TestCase):
    """An update deployed by anything but this loop must clear promptly.

    The deployer CLI pulls a new image; nothing tells the agent, so the
    published document kept advertising an update that was already installed
    until the next scheduled registry check (an hour by default).
    """

    def test_deployer_pull_clears_the_stale_update_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watch = WatchRuntime(_watch_args(root))

            # Scheduled check: an update is genuinely available.
            with (
                patch("composer.watcher.remote_tag_digest", return_value="sha256:new"),
                patch("composer.watcher._local_repo_digest", return_value="sha256:old"),
                patch("composer.watcher.remote_image_labels", return_value={}),
            ):
                watch.maybe_check_availability()
            self.assertTrue(_read(watch.availability_file)["available"])
            self.assertEqual(watch.local_digests[IMAGE], "sha256:old")

            # The deployer pulls and recreates: the local digest moves, and the
            # next scheduled check is still an hour out.
            watch.next_local_probe = 0.0
            with (
                patch("composer.watcher.remote_tag_digest", return_value="sha256:new"),
                patch("composer.watcher._local_repo_digest", return_value="sha256:new"),
            ):
                watch.maybe_check_availability()

            payload = _read(watch.availability_file)
            self.assertFalse(payload["available"])
            self.assertFalse(payload["images"][0]["update_available"])
            self.assertEqual(payload["images"][0]["local_digest"], "sha256:new")
            self.assertEqual(watch.local_digests[IMAGE], "sha256:new")

    def test_unchanged_digest_does_not_republish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watch = WatchRuntime(_watch_args(root))
            with (
                patch("composer.watcher.remote_tag_digest", return_value="sha256:same"),
                patch("composer.watcher._local_repo_digest", return_value="sha256:same"),
            ):
                watch.maybe_check_availability()
                first = _read(watch.availability_file)["checked_at"]

                watch.next_local_probe = 0.0
                watch.maybe_check_availability()
                self.assertEqual(_read(watch.availability_file)["checked_at"], first)

    def test_unreadable_local_digest_is_unknown_not_a_change(self):
        """A transient Docker error must not trigger a re-publish."""
        with tempfile.TemporaryDirectory() as tmp:
            watch = WatchRuntime(_watch_args(Path(tmp)))
            watch.local_digests[IMAGE] = "sha256:old"
            watch.next_local_probe = 0.0
            with patch("composer.watcher._local_repo_digest", return_value=None):
                self.assertFalse(watch.local_image_changed())

    def test_probe_is_rate_limited_between_scheduled_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            watch = WatchRuntime(_watch_args(Path(tmp)))
            watch.local_digests[IMAGE] = "sha256:old"
            self.assertEqual(watch.local_probe_interval, LOCAL_DIGEST_PROBE_SECONDS)

            with patch(
                "composer.watcher._local_repo_digest", return_value="sha256:new"
            ) as probe:
                watch.next_local_probe = 0.0
                self.assertTrue(watch.local_image_changed())
                self.assertFalse(watch.local_image_changed())  # inside the window
            self.assertEqual(probe.call_count, 1)

    def test_probe_runs_well_inside_the_shortest_scheduled_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            watch = WatchRuntime(_watch_args(Path(tmp), check_interval=60))
            self.assertLess(watch.local_probe_interval, watch.check_interval)

    def test_untracked_image_is_skipped_until_seeded(self):
        with tempfile.TemporaryDirectory() as tmp:
            watch = WatchRuntime(_watch_args(Path(tmp)))
            watch.next_local_probe = 0.0
            with patch("composer.watcher._local_repo_digest") as probe:
                self.assertFalse(watch.local_image_changed())
            probe.assert_not_called()

    def test_disabled_availability_never_probes(self):
        with tempfile.TemporaryDirectory() as tmp:
            watch = WatchRuntime(_watch_args(Path(tmp), availability_file=None))
            with patch("composer.watcher._local_repo_digest") as probe:
                watch.maybe_check_availability(force=True)
            probe.assert_not_called()


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
        check_image=[IMAGE],
        check_interval=3600,
        availability_file=str(root / "image-available.json"),
        allow_http_localhost=False,
        once=True,
    )


class ExecutorUpdateRefreshesAvailabilityTests(unittest.TestCase):
    def test_observed_executor_update_republishes_availability(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = ComposerAgent(_agent_args(root))
            agent.store.set_meta("last_reported_ack_token", "tok-0")
            Path(f"{agent.args.trigger_file}.ack").write_text(
                json.dumps({"token": "tok-1", "exit_code": 0}), encoding="utf-8"
            )

            with patch.object(agent.watch, "maybe_check_availability") as refresh:
                agent._observe_executor_update()
            refresh.assert_called_once_with(force=True)

    def test_seeding_pass_does_not_republish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = ComposerAgent(_agent_args(root))
            Path(f"{agent.args.trigger_file}.ack").write_text(
                json.dumps({"token": "tok-1", "exit_code": 0}), encoding="utf-8"
            )

            with patch.object(agent.watch, "maybe_check_availability") as refresh:
                agent._observe_executor_update()
            refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
