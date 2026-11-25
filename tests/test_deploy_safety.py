"""Deploy safety: break-it findings #3, #10 and #11 (2026-11-25)."""
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from composer import downgrade_guard, health_monitor
from composer.constants import SERVICE_HEALTHY
from composer.launcher import DockerComposeLauncher


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _running(service, container_id, health=""):
    return {"Service": service, "ID": container_id, "State": "running", "Health": health}


class HealthHarness:
    """A launcher whose Docker calls replay scripted polls."""

    def setUp(self):
        self.clock = FakeClock()
        patcher = patch.object(health_monitor, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = patch.dict(os.environ, {health_monitor.SETTLE_ENV: "20"})
        env.start()
        self.addCleanup(env.stop)
        self.launcher = DockerComposeLauncher()
        self.launcher.services = ["web", "celery"]
        self.launcher.service_state = {}
        self.launcher.render = lambda *args, **kwargs: None
        self.statuses = []
        self.launcher.emit_status = lambda label, message: self.statuses.append(message)
        self.logs_requested = []

        def run_docker_compose(args, timeout=None):
            if args[:1] == ["logs"]:
                self.logs_requested.append(args[-1])
                return True, f"{args[-1]}-1  | ModuleNotFoundError: No module named 'celery'\n", ""
            return True, "", ""

        self.launcher.run_docker_compose = run_docker_compose

    def script(self, polls, markers):
        """``polls``: ps entries per poll; ``markers``: inspect output per poll."""
        state = {"poll": -1}

        def ps(include_all=False):
            if include_all:
                return True, polls[min(state["poll"], len(polls) - 1)], ""
            state["poll"] += 1
            return True, polls[min(state["poll"], len(polls) - 1)], ""

        def run_command(argv, timeout=None, env=None):
            if argv[:2] == ["docker", "inspect"]:
                return True, markers[min(state["poll"], len(markers) - 1)], ""
            return False, "", "unexpected"

        self.launcher.get_compose_ps_entries = ps
        self.launcher.run_command = run_command


class HealthSettleWindowTests(HealthHarness, unittest.TestCase):
    """#3: the result must not depend on when the poll lands."""

    def test_a_crash_looping_service_without_a_healthcheck_fails(self):
        # celery has no healthcheck, so it reads "running" between crashes. Its
        # restart count climbs while every poll still looks healthy.
        polls = [[_running("web", "aaa"), _running("celery", "ccc")]]
        markers = [
            "aaa 0 2026-11-25T10:00:00Z\nccc 0 2026-11-25T10:00:00Z\n",
            "aaa 0 2026-11-25T10:00:00Z\nccc 1 2026-11-25T10:00:02Z\n",
            "aaa 0 2026-11-25T10:00:00Z\nccc 2 2026-11-25T10:00:05Z\n",
        ]
        self.script(polls, markers)

        ok, detail = self.launcher.monitor_health()

        self.assertFalse(ok)
        self.assertIn("celery restarted once during the health check", detail)
        self.assertIn("No module named 'celery'", detail)
        self.assertEqual(self.logs_requested, ["celery"])

    def test_a_service_caught_restarting_fails(self):
        polls = [
            [_running("web", "aaa"), _running("celery", "ccc")],
            [_running("web", "aaa"), {"Service": "celery", "ID": "ccc", "State": "restarting"}],
        ]
        self.script(polls, ["aaa 0 t0\nccc 0 t0\n"])

        ok, detail = self.launcher.monitor_health()

        self.assertFalse(ok)
        self.assertIn("celery restarted", detail)

    def test_a_service_that_leaves_healthy_inside_the_window_fails(self):
        polls = [
            [_running("web", "aaa", "healthy"), _running("celery", "ccc")],
            [_running("web", "aaa", "unhealthy"), _running("celery", "ccc")],
        ]
        self.script(polls, ["aaa 0 t0\nccc 0 t0\n"])

        ok, detail = self.launcher.monitor_health()

        self.assertFalse(ok)
        self.assertIn("web was healthy, then went to", detail)
        self.assertIn("web", self.logs_requested)

    def test_stable_services_pass_only_after_the_settle_window(self):
        polls = [[_running("web", "aaa", "healthy"), _running("celery", "ccc")]]
        self.script(polls, ["aaa 3 t0\nccc 0 t0\n"])
        started = self.clock.now

        ok, detail = self.launcher.monitor_health()

        self.assertTrue(ok, detail)
        self.assertGreaterEqual(self.clock.now - started, 20)
        self.assertEqual(self.statuses[-1], "All services healthy")

    def test_a_restart_before_the_deploy_does_not_count(self):
        # A container kept from an earlier run carries its old RestartCount.
        polls = [[_running("web", "aaa", "healthy"), _running("celery", "ccc")]]
        self.script(polls, ["aaa 7 t0\nccc 0 t0\n"])
        self.assertTrue(self.launcher.monitor_health()[0])

    def test_the_settle_window_is_configurable(self):
        polls = [[_running("web", "aaa", "healthy"), _running("celery", "ccc")]]
        self.script(polls, ["aaa 0 t0\nccc 0 t0\n"])
        started = self.clock.now
        with patch.dict(os.environ, {health_monitor.SETTLE_ENV: "0"}):
            self.assertTrue(self.launcher.monitor_health()[0])
        self.assertLess(self.clock.now - started, 1)

    def test_restart_markers_map_containers_back_to_services(self):
        self.launcher.service_container_ids = {"web": ["aaa"], "celery": ["ccc"]}
        self.launcher.run_command = lambda argv, timeout=None, env=None: (
            True, "aaa111 0 t0\nccc222 4 t1\n", ""
        )
        self.assertEqual(
            self.launcher.container_restart_markers(),
            {"web": {"aaa111": (0, "t0")}, "celery": {"ccc222": (4, "t1")}},
        )


class EdgeProxyTests(unittest.TestCase):
    """#10: a failed deploy must leave the proxy serving the maintenance page."""

    def setUp(self):
        self.launcher = DockerComposeLauncher()
        self.launcher.services = ["web", "worker", "caddy"]
        self.compose_calls = []

        def run_docker_compose(args, timeout=None):
            self.compose_calls.append(list(args))
            return True, "", ""

        self.launcher.run_docker_compose = run_docker_compose
        self.entries = [
            {"Service": "web", "State": "running"},
            {"Service": "worker", "State": "running", "Health": "unhealthy"},
            {"Service": "caddy", "State": "created"},
        ]
        self.launcher.get_compose_ps_entries = lambda include_all=False: (True, self.entries, "")

    def test_a_proxy_left_created_is_started_without_its_dependencies(self):
        started, error = self.launcher.ensure_edge_proxy()
        self.assertEqual((started, error), (["caddy"], ""))
        self.assertEqual(self.compose_calls, [["up", "-d", "--no-deps", "caddy"]])

    def test_a_running_proxy_is_left_alone(self):
        self.entries[-1]["State"] = "running"
        self.assertEqual(self.launcher.ensure_edge_proxy(), ([], ""))
        self.assertEqual(self.compose_calls, [])

    def test_a_scoped_update_does_not_touch_the_proxy(self):
        self.launcher.up_service = ["web"]
        self.assertEqual(self.launcher.ensure_edge_proxy(), ([], ""))

    def test_the_failure_says_the_site_could_not_be_kept_up(self):
        self.launcher.run_docker_compose = lambda args, timeout=None: (False, "", "port is already allocated")
        detail = self.launcher.keep_site_reachable("worker failed")
        self.assertIn("could not be started", detail)
        self.assertIn("port is already allocated", detail)


def _deploy(launcher, health=(True, ""), launch=(True, "", "")):
    """Drive ``DockerComposeLauncher.run`` for a plain deploy with Docker stubbed."""
    launcher.extract_config = lambda: None
    launcher.services = list(launcher.services or ["web", "worker", "caddy"])

    def discover_services(silent=False):
        launcher.service_state = {s: SERVICE_HEALTHY for s in launcher.services}
        return True

    launcher.discover_services = discover_services
    launcher.update_service_states = lambda: True
    launcher.resolve_secrets = lambda: (True, "")
    launcher.render = lambda *args, **kwargs: None
    launcher.statuses = []
    launcher.write_status = lambda status, error=None: launcher.statuses.append((status, error))
    launcher.prepare_app_data = lambda services=None: (True, "")
    launcher.launched = []
    launcher.launch_containers = lambda: (launcher.launched.append(True) or launch)
    launcher.monitor_health = lambda: health
    launcher.run_post_start_hooks = lambda: (True, "")
    with patch("sys.argv", ["composer"]), patch("composer.launcher.install_hangup_guard"):
        try:
            launcher.run()
        except SystemExit as exc:
            return exc.code
    return 0


class FailedDeployKeepsProxyTests(unittest.TestCase):
    def setUp(self):
        self.launcher = DockerComposeLauncher()
        self.launcher.services = ["web", "worker", "caddy"]
        self.launcher.downgrade_guard = lambda: (True, "")
        self.launcher.record_deployed_release = lambda: None
        self.compose_calls = []
        self.caddy_state = "created"

        def run_docker_compose(args, timeout=None):
            self.compose_calls.append(list(args))
            if args[:3] == ["up", "-d", "--no-deps"]:
                self.caddy_state = "running"
            return True, "", ""

        self.launcher.run_docker_compose = run_docker_compose
        self.launcher.get_compose_ps_entries = lambda include_all=False: (
            True,
            [{"Service": "web", "State": "running"}, {"Service": "caddy", "State": self.caddy_state}],
            "",
        )

    def test_a_failed_health_check_starts_the_proxy(self):
        code = _deploy(self.launcher, health=(False, "Containers failed to become healthy: worker (failed)"))
        self.assertEqual(code, 1)
        self.assertIn(["up", "-d", "--no-deps", "caddy"], self.compose_calls)
        status, error = self.launcher.statuses[-1]
        self.assertEqual(status, "failed")
        self.assertIn("visitors get the maintenance page", error)

    def test_a_failed_up_starts_the_proxy(self):
        code = _deploy(self.launcher, launch=(False, "", "service worker pre_start failed"))
        self.assertEqual(code, 1)
        self.assertIn(["up", "-d", "--no-deps", "caddy"], self.compose_calls)

    def test_a_proxy_compose_left_created_is_started_before_health_is_watched(self):
        code = _deploy(self.launcher)
        self.assertEqual(code, 0)
        self.assertIn(["up", "-d", "--no-deps", "caddy"], self.compose_calls)


def _manifest_label(version):
    raw = json.dumps({"schema_version": 1, "version": version}).encode()
    return "base64:" + base64.urlsafe_b64encode(raw).decode()


def _labels(app, dlux):
    labels = {"org.dlux.stack-schema": "5"}
    if app:
        labels["org.dlux.project.release-manifest"] = _manifest_label(app)
    if dlux:
        labels["org.decrees.dlux_baked_version"] = dlux
    return labels


OLD = _labels("2.1.26", "1.15.1")
NEW = _labels("2.2.1", "1.16.3")
NEWER = _labels("2.2.2", "1.16.6")
CONFIG = {"services": {
    "web": {"image": "debeski/decrees:v2.1.26"},
    "worker": {"image": "debeski/decrees:v2.1.26"},
    "caddy": {"image": "caddy:latest"},
}}


class DowngradeGuardTests(unittest.TestCase):
    """#11: an app image older than the migrated database is refused before `up`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, cwd)
        self.launcher = DockerComposeLauncher()
        self.launcher.services = ["web", "worker", "caddy"]
        self.launcher.compose_config_json = lambda: CONFIG
        self.image = {"debeski/decrees:v2.1.26": OLD}
        self.containers = {"w1": NEW, "k1": NEW}
        self.pulled = []

        def run_command(argv, timeout=None, env=None):
            if argv[:3] == ["docker", "image", "inspect"]:
                labels = self.image.get(argv[3])
                return (labels is not None), json.dumps(labels), ""
            if argv[:3] == ["docker", "container", "inspect"]:
                return True, json.dumps(self.containers[argv[3]]), ""
            return False, "", "unexpected"

        def run_docker_compose(args, timeout=None):
            if args[:1] == ["pull"]:
                self.pulled.append(args[-1])
                self.image["debeski/decrees:v2.1.26"] = OLD
            return True, "", ""

        self.launcher.run_command = run_command
        self.launcher.run_docker_compose = run_docker_compose
        self.launcher.get_compose_ps_entries = lambda include_all=False: (
            True,
            [{"Service": "web", "ID": cid} if cid.startswith("w") else {"Service": "worker", "ID": cid}
             for cid in self.containers],
            "",
        )

    def test_labels_give_the_app_and_baked_djangolux_versions(self):
        self.assertEqual(downgrade_guard.label_versions(OLD), {"app": "2.1.26", "dlux": "1.15.1"})
        self.assertEqual(
            downgrade_guard.label_versions({"org.opencontainers.image.version": "3.0.0"}),
            {"app": "3.0.0"},
        )
        self.assertEqual(downgrade_guard.label_versions({"org.x.dlux_baked_version": ""}), {})

    def test_an_older_image_over_newer_containers_is_refused(self):
        ok, message = self.launcher.downgrade_guard()
        self.assertFalse(ok)
        self.assertIn("app 2.1.26 is older than 2.2.1", message)
        self.assertIn("DjangoLux 1.15.1 is older than 1.16.3", message)
        self.assertIn("--force", message)
        self.assertIn("Migrations do not run backwards", message)

    def test_the_record_still_refuses_after_down_removed_the_containers(self):
        downgrade_guard.write_record(downgrade_guard.label_versions(NEW), "debeski/decrees:v2.2.1", replace=False)
        self.containers = {}
        self.assertFalse(self.launcher.downgrade_guard()[0])

    def test_force_deploys_and_makes_the_forced_release_the_record(self):
        self.launcher.force = True
        ok, message = self.launcher.downgrade_guard()
        self.assertTrue(ok)
        self.assertIn("--force", message)
        self.launcher.record_deployed_release()
        self.assertEqual(downgrade_guard.read_record(), {"app": "2.1.26", "dlux": "1.15.1"})

    def test_an_upgrade_passes_and_raises_the_record(self):
        self.image["debeski/decrees:v2.1.26"] = NEWER
        self.assertEqual(self.launcher.downgrade_guard(), (True, ""))
        self.launcher.record_deployed_release()
        self.assertEqual(downgrade_guard.read_record(), {"app": "2.2.2", "dlux": "1.16.6"})

    def test_a_normal_deploy_never_lowers_the_record(self):
        downgrade_guard.write_record({"app": "2.2.1", "dlux": "1.16.3"}, "x", replace=False)
        downgrade_guard.write_record({"app": "2.1.0", "dlux": "1.17.0"}, "y", replace=False)
        self.assertEqual(downgrade_guard.read_record(), {"app": "2.2.1", "dlux": "1.17.0"})

    def test_an_unlabelled_image_over_a_labelled_release_is_refused(self):
        self.image["debeski/decrees:v2.1.26"] = {}
        ok, message = self.launcher.downgrade_guard()
        self.assertFalse(ok)
        self.assertIn("no app or DjangoLux version label", message)

    def test_a_stack_with_no_labelled_history_is_not_blocked(self):
        self.containers = {"w1": {}}
        self.assertEqual(self.launcher.downgrade_guard(), (True, ""))

    def test_an_image_not_pulled_yet_is_pulled_to_read_it(self):
        del self.image["debeski/decrees:v2.1.26"]
        self.assertFalse(self.launcher.downgrade_guard()[0])
        self.assertEqual(self.pulled, ["web"])

    def test_local_builds_are_not_compared(self):
        self.launcher.build_images = True
        self.assertEqual(self.launcher.downgrade_guard(), (True, ""))

    def test_a_scope_without_the_app_is_not_compared(self):
        self.launcher.up_service = ["caddy"]
        self.assertEqual(self.launcher.downgrade_guard(), (True, ""))

    def test_the_deploy_stops_before_up(self):
        code = _deploy(self.launcher)
        self.assertEqual(code, 1)
        self.assertEqual(self.launcher.launched, [])
        status, error = self.launcher.statuses[-1]
        self.assertEqual(status, "failed")
        self.assertIn("Refusing to deploy debeski/decrees:v2.1.26", error)


if __name__ == "__main__":
    unittest.main()
