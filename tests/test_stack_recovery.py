"""`check --fix` finishes what it changes: dropped services removed, changed ones recreated.

Section A of the stack recovery design. A repair that rewrites the Compose files
used to leave the running stack on its old definition (and dropped services
running orphaned) until the operator found the right Compose command.
"""

import io
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from composer.checkup import FAIL, OK, WARN
from composer.launcher import DockerComposeLauncher


def _args(**over):
    base = dict(
        file=None, dev=False, fix=True, yes=True, deep=False,
        deep_service="web", deep_command="python manage.py dlux_doctor",
        json=False, beta=False, stable=False, no_recreate=False, resident_apply=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _model(**services):
    return {"name": "demo", "services": services}


def _row(service, state="running", cid=None):
    return {"service": service, "name": f"demo-{service}-1", "id": cid or f"id-{service}",
            "state": state, "health": "", "restarts": 0, "started_at": ""}


class _InTempProject(unittest.TestCase):
    def setUp(self):
        holder = TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        previous = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)


class DefinitionDiffTests(unittest.TestCase):
    def test_key_order_does_not_count_as_a_change(self):
        from composer.stack_lifecycle import changes, definitions

        before = definitions(_model(web={"image": "a", "environment": {"A": "1", "B": "2"}}))
        after = definitions(_model(web={"environment": {"B": "2", "A": "1"}, "image": "a"}))

        self.assertEqual(changes(before, after), {"dropped": [], "changed": [], "added": []})

    def test_dropped_changed_and_added_services(self):
        from composer.stack_lifecycle import changes, definitions

        before = definitions(_model(web={"image": "a"}, redis={"command": ["redis-server", "--appendonly", "yes"]},
                                    **{"dlux-updater": {"image": "a"}}))
        after = definitions(_model(web={"image": "a"}, redis={"command": ["redis-server", "--appendonly", "no"]},
                                   worker={"image": "a"}))

        self.assertEqual(
            changes(before, after),
            {"dropped": ["dlux-updater"], "changed": ["redis"], "added": ["worker"]},
        )


class PendingMarkerTests(_InTempProject):
    def test_a_service_stays_pending_until_its_container_is_replaced(self):
        from composer.stack_lifecycle import read_pending, record_pending, still_pending

        record_pending(self.root, {"composer-agent": "c1", "composer-executor": "c2"})
        recorded = read_pending(self.root)

        self.assertEqual(still_pending(recorded, {"composer-agent": {"c1"}, "composer-executor": {"c2"}}),
                         ["composer-agent", "composer-executor"])
        # A restart keeps the container (and its old definition); a recreate replaces it.
        self.assertEqual(still_pending(recorded, {"composer-agent": {"c9"}, "composer-executor": {"c2"}}),
                         ["composer-executor"])
        self.assertEqual(still_pending(recorded, {}), [])

    def test_a_new_record_keeps_the_entries_it_does_not_replace(self):
        from composer.stack_lifecycle import read_pending, record_pending

        record_pending(self.root, {"web": "w1"})
        record_pending(self.root, {"composer-agent": "c1"})

        self.assertEqual(sorted(read_pending(self.root)), ["composer-agent", "web"])
        self.assertTrue((self.root / ".xclude" / "composer-check" / "pending-recreate.json").is_file())


class FinishFixTests(_InTempProject):
    """What `check --fix` does after the repairs it was allowed to apply."""

    BEFORE = _model(
        web={"image": "app:1", "environment": {"P": "all"}},
        caddy={"image": "caddy:2", "environment": {"DLUX_FRONT_PROXY": "private_ranges"}},
        smtp={"image": "app:1"},
        db={"image": "postgres:17"},
        **{
            "dlux-updater": {"image": "app:1"},
            "composer-agent": {"image": "composer:1", "environment": {"P": "all"}},
            "composer-executor": {"image": "composer:1", "environment": {"P": "all"}},
        },
    )
    AFTER = _model(
        web={"image": "app:1", "environment": {"P": "inline"}},
        caddy={"image": "caddy:2", "environment": {"DLUX_FRONT_PROXY": "10.0.0.5"}},
        smtp={"image": "app:1", "environment": {"X": "1"}},
        db={"image": "postgres:17"},
        **{
            "composer-agent": {"image": "composer:1", "environment": {"P": "inline"}},
            "composer-executor": {"image": "composer:1", "environment": {"P": "inline"}},
        },
    )
    ROWS = [_row("web"), _row("caddy"), _row("smtp", state="exited"), _row("db"),
            _row("dlux-updater"), _row("composer-agent"), _row("composer-executor")]

    def _launcher(self, *, recreated=(), forced=(), secrets=(True, "")):
        launcher = DockerComposeLauncher()
        launcher.services = sorted(self.BEFORE["services"])
        launcher.active_compose_files = ["compose.yml"]
        launcher._fix_recreated = set(recreated)
        launcher._fix_force_recreate = set(forced)
        self.calls = []

        def run_command(argv, timeout=None, env=None):
            self.calls.append(list(argv))
            if argv[:3] == ["docker", "ps", "-aq"]:
                return True, "cid-updater\n" if any("dlux-updater" in a for a in argv) else "", ""
            return True, "", ""

        self.recreate = Mock(return_value=(True, "", ""))
        self.wait = Mock(return_value=(True, ""))
        patches = [
            patch.object(launcher, "compose_config_json", return_value=self.AFTER),
            patch.object(launcher, "resolve_secrets", return_value=secrets),
            patch.object(launcher, "run_command", side_effect=run_command),
            patch.object(launcher, "recreate_containers", self.recreate),
            patch.object(launcher, "_wait_healthy", self.wait),
            patch.object(launcher, "_image_drift", return_value={}),
            patch("sys.stdout", new_callable=io.StringIO),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return launcher

    def _baseline(self, launcher):
        with patch.object(launcher, "compose_config_json", return_value=self.BEFORE), \
                patch.object(launcher, "_container_states", return_value=self.ROWS):
            return launcher._lifecycle_baseline()

    def test_dropped_services_are_stopped_and_removed_by_label_volumes_kept(self):
        launcher = self._launcher()
        fixes = launcher._finish_fix(_args(), self._baseline(launcher))

        removed = [f for f in fixes if f["name"] == "fix:remove-services"]
        self.assertEqual(removed[0]["level"], OK, fixes)
        self.assertIn("dlux-updater", removed[0]["message"])
        self.assertIn(["docker", "stop", "cid-updater"], self.calls)
        self.assertIn(["docker", "rm", "cid-updater"], self.calls)
        self.assertFalse(any("-v" in call or "--volumes" in call for call in self.calls))

    def test_changed_running_services_are_recreated_and_the_resident_pair_last(self):
        launcher = self._launcher()
        fixes = launcher._finish_fix(_args(), self._baseline(launcher))

        self.assertEqual(self.recreate.call_args_list[0].args[0], ["caddy", "web"])
        self.assertEqual(self.recreate.call_args_list[1].args[0], ["composer-executor", "composer-agent"])
        self.assertEqual(self.wait.call_args_list[0].args[0], ["caddy", "web"])
        self.assertTrue(any(f["name"] == "fix:recreate" and f["level"] == OK for f in fixes), fixes)
        self.assertTrue(any(f["name"] == "fix:recreate-resident" and f["level"] == OK for f in fixes), fixes)

    def test_a_stopped_service_is_left_stopped(self):
        launcher = self._launcher()
        launcher._finish_fix(_args(), self._baseline(launcher))

        recreated = [name for call in self.recreate.call_args_list for name in call.args[0]]
        self.assertNotIn("smtp", recreated)
        self.assertNotIn("db", recreated)

    def test_a_service_its_repair_recreated_is_not_recreated_again_but_is_waited_for(self):
        launcher = self._launcher(recreated={"caddy"})
        launcher._finish_fix(_args(), self._baseline(launcher))

        self.assertEqual(self.recreate.call_args_list[0].args[0], ["web"])
        self.assertEqual(sorted(self.wait.call_args_list[0].args[0]), ["caddy", "web"])

    def test_a_panel_apply_leaves_the_pair_it_runs_in_to_start_sh(self):
        launcher = self._launcher()
        fixes = launcher._finish_fix(_args(resident_apply=True), self._baseline(launcher))

        recreated = [name for call in self.recreate.call_args_list for name in call.args[0]]
        self.assertNotIn("composer-agent", recreated)
        self.assertNotIn("composer-executor", recreated)
        resident = [f for f in fixes if f["name"] == "fix:recreate-resident"][0]
        self.assertEqual(resident["level"], WARN)
        self.assertIn("./start.sh", resident["message"])
        self.assertNotIn("docker compose", resident["message"])
        from composer.stack_lifecycle import read_pending

        self.assertEqual(sorted(read_pending(self.root)), ["composer-agent", "composer-executor"])

    def test_resident_check_counts_as_a_panel_apply(self):
        launcher = self._launcher()
        launcher.resident_check = True
        launcher._finish_fix(_args(), self._baseline(launcher))

        recreated = [name for call in self.recreate.call_args_list for name in call.args[0]]
        self.assertNotIn("composer-agent", recreated)

    def test_no_recreate_changes_files_only_and_says_what_is_left(self):
        launcher = self._launcher()
        fixes = launcher._finish_fix(_args(no_recreate=True), self._baseline(launcher))

        self.recreate.assert_not_called()
        left = [f for f in fixes if f["name"] == "fix:recreate"][0]
        self.assertEqual(left["level"], WARN)
        self.assertIn("./start.sh", left["message"])
        for service in ("web", "caddy", "composer-agent"):
            self.assertIn(service, left["message"])
        # Dropped services are still removed: nothing else would ever remove them.
        self.assertIn(["docker", "rm", "cid-updater"], self.calls)
        from composer.stack_lifecycle import read_pending

        self.assertIn("web", read_pending(self.root))

    def test_a_forced_service_is_recreated_even_with_no_recreate(self):
        """Redis after an append-only-file recovery is down until it is recreated."""
        launcher = self._launcher(forced={"redis"})
        launcher._finish_fix(_args(no_recreate=True), self._baseline(launcher))

        self.assertEqual(self.recreate.call_args_list[0].args[0], ["redis"])

    def test_unreadable_secrets_refuse_the_recreate(self):
        launcher = self._launcher(secrets=(False, ".secrets/.env exists but could not be read"))
        fixes = launcher._finish_fix(_args(), self._baseline(launcher))

        self.recreate.assert_not_called()
        refused = [f for f in fixes if f["name"] == "fix:recreate"][0]
        self.assertEqual(refused["level"], FAIL)
        self.assertIn("secrets", refused["message"])
        self.assertIn("./start.sh", refused["message"])

    def test_an_unhealthy_service_fails_with_its_log_command(self):
        launcher = self._launcher()
        self.wait.return_value = (False, "web restarted twice during the health check: it is crash-looping.")
        fixes = launcher._finish_fix(_args(), self._baseline(launcher))

        failed = [f for f in fixes if f["name"] == "fix:recreate"][0]
        self.assertEqual(failed["level"], FAIL)
        self.assertIn("./start.sh logs", failed["message"])
        self.recreate.assert_called_once()  # the resident pair waits for a healthy app

    def test_a_service_running_another_image_than_its_tag_is_held_back(self):
        launcher = self._launcher()
        with patch.object(launcher, "_image_drift", return_value={"web": "app:1 now names another image"}):
            fixes = launcher._finish_fix(_args(), self._baseline(launcher))

        self.assertEqual(self.recreate.call_args_list[0].args[0], ["caddy"])
        held = [f for f in fixes if f["name"] == "fix:recreate" and f["level"] == WARN][0]
        self.assertIn("web", held["message"])
        self.assertIn("./start.sh", held["message"])

    def test_an_unreadable_model_recreates_nothing_and_says_how_to(self):
        launcher = self._launcher()
        fixes = launcher._finish_fix(_args(), None)

        self.recreate.assert_not_called()
        self.assertEqual(fixes[0]["level"], WARN)
        self.assertIn("./start.sh", fixes[0]["message"])

    def test_nothing_running_means_nothing_to_recreate(self):
        launcher = self._launcher()
        with patch.object(launcher, "compose_config_json", return_value=self.BEFORE), \
                patch.object(launcher, "_container_states", return_value=[]):
            baseline = launcher._lifecycle_baseline()
        fixes = launcher._finish_fix(_args(), baseline)

        self.recreate.assert_not_called()
        self.assertTrue(all(f["level"] != FAIL for f in fixes), fixes)


class _FakeDocker:
    """Docker and Compose for one project, answered from the Compose files on disk.

    Compose merges every ``-f`` file, and rejects the whole project when one
    names a service no file gives an image: what a stale runtime override did.
    """

    def __init__(self, live):
        self.live = dict(live)
        self.calls = []
        self.recreated = []

    def _model(self, files):
        import yaml

        services = {}
        for name in files:
            data = yaml.safe_load(Path(name).read_text(encoding="utf-8")) or {}
            for service, spec in (data.get("services") or {}).items():
                merged = services.setdefault(service, {})
                for key, value in (spec or {}).items():
                    if isinstance(value, dict) and isinstance(merged.get(key), dict):
                        merged[key] = {**merged[key], **value}
                    else:
                        merged[key] = value
        for service, spec in services.items():
            if "image" not in spec and "build" not in spec:
                return None, (f'service "{service}" has neither an image nor a build context specified: '
                              "invalid compose project")
        return {"name": "demo", "services": services}, ""

    def __call__(self, argv, timeout=None, env=None, progress_callback=None):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:2] == ["docker", "compose"]:
            rest = argv[2:]
            files = []
            while rest[:1] == ["-f"]:
                files.append(rest[1])
                rest = rest[2:]
            model, error = self._model(files)
            if model is None:
                return False, "", error
            if rest[:2] == ["config", "--services"]:
                return True, "".join(f"{name}\n" for name in model["services"]), ""
            if rest[:3] == ["config", "--format", "json"]:
                return True, json.dumps(model), ""
            if rest[:1] == ["ps"]:
                return True, json.dumps([
                    {"Service": name, "Name": f"demo-{name}-1", "ID": cid, "State": "running"}
                    for name, cid in self.live.items()
                ]), ""
            if rest[:1] == ["up"]:
                self.recreated.append([name for name in rest[1:] if not name.startswith("-")])
            return True, "", ""
        if argv[:3] == ["docker", "ps", "-aq"]:
            service = next(a.rsplit("=", 1)[1] for a in argv if a.startswith("label=com.docker.compose.service="))
            return True, (self.live[service] + "\n") if service in self.live else "", ""
        if argv[:2] == ["docker", "rm"]:
            self.live = {name: cid for name, cid in self.live.items() if cid not in argv[2:]}
        return True, "", ""


DROPPED_BEFORE = """services:
  web:
    image: app:2
    environment: {A: "1"}
  worker:
    image: app:2
  celery:
    image: app:2
  docker-socket-proxy:
    image: proxy:1
  composer-executor:
    image: composer:1
    environment: {COMPOSER_AGENT_RESTART_SERVICES: "web,worker,celery"}
  composer-agent:
    image: composer:1
    environment: {COMPOSER_AGENT_RESTART_SERVICES: "web,worker,celery"}
"""
DROPPED_AFTER = """services:
  web:
    image: app:2
    environment: {A: "1", DLUX_UPDATE_POLICY: "all"}
  worker:
    image: app:2
    environment: {DLUX_UPDATE_POLICY: "all"}
  composer-executor:
    image: composer:1
    environment: {COMPOSER_AGENT_RESTART_SERVICES: "web,worker"}
  composer-agent:
    image: composer:1
    environment: {COMPOSER_AGENT_RESTART_SERVICES: "web,worker"}
"""


class DroppedServiceRunTests(_InTempProject):
    """S1 (1.11.0b1 live proof): a `check --fix` that drops a service still brings the stack to its files.

    The runtime override was written once, at the start, naming every service then
    defined; once a repair dropped celery or docker-socket-proxy, every later
    Compose read failed, so nothing was removed or recreated and the run exited 1.
    """

    LIVE = {"web": "c-web", "worker": "c-worker", "celery": "c-celery", "docker-socket-proxy": "c-proxy",
            "composer-executor": "c-exec", "composer-agent": "c-agent"}

    def _launcher(self):
        (self.root / "compose.yml").write_text(DROPPED_BEFORE, encoding="utf-8")
        self.docker = _FakeDocker(self.LIVE)
        launcher = DockerComposeLauncher()
        stubs = {
            "_check_docker": [], "_check_wrappers": [], "_check_composer_channel": _ok("composer-channel"),
            "_check_compose_files": _ok("compose-files"), "_check_secrets": _ok("secrets"),
            "_fix_composer_pin": [], "resolve_secrets": (True, ""), "_wait_healthy": (True, ""),
        }
        for name, value in stubs.items():
            patch.object(launcher, name, return_value=value).start()
        patch.object(launcher, "run_command", side_effect=self.docker).start()
        patch.object(launcher, "run_command_streaming", side_effect=self.docker).start()
        patch("composer.app_permissions.ensure_for_image", return_value=(True, "")).start()
        patch("sys.stdout", new_callable=io.StringIO).start()
        self.addCleanup(patch.stopall)
        self.reads_after_repair = []

        def repair(args, results):
            """Like fix:celery and fix:resident-block: drop the services, then the next repair reads Compose."""
            launcher._fix_confirmed = True
            launcher._fix_baseline = launcher._lifecycle_baseline()
            self.docker.live.pop("celery")
            (self.root / "compose.yml").write_text(DROPPED_AFTER, encoding="utf-8")
            self.reads_after_repair.append(launcher.compose_config_json())
            return [{"level": OK, "name": "fix:celery", "message": "Removed celery."}]

        patch.object(launcher, "_maybe_fix", side_effect=repair).start()
        return launcher

    def _run(self, **over):
        launcher = self._launcher()
        with patch.object(type(launcher), "_check_compose_parses",
                          lambda self: _ok("compose-config") if self.discover_services(silent=True) else
                          {"level": FAIL, "name": "compose-config", "message": ""}):
            results, fixes = launcher.collect_checkup(_args(**over))
        return launcher, results, fixes

    def test_the_dropped_services_are_removed_and_the_changed_ones_recreated(self):
        launcher, results, fixes = self._run()

        self.assertIsNotNone(self.reads_after_repair[0], "a repair after the drop could not read Compose")
        by_name = {entry["name"]: entry for entry in fixes}
        self.assertEqual(by_name["fix:remove-services"]["level"], OK, fixes)
        self.assertIn("docker-socket-proxy", by_name["fix:remove-services"]["message"])
        self.assertIn(["docker", "rm", "c-proxy"], self.docker.calls)
        self.assertEqual(by_name["fix:recreate"]["level"], OK, fixes)
        self.assertEqual(by_name["fix:recreate-resident"]["level"], OK, fixes)
        self.assertEqual(self.docker.recreated, [["web", "worker"], ["composer-executor", "composer-agent"]])
        self.assertNotIn(FAIL, [entry["level"] for entry in results + fixes])
        # The run reads the stack it left, not the one it started from.
        self.assertEqual(sorted(launcher.services), ["composer-agent", "composer-executor", "web", "worker"])

    def test_the_override_names_only_services_the_files_define(self):
        import yaml

        launcher, _results, _fixes = self._run()

        override = yaml.safe_load(launcher.compose_runtime_override.read_text(encoding="utf-8"))
        self.assertEqual(sorted(override["services"]), ["composer-agent", "composer-executor", "web", "worker"])
        for call in self.docker.calls:
            if call[:2] == ["docker", "compose"] and call[-1] == "--services":
                self.assertNotIn(str(launcher.compose_runtime_override), call, "read through the override")

    def test_a_panel_apply_removes_and_recreates_all_but_the_pair(self):
        _launcher, _results, fixes = self._run(resident_apply=True)

        by_name = {entry["name"]: entry for entry in fixes}
        self.assertEqual(by_name["fix:remove-services"]["level"], OK, fixes)
        self.assertEqual(by_name["fix:recreate"]["level"], OK, fixes)
        self.assertEqual(by_name["fix:recreate-resident"]["level"], WARN, fixes)
        self.assertEqual(self.docker.recreated, [["web", "worker"]])


class ImageDriftTests(unittest.TestCase):
    def test_a_tag_that_moved_since_the_container_started_is_reported(self):
        launcher = DockerComposeLauncher()
        before = _model(web={"image": "app:1"}, caddy={"image": "caddy:2"})
        baseline = {"images": {"web": "app:1", "caddy": "caddy:2"},
                    "containers": [_row("web", cid="w1"), _row("caddy", cid="c1")]}

        def run_command(argv, timeout=None, env=None):
            if argv[:2] == ["docker", "inspect"]:
                return True, "w1 sha256:old\nc1 sha256:caddy\n", ""
            if argv[:3] == ["docker", "image", "inspect"]:
                return True, {"app:1": "sha256:new", "caddy:2": "sha256:caddy"}[argv[-1]] + "\n", ""
            return False, "", ""

        with patch.object(launcher, "run_command", side_effect=run_command):
            held = launcher._image_drift(["web", "caddy"], baseline, before)

        self.assertEqual(list(held), ["web"])

    def test_a_service_the_repairs_add_is_not_started_on_an_image_the_stack_does_not_run(self):
        """S2: pull, check --fix, deploy. worker waits for the deploy of the pulled image."""
        launcher = DockerComposeLauncher()
        after = _model(web={"image": "app:2"}, worker={"image": "app:2"})
        baseline = {"images": {"web": "app:2", "celery": "app:2"},
                    "containers": [_row("web", cid="w1"), _row("celery", cid="k1")]}

        def run_command(argv, timeout=None, env=None):
            if argv[:2] == ["docker", "inspect"]:
                return True, "w1 sha256:old\nk1 sha256:old\n", ""
            if argv[:3] == ["docker", "image", "inspect"]:
                return True, {"app:2": "sha256:new"}[argv[-1]] + "\n", ""
            return False, "", ""

        with patch.object(launcher, "run_command", side_effect=run_command):
            held = launcher._image_drift(["worker"], baseline, after)

        self.assertEqual(list(held), ["worker"])
        self.assertIn("than the one web runs", held["worker"])

    def test_a_service_the_repairs_add_on_the_image_the_stack_runs_is_started(self):
        launcher = DockerComposeLauncher()
        after = _model(web={"image": "app:2"}, worker={"image": "app:2"})
        baseline = {"images": {"web": "app:2"}, "containers": [_row("web", cid="w1")]}

        def run_command(argv, timeout=None, env=None):
            if argv[:2] == ["docker", "inspect"]:
                return True, "w1 sha256:same\n", ""
            if argv[:3] == ["docker", "image", "inspect"]:
                return True, "sha256:same\n", ""
            return False, "", ""

        with patch.object(launcher, "run_command", side_effect=run_command):
            self.assertEqual(launcher._image_drift(["worker"], baseline, after), {})


def _ok(name):
    return {"level": OK, "name": name, "message": ""}


class CollectWiringTests(unittest.TestCase):
    def _launcher(self):
        launcher = DockerComposeLauncher()
        launcher.services = []
        stubs = {
            "_check_docker": [], "_check_wrappers": [], "_check_composer_channel": _ok("composer-channel"),
            "_check_compose_files": _ok("compose-files"), "_check_compose_parses": _ok("compose-config"),
            "_check_secrets": _ok("secrets"), "resolve_active_compose_files": None,
            "discover_services": True, "_fix_composer_pin": [],
        }
        for name, value in stubs.items():
            patch.object(launcher, name, return_value=value).start()
        hold = patch("composer.checkup.check_lock.hold").start()
        hold.return_value.__enter__ = lambda *a: None
        hold.return_value.__exit__ = lambda *a: False
        self.addCleanup(patch.stopall)
        return launcher

    def test_the_lifecycle_runs_after_a_confirmed_fix(self):
        launcher = self._launcher()

        def confirmed(args, results):
            self.assertTrue(launcher._capture_lifecycle, "collect_checkup asks for the baseline")
            launcher._fix_confirmed = True
            launcher._fix_baseline = {"marker": 1}
            return [{"level": OK, "name": "fix:front-proxy", "message": "done"}]

        with patch.object(launcher, "_maybe_fix", side_effect=confirmed), \
                patch.object(launcher, "_finish_fix", return_value=[]) as finish:
            launcher.collect_checkup(_args())

        finish.assert_called_once()
        self.assertEqual(finish.call_args.args[1], {"marker": 1})

    def test_a_declined_fix_runs_no_lifecycle(self):
        launcher = self._launcher()
        with patch.object(launcher, "_maybe_fix", return_value=[{"level": WARN, "name": "fix", "message": "Changes declined."}]), \
                patch.object(launcher, "_finish_fix") as finish:
            launcher.collect_checkup(_args())

        finish.assert_not_called()

    def test_maybe_fix_takes_the_baseline_after_the_confirmation_only(self):
        launcher = DockerComposeLauncher()
        launcher.services = ["web", "composer-agent", "composer-executor"]
        launcher.active_compose_files = ["compose.yml"]
        launcher._capture_lifecycle = True
        order = []

        def labels(path, compose_file="", apply=False, **kw):
            order.append("apply" if apply else "plan")
            return {"files": ["compose.yml"], "backup_root": "/x"}

        def confirm(*a, **k):
            order.append("confirm")
            return True

        def baseline():
            order.append("baseline")
            return {"marker": 1}

        with (
            patch("composer.agent_installer.normalize_restart_labels", side_effect=labels),
            patch("composer.agent_installer.enable_executor", return_value={"files": []}),
            patch("composer.agent_installer.enable_post_start_label", return_value={"files": []}),
            patch("composer.checkup.inspect_legacy_proxy_routes", return_value={"unsupported": [], "recognized": []}),
            patch("composer.wrappers.inspect_wrappers", return_value=[]),
            patch("composer.checkup.confirm", side_effect=confirm),
            patch.object(launcher, "_lifecycle_baseline", side_effect=baseline),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            launcher._maybe_fix(_args(), [])

        self.assertEqual(order[-3:], ["confirm", "baseline", "apply"])
        self.assertEqual(launcher._fix_baseline, {"marker": 1})
        self.assertTrue(launcher._fix_confirmed)


class ConfirmationTests(unittest.TestCase):
    """The one confirmation lists the removals, the recreations and the restart window."""

    def _consequences(self, **args):
        launcher = DockerComposeLauncher()
        launcher.services = ["web", "celery", "dlux-updater", "composer-agent", "composer-executor"]
        launcher.active_compose_files = ["compose.yml"]
        seen = {}

        def confirm(action, consequences, assume_yes=False):
            seen["lines"] = list(consequences)
            return False

        with (
            patch.object(launcher, "run_command", return_value=(True, "5.3.1", "")),
            patch.object(launcher, "_dlux_runtime_version", return_value=(1, 8, 0)),
            patch("composer.agent_installer.migrate_dlux_init_containers", return_value={"files": ["compose.yml"]}),
            patch.object(launcher, "build_compose_env", return_value={}),
            patch.object(launcher, "plaintext_env_candidates", return_value=[]),
            patch("composer.wrappers.inspect_wrappers", return_value=[]),
            patch("composer.checkup.inspect_legacy_proxy_routes", return_value={"recognized": [], "unsupported": []}),
            patch("composer.agent_installer.enable_post_start_label", return_value={"files": []}),
            patch("composer.agent_installer.migrate_dlux_updater", return_value={"files": []}),
            patch("composer.agent_installer.enable_executor", return_value={"files": []}),
            patch("composer.checkup.confirm", side_effect=confirm),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            launcher._maybe_fix(_args(**args), [])
        return "\n".join(seen["lines"])

    def test_it_names_the_removed_service_and_the_recreations(self):
        text = self._consequences()

        self.assertIn("Stop and remove", text)
        self.assertIn("dlux-updater", text)
        self.assertIn("Volumes are kept", text)
        self.assertIn("Recreate", text)
        self.assertIn("celery", text)
        self.assertIn("web", text)
        self.assertIn("Restart window", text)

    def test_no_recreate_says_the_services_keep_their_definition(self):
        text = self._consequences(no_recreate=True)

        self.assertIn("--no-recreate", text)
        self.assertIn("./start.sh", text)
        self.assertNotIn("Restart window", text)


class StartShHintTests(unittest.TestCase):
    """Every remaining step names a ./start.sh command, never raw Compose."""

    def test_every_stack_migration_hint_is_a_start_sh_command(self):
        from composer import agent_installer
        from tests.test_dlux_init_container_migration import DEPLOYED

        with TemporaryDirectory() as root:
            Path(root, "compose.yml").write_text(DEPLOYED, encoding="utf-8")
            for helper in ("migrate_dlux_init_containers", "enable_post_start_label"):
                outcome = getattr(agent_installer, helper)(root)
                if outcome["command"]:
                    self.assertTrue(outcome["command"].startswith("./start.sh"), (helper, outcome["command"]))
        source = Path(agent_installer.__file__).read_text(encoding="utf-8")
        self.assertNotIn('redeploy_command="docker compose', source)
        self.assertNotIn('"docker compose up -d', source)
        self.assertNotIn('"docker compose -f compose.yml -f compose.dev.yml up -d"', source)

    def test_fix_messages_carry_no_raw_compose_command(self):
        from composer import checkup

        source = Path(checkup.__file__).read_text(encoding="utf-8")
        self.assertNotIn("Apply with '{outcome.get('command')}'", source)
        self.assertNotIn("Start them with '{outcome.get('command')}'", source)
        self.assertNotIn("Recreate it with: {outcome['command']}", source)


class DroppedServiceRemovalTests(unittest.TestCase):
    """A repair that drops a service stops and removes its container before writing."""

    def _run(self, runner):
        from composer.agent_installer import migrate_dlux_init_containers
        from tests.test_dlux_init_container_migration import DEPLOYED

        holder = TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        root = Path(holder.name)
        (root / "compose.yml").write_text(DEPLOYED, encoding="utf-8")
        self.root = root
        with patch("composer.agent_installer.shutil.which", return_value="/usr/bin/docker"):
            return migrate_dlux_init_containers(str(root), apply=True, command_runner=runner)

    def test_retiring_dlux_updater_removes_its_container_first(self):
        seen = []

        def runner(argv, **kwargs):
            text = (self.root / "compose.yml").read_text(encoding="utf-8")
            seen.append((list(argv), "  dlux-updater:\n" in text))
            return Mock(returncode=0, stderr="", stdout="")

        self._run(runner)

        removals = [(argv, defined) for argv, defined in seen if "rm" in argv]
        self.assertEqual(len(removals), 1, seen)
        argv, still_defined = removals[0]
        self.assertEqual(argv[-4:], ["rm", "-s", "-f", "dlux-updater"])
        self.assertIn("demoproj", argv)
        self.assertTrue(still_defined, "removed while the file still names the service")
        self.assertNotIn("  dlux-updater:\n", (self.root / "compose.yml").read_text(encoding="utf-8"))

    def test_a_failed_removal_writes_nothing(self):
        from composer.agent_installer import AgentInstallError
        from tests.test_dlux_init_container_migration import DEPLOYED

        def runner(argv, **kwargs):
            return Mock(returncode=1 if "rm" in argv else 0, stderr="daemon gone", stdout="")

        with self.assertRaises(AgentInstallError) as caught:
            self._run(runner)
        self.assertIn("no project files were changed", str(caught.exception))
        self.assertEqual((self.root / "compose.yml").read_text(encoding="utf-8"), DEPLOYED)


class CliAndExecutorTests(unittest.TestCase):
    def test_check_takes_no_recreate(self):
        from composer.cli import parse_check_args

        self.assertTrue(parse_check_args(["--fix", "--no-recreate"]).no_recreate)
        self.assertFalse(parse_check_args(["--fix"]).no_recreate)

    def test_the_panel_apply_marks_itself_resident(self):
        from composer import executor_ops

        launcher = Mock()
        with patch("composer.launcher.DockerComposeLauncher", return_value=launcher), \
                patch("composer.ops.compose_digest", return_value="d" * 64), \
                patch("composer.dlux_runtime_access.secret_flags", return_value=[]), \
                patch("composer.dlux_runtime_access.self_image", return_value="debeski/composer:test"), \
                patch.object(executor_ops, "_run", return_value=(0, "")) as run, \
                patch.object(executor_ops, "_op_env", return_value={}):
            executor_ops._run_check_fix("op-1", {"compose_digest": "d" * 64})

        argv = run.call_args.args[0]
        self.assertEqual(argv[-4:], ["check", "--fix", "-y", "--resident-apply"])


class PendingRecreateFindingTests(_InTempProject):
    def test_it_warns_until_the_containers_are_replaced(self):
        from composer.stack_lifecycle import record_pending

        record_pending(self.root, {"composer-agent": "c1", "composer-executor": "c2"})
        launcher = DockerComposeLauncher()
        with patch.object(launcher, "_container_states",
                          return_value=[_row("composer-agent", cid="c1"), _row("composer-executor", cid="c2")]):
            finding = launcher._check_pending_recreate()
        self.assertEqual(finding["level"], WARN)
        self.assertEqual(finding["name"], "pending-recreate")
        self.assertIn("composer-agent", finding["message"])
        self.assertIn("./start.sh", finding["fix"])

        with patch.object(launcher, "_container_states",
                          return_value=[_row("composer-agent", cid="n1"), _row("composer-executor", cid="n2")]):
            self.assertIsNone(launcher._check_pending_recreate())

    def test_no_marker_no_finding(self):
        launcher = DockerComposeLauncher()
        with patch.object(launcher, "_container_states", side_effect=AssertionError("not asked")):
            self.assertIsNone(launcher._check_pending_recreate())


class PreviewLifecycleTests(unittest.TestCase):
    """A panel apply runs the removals and recreations, so its preview lists them first."""

    def test_the_preview_lists_removals_and_recreations(self):
        from composer.ops import _dry_run_repairs

        launcher = SimpleNamespace(
            active_compose_files=["compose.yml"], _maintenance_compose_files=lambda: ["compose.yml"],
            planned_lifecycle=lambda args, results: [
                {"name": "remove-services", "files": [], "diff": "", "note": "Stops and removes dlux-updater."},
                {"name": "recreate-services", "files": [], "diff": "", "note": "Recreates celery and web."},
            ],
        )
        with TemporaryDirectory() as root:
            previous = os.getcwd()
            os.chdir(root)
            try:
                Path("compose.yml").write_text("name: demo\nservices:\n  web:\n    image: a\n", encoding="utf-8")
                with patch("composer.agent_installer.enable_agent", return_value={"files": []}), \
                        patch("composer.agent_installer.enable_executor", return_value={"files": []}), \
                        patch("composer.agent_installer.enable_post_start_label", return_value={"files": []}), \
                        patch("composer.agent_installer.migrate_dlux_updater", return_value={"files": []}), \
                        patch("composer.agent_installer.normalize_restart_labels", return_value={"files": []}):
                    repairs, _digest = _dry_run_repairs(launcher, SimpleNamespace(file=None), [])
            finally:
                os.chdir(previous)
        names = [repair["name"] for repair in repairs]
        self.assertEqual(names[-2:], ["remove-services", "recreate-services"])
        self.assertEqual(repairs[-1]["files"], [])

    def test_planned_lifecycle_names_the_pair_as_left_to_start_sh(self):
        launcher = DockerComposeLauncher()
        launcher.services = ["web", "worker", "composer-agent", "composer-executor"]
        plan = {"pending": True, "refused": False, "update_policy_files": ["compose.yml"]}
        with patch.object(launcher, "_fix_plan", return_value=plan):
            entries = launcher.planned_lifecycle(_args(fix=False), [])

        recreate = [e for e in entries if e["name"] == "recreate-services"][0]
        self.assertIn("web", recreate["note"])
        self.assertIn("worker", recreate["note"])
        self.assertIn("./start.sh", recreate["note"])
        self.assertEqual(recreate["files"], [])



class SharedEnvironmentExpectationTests(_InTempProject):
    """1.11.0b1 live proof N-4: the confirmation omitted db, which merges x-environment on older stacks."""

    COMPOSE = (
        "name: demo\n"
        "x-environment:\n"
        "  # Database settings\n"
        "  &de\n"
        '  DLUX_STACK_SCHEMA: "4"\n'
        "services:\n"
        "  db:\n    image: postgres:17\n    environment:\n      <<: *de\n"
        "  web:\n    image: app\n    environment:\n      <<: *de\n"
        "  worker:\n    image: app\n    environment:\n      <<: *de\n"
        "  smtp-relay:\n    image: app\n"
        "  composer-agent:\n    image: debeski/composer:beta\n"
        "  composer-executor:\n    image: debeski/composer:beta\n"
    )

    def _launcher(self):
        Path("compose.yml").write_text(self.COMPOSE, encoding="utf-8")
        launcher = DockerComposeLauncher()
        launcher.resolve_active_compose_files()
        launcher.services = ["db", "web", "worker", "smtp-relay", "composer-agent", "composer-executor"]
        calls = []

        def compose(args, timeout=None):
            calls.append((list(args), list(launcher.active_compose_files)))
            text = Path(launcher.active_compose_files[0]).read_text(encoding="utf-8")
            shared = {"DLUX_STACK_SCHEMA": "4"}
            if launcher._SHARED_PROBE_KEY in text:
                shared[launcher._SHARED_PROBE_KEY] = "1"
            services = {name: {"image": "app", "environment": dict(shared)} for name in ("db", "web", "worker")}
            services.update({name: {"image": "x"} for name in ("smtp-relay", "composer-agent", "composer-executor")})
            return True, json.dumps({"name": "demo", "services": services}), ""

        return launcher, calls, compose

    def test_the_x_environment_mergers_come_from_the_model(self):
        launcher, calls, compose = self._launcher()
        with patch.object(launcher, "run_docker_compose", side_effect=compose), \
                patch.object(launcher, "resolve_secrets", return_value=(True, "")):
            shared = launcher._shared_environment_services()
        self.assertEqual(shared, {"db", "web", "worker"})
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(args[:2] == ["--project-directory", str(self.root.resolve())] for args, _ in calls))
        self.assertEqual(launcher.active_compose_files, ["compose.yml"], "the file list is restored")
        self.assertEqual(Path("compose.yml").read_text(encoding="utf-8"), self.COMPOSE, "the project is untouched")

    def test_the_confirmation_lists_db_when_the_update_policy_line_changes_it(self):
        launcher, _, compose = self._launcher()
        plan = {"pending": True, "refused": False, "update_policy_files": ["compose.yml"]}
        with patch.object(launcher, "run_docker_compose", side_effect=compose), \
                patch.object(launcher, "resolve_secrets", return_value=(True, "")):
            life = launcher._lifecycle_expectation(plan)
            with patch.object(launcher, "_fix_plan", return_value=plan):
                entries = launcher.planned_lifecycle(_args(fix=False), [])
        self.assertEqual(life["recreate"], ["db", "web", "worker"])
        note = [e for e in entries if e["name"] == "recreate-services"][0]["note"]
        self.assertIn("expected: db, web and worker", note)

    def test_without_a_readable_model_the_fixed_list_is_used(self):
        launcher, _, _ = self._launcher()
        with patch.object(launcher, "run_docker_compose", return_value=(False, "", "boom")), \
                patch.object(launcher, "resolve_secrets", return_value=(True, "")):
            self.assertEqual(launcher._shared_environment_services(), set(launcher._SHARED_ENVIRONMENT))


if __name__ == "__main__":
    unittest.main()
