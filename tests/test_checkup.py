import io
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from composer.checkup import FAIL, OK, WARN
from composer.cli import parse_check_args
from composer.launcher import DockerComposeLauncher
from composer.stack_cleanup import StackCleanupError


def _args(**over):
    base = dict(
        file=None,
        dev=False,
        fix=False,
        yes=False,
        deep=False,
        deep_service="web",
        deep_command="python manage.py dlux_doctor",
        json=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


class CheckArgTests(unittest.TestCase):
    def test_defaults(self):
        args = parse_check_args([])
        self.assertFalse(args.fix)
        self.assertFalse(args.deep)
        self.assertEqual(args.deep_service, "web")
        self.assertEqual(args.deep_command, "python manage.py dlux_doctor")


class CheckupCheckTests(unittest.TestCase):
    def setUp(self):
        self.launcher = DockerComposeLauncher()
        self.launcher.active_compose_files = ["compose.yml"]

    def test_secrets_missing_file_is_a_warning(self):
        with patch.object(self.launcher, "plaintext_env_candidates", return_value=[]):
            result = self.launcher._check_secrets()
        self.assertEqual(result["level"], WARN)

    def test_secrets_empty_file_fails(self):
        with (
            patch.object(self.launcher, "plaintext_env_candidates", return_value=["/x/.env"]),
            patch.object(self.launcher, "parse_env_file", return_value={}),
        ):
            result = self.launcher._check_secrets()
        self.assertEqual(result["level"], FAIL)

    def test_secrets_unreadable_file_fails(self):
        with (
            patch.object(self.launcher, "plaintext_env_candidates", return_value=["/x/.env"]),
            patch.object(self.launcher, "parse_env_file", side_effect=OSError("denied")),
        ):
            result = self.launcher._check_secrets()
        self.assertEqual(result["level"], FAIL)

    def test_missing_required_vars_fail_and_list_names(self):
        self.launcher.services = ["web"]
        with (
            patch.object(self.launcher, "required_compose_vars", return_value={"SECRET_KEY", "DB_PASSWORD"}),
            patch.object(self.launcher, "plaintext_env_candidates", return_value=[]),
            patch.object(self.launcher, "inherited_secret_keys", return_value=[]),
            patch.dict(os.environ, {}, clear=True),
        ):
            result = self.launcher._check_required_vars()
        self.assertEqual(result["level"], FAIL)
        self.assertIn("DB_PASSWORD", result["message"])
        self.assertIn("SECRET_KEY", result["message"])

    def test_required_vars_satisfied_by_env_and_secrets(self):
        with (
            patch.object(self.launcher, "required_compose_vars", return_value={"SECRET_KEY", "DB_PASSWORD"}),
            patch.object(self.launcher, "plaintext_env_candidates", return_value=["/x/.env"]),
            patch.object(self.launcher, "parse_env_file", return_value={"DB_PASSWORD": "x"}),
            patch.object(self.launcher, "inherited_secret_keys", return_value=[]),
            patch.dict(os.environ, {"SECRET_KEY": "y"}, clear=True),
        ):
            result = self.launcher._check_required_vars()
        self.assertEqual(result["level"], OK)

    def test_topology_legacy_is_a_fixable_warning(self):
        self.launcher.services = ["web", "composer-updater", "docker-socket-proxy"]
        result = self.launcher._check_topology()
        self.assertEqual(result["level"], WARN)
        self.assertIn("agent enable", result["fix"])

    def test_topology_agent_without_proxy_warns(self):
        self.launcher.services = ["web", "composer-agent"]
        result = self.launcher._check_topology()
        self.assertEqual(result["level"], WARN)

    def test_topology_agent_without_executor_warns_to_harden(self):
        # A functional but un-hardened agent stack: WARN (non-blocking, never
        # FAILs), surfacing the hint and committing to check --fix / executor enable.
        self.launcher.services = ["web", "composer-agent", "docker-socket-proxy"]
        result = self.launcher._check_topology()
        self.assertEqual(result["level"], WARN)
        self.assertIn("composer-executor", result["fix"])
        self.assertIn("check --fix", result["fix"])
        self.assertIn("executor enable", result["fix"])
        self.assertIn("executor-hardening", result["fix"])

    def test_topology_hardened_pair_is_ok_and_labeled(self):
        self.launcher.services = ["web", "composer-agent", "composer-executor", "docker-socket-proxy"]
        result = self.launcher._check_topology()
        self.assertEqual(result["level"], OK)
        self.assertIn("Hardened", result["message"])
        self.assertIn("composer-executor holds Docker authority", result["message"])
        self.assertNotIn("fix", result.get("fix", ""))  # nothing to nudge

    def test_topology_hardened_without_proxy_is_ok(self):
        self.launcher.services = ["web", "composer-agent", "composer-executor"]
        result = self.launcher._check_topology()
        self.assertEqual(result["level"], OK)
        self.assertIn("Hardened", result["message"])

    def test_topology_executor_without_agent_is_incomplete(self):
        self.launcher.services = ["web", "composer-executor", "docker-socket-proxy"]
        result = self.launcher._check_topology()
        self.assertEqual(result["level"], WARN)
        self.assertIn("incomplete", result["message"])

    def test_mixed_agent_and_legacy_topology_is_blocking(self):
        self.launcher.services = [
            "web",
            "composer-agent",
            "composer-updater",
            "docker-socket-proxy",
        ]
        result = self.launcher._check_topology()
        self.assertEqual(result["level"], FAIL)
        self.assertIn("Conflicting", result["message"])

    def test_removed_services_are_a_fixable_warning(self):
        self.launcher.services = ["web", "pgadmin", "db-backup", "db_backup"]
        result = self.launcher._check_removed_services()
        self.assertEqual(result["level"], WARN)
        self.assertIn("db-backup", result["message"])
        self.assertIn("db_backup", result["message"])
        self.assertIn("pgadmin", result["message"])
        self.assertIn("check --fix", result["fix"])

    def test_stack_without_removed_services_is_ok(self):
        self.launcher.services = ["web", "db", "composer-agent"]
        self.assertEqual(self.launcher._check_removed_services()["level"], OK)

    def test_legacy_proxy_routes_are_a_fixable_warning(self):
        with patch(
            "composer.checkup.inspect_legacy_proxy_routes",
            return_value={
                "recognized": [".proxy/Caddyfile"],
                "unsupported": [],
            },
        ):
            result = self.launcher._check_proxy_routes()
        self.assertEqual(result["level"], WARN)
        self.assertIn(".proxy/Caddyfile", result["message"])

    def test_version_drift_between_deployer_and_resident_warns(self):
        self.launcher.services = ["composer-agent"]
        self.launcher.composer_version = "1.2.5"
        with patch.object(self.launcher, "run_docker_compose", return_value=(True, "1.2.3\n", "")):
            result = self.launcher._check_versions()
        self.assertEqual(result["level"], WARN)
        self.assertIn("1.2.3", result["message"])

    def test_version_resident_unavailable_is_ok(self):
        self.launcher.services = ["composer-agent"]
        with patch.object(self.launcher, "run_docker_compose", return_value=(False, "", "no container")):
            result = self.launcher._check_versions()
        self.assertEqual(result["level"], OK)


class DluxUpdaterExecutorCheckTests(unittest.TestCase):
    """Is the stack ready for DjangoLux to hand its updates to Composer?

    The `dlux-updater` SERVICE is never proposed for removal. It also runs
    `dlux_reconcile` and `migrator`, `web` gates its start on its health, and it
    is the queue worker that writes the hand-off. What 1.9.0 deletes is the
    executor code inside it.
    """

    def setUp(self):
        self.launcher = DockerComposeLauncher()
        self.launcher.active_compose_files = ["compose.yml"]

    def _check(self, services, runtime, mounted=True):
        self.launcher.services = services
        with patch.object(self.launcher, "_dlux_runtime_version", return_value=runtime), \
             patch.object(self.launcher, "_mounts_dlux_runtime", return_value=mounted):
            return self.launcher._check_dlux_updater_executor()

    def test_ok_when_the_service_is_absent(self):
        result = self._check(["web", "celery"], (1, 8, 0))
        self.assertEqual(result["level"], OK)

    def test_a_ready_stack_is_ok_and_never_proposes_removing_the_service(self):
        result = self._check(["web", "dlux-updater", "composer-executor"], (1, 8, 0))

        self.assertEqual(result["level"], OK)
        self.assertIn("composer-executor", result["message"])
        self.assertNotIn("fix", result)

    def test_no_composer_loop_means_a_request_would_never_be_executed(self):
        """Composer is required for DjangoLux updates, not an optional companion."""
        result = self._check(["web", "dlux-updater"], (1, 8, 0))

        self.assertEqual(result["level"], FAIL)
        self.assertIn("never be executed", result["message"])
        self.assertIn("--fix", result["fix"])

    def test_a_loop_without_the_runtime_volume_fails(self):
        """It cannot read the request or publish availability — silently."""
        result = self._check(
            ["web", "dlux-updater", "composer-agent"], (1, 8, 0), mounted=False)

        self.assertEqual(result["level"], FAIL)
        self.assertIn("dlux_runtime", result["message"])

    def test_an_older_runtime_is_ok_and_told_nothing_needs_changing(self):
        """Running --fix before the 1.8.0 upgrade must be a no-op, not a warning."""
        result = self._check(["web", "dlux-updater", "composer-executor"], (1, 7, 1))

        self.assertEqual(result["level"], OK)
        self.assertIn("Nothing to change now", result["message"])

    def test_warns_when_the_runtime_cannot_be_determined(self):
        result = self._check(["web", "dlux-updater"], None)

        self.assertEqual(result["level"], WARN)
        self.assertIn("could not be read", result["message"])

    def test_the_executor_is_preferred_over_the_agent_as_the_loop(self):
        self.launcher.services = ["dlux-updater", "composer-agent", "composer-executor"]
        self.assertEqual(self.launcher._package_loop_service(), "composer-executor")


class InitContainerFixGatingTests(unittest.TestCase):
    """When --fix may retire dlux-updater, and when it must refuse.

    Applying this on a host whose Compose ignores `pre_start` would remove the
    service that runs migrations and replace it with steps that never execute —
    the stack would boot unmigrated. Applying it on an image whose DjangoLux
    still performs its own updates would remove its only update path.
    """

    def _fixes(self, *, services, compose="5.3.1", dlux=(1, 8, 0), migration_files=("compose.yml",)):
        launcher = DockerComposeLauncher()
        launcher.services = list(services)
        launcher.active_compose_files = ["compose.yml"]
        args = _args(fix=True, yes=True)
        with (
            patch.object(launcher, "run_command", return_value=(True, compose, "")),
            patch.object(launcher, "_dlux_runtime_version", return_value=dlux),
            patch("composer.agent_installer.migrate_dlux_init_containers",
                  return_value={"files": list(migration_files), "command": "docker compose up -d"}),
            patch.object(launcher, "build_compose_env", return_value={}),
            patch.object(launcher, "plaintext_env_candidates", return_value=[]),
            patch("composer.wrappers.inspect_wrappers", return_value=[]),
            patch("composer.checkup.inspect_legacy_proxy_routes",
                  return_value={"recognized": [], "unsupported": []}),
            patch("composer.agent_installer.enable_post_start_label", return_value={"files": []}),
            patch("composer.agent_installer.migrate_dlux_updater", return_value={"files": []}),
            patch("composer.agent_installer.enable_executor", return_value={"files": []}),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            return launcher._maybe_fix(args, [])

    def _names(self, fixes):
        return {entry["name"] for entry in fixes}

    def test_a_ready_stack_is_migrated(self):
        fixes = self._fixes(services=["web", "celery", "dlux-updater", "composer-agent",
                                      "composer-executor"])
        self.assertIn("fix:dlux-init-containers", self._names(fixes))

    def test_an_old_compose_plugin_blocks_it(self):
        """It would ignore pre_start and boot the stack unmigrated."""
        fixes = self._fixes(
            services=["web", "celery", "dlux-updater", "composer-agent", "composer-executor"],
            compose="2.29.7")

        self.assertNotIn("fix:dlux-init-containers", self._names(fixes))
        blocked = [f for f in fixes if f["name"] == "dlux-init-containers"]
        self.assertTrue(blocked)
        self.assertIn("unmigrated", blocked[0]["message"])

    def test_an_older_dlux_image_blocks_it(self):
        """That service is still the deployment's only update path."""
        fixes = self._fixes(
            services=["web", "celery", "dlux-updater", "composer-agent", "composer-executor"],
            dlux=(1, 7, 1))

        self.assertNotIn("fix:dlux-init-containers", self._names(fixes))
        blocked = [f for f in fixes if f["name"] == "dlux-init-containers"]
        self.assertIn("still owns the update path", blocked[0]["message"])

    def test_an_unreadable_dlux_version_blocks_it(self):
        fixes = self._fixes(
            services=["web", "celery", "dlux-updater", "composer-agent", "composer-executor"],
            dlux=None)

        self.assertNotIn("fix:dlux-init-containers", self._names(fixes))

    def test_a_stack_without_the_service_is_left_alone(self):
        fixes = self._fixes(services=["web", "celery", "composer-agent", "composer-executor"])

        self.assertNotIn("fix:dlux-init-containers", self._names(fixes))
        self.assertNotIn("dlux-init-containers", self._names(fixes))

    def test_an_already_migrated_stack_reports_no_change(self):
        """The transform is a no-op, so it must not be offered."""
        fixes = self._fixes(
            services=["web", "celery", "dlux-updater", "composer-agent", "composer-executor"],
            migration_files=())

        self.assertNotIn("fix:dlux-init-containers", self._names(fixes))


class ComposeVersionFloorTests(unittest.TestCase):
    """Init containers landed in Compose 5.3.0.

    An older plugin ignores the `pre_start` key rather than rejecting it, so the
    stack would come up with no migrations applied and every gated service
    waiting forever. Nothing else surfaces that before a deploy.
    """

    def setUp(self):
        self.launcher = DockerComposeLauncher()

    def _result(self, raw):
        return self.launcher._compose_version_result(raw)

    def test_a_supported_version_is_ok(self):
        result = self._result("5.3.1")

        self.assertEqual(result["level"], OK)
        self.assertIn("5.3.1", result["message"])

    def test_the_v_prefix_is_tolerated(self):
        self.assertEqual(self._result("v5.3.1")["level"], OK)

    def test_a_prerelease_suffix_is_tolerated(self):
        self.assertEqual(self._result("5.3.0-rc.2")["level"], OK)

    def test_an_older_plugin_fails(self):
        result = self._result("2.29.7")

        self.assertEqual(result["level"], FAIL)
        self.assertIn("never start", result["message"])
        self.assertIn("5.3.0", result["fix"])

    def test_the_boundary_version_is_accepted(self):
        self.assertEqual(self._result("5.3.0")["level"], OK)

    def test_the_version_just_below_is_refused(self):
        self.assertEqual(self._result("5.2.9")["level"], FAIL)

    def test_unreadable_output_warns_rather_than_guessing(self):
        for raw in ("", "   ", "not-a-version"):
            with self.subTest(raw=raw):
                self.assertEqual(self._result(raw)["level"], WARN)

    def test_a_two_part_version_means_dot_zero(self):
        """(5, 3) compares as LESS than (5, 3, 0), so it must be padded."""
        self.assertEqual(self.launcher._parse_compose_version("5.3"), (5, 3, 0))
        self.assertEqual(self._result("5.3")["level"], OK)

    def test_a_single_component_version_is_padded_too(self):
        self.assertEqual(self.launcher._parse_compose_version("6"), (6, 0, 0))
        self.assertEqual(self._result("6")["level"], OK)


class UnmanagedStackTopologyTests(unittest.TestCase):
    """A DjangoLux stack with no Composer service has no update path at all."""

    def _topology(self, services):
        launcher = DockerComposeLauncher()
        launcher.services = services
        return launcher._check_topology()

    def test_a_stack_with_no_composer_service_fails(self):
        result = self._topology(["web", "celery", "db", "dlux-updater"])

        self.assertEqual(result["level"], FAIL)
        self.assertIn("no update path", result["message"])
        self.assertIn("--fix", result["fix"])

    def test_the_hardened_trio_is_still_ok(self):
        result = self._topology(
            ["web", "composer-agent", "composer-executor", "docker-socket-proxy"])
        self.assertEqual(result["level"], OK)


class DluxRuntimeMountTests(unittest.TestCase):
    def setUp(self):
        self.launcher = DockerComposeLauncher()

    def _mounts(self, definition):
        model = json.dumps({"services": {"composer-executor": definition}})
        with patch.object(self.launcher, "run_docker_compose", return_value=(True, model, "")):
            return self.launcher._mounts_dlux_runtime("composer-executor")

    def test_a_named_volume_mount_is_recognized(self):
        self.assertTrue(self._mounts(
            {"volumes": [{"type": "volume", "source": "dlux_runtime",
                          "target": "/opt/dlux-runtime"}]}))

    def test_a_project_prefixed_volume_name_is_recognized(self):
        """`docker compose config` reports the volume with the project prefix."""
        self.assertTrue(self._mounts(
            {"volumes": [{"type": "volume", "source": "myproject_dlux_runtime",
                          "target": "/opt/dlux-runtime"}]}))

    def test_short_syntax_is_recognized(self):
        self.assertTrue(self._mounts({"volumes": ["dlux_runtime:/opt/dlux-runtime:rw"]}))

    def test_an_unrelated_mount_is_not_a_match(self):
        self.assertFalse(self._mounts({"volumes": ["static:/app/staticfiles:rw"]}))

    def test_no_volumes_at_all_is_not_a_match(self):
        self.assertFalse(self._mounts({}))

    def test_an_unreadable_model_is_unknown_rather_than_a_failure(self):
        with patch.object(self.launcher, "run_docker_compose", return_value=(False, "", "boom")):
            self.assertIsNone(self.launcher._mounts_dlux_runtime("composer-executor"))

    def test_a_service_the_model_does_not_define_is_unknown(self):
        model = json.dumps({"services": {"web": {}}})
        with patch.object(self.launcher, "run_docker_compose", return_value=(True, model, "")):
            self.assertIsNone(self.launcher._mounts_dlux_runtime("composer-executor"))


class CheckupRunTests(unittest.TestCase):
    def test_failing_docker_yields_nonzero_exit(self):
        launcher = DockerComposeLauncher()
        with (
            patch.object(launcher, "run_command", return_value=(False, "", "no docker")),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            code = launcher.run_checkup(_args())
        self.assertEqual(code, 1)

    def test_clean_stack_exits_zero(self):
        launcher = DockerComposeLauncher()
        launcher.composer_version = "1.2.6"
        with (
            patch.object(launcher, "run_command", return_value=(True, "27.0\n", "")),
            patch.object(launcher, "discover_services", side_effect=lambda silent=False: setattr(launcher, "services", ["web", "composer-agent", "docker-socket-proxy"]) or True),
            patch.object(launcher, "plaintext_env_candidates", return_value=["/x/.env"]),
            patch.object(launcher, "parse_env_file", return_value={"SECRET_KEY": "x"}),
            patch.object(launcher, "required_compose_vars", return_value=set()),
            patch.object(launcher, "run_docker_compose", return_value=(True, "1.2.6\n", "")),
            patch("composer.checkup.os.path.exists", return_value=True),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            code = launcher.run_checkup(_args())
        self.assertEqual(code, 0)

    def test_fix_routes_legacy_topology_through_enable_agent(self):
        launcher = DockerComposeLauncher()
        launcher.composer_version = "1.2.5"
        with (
            patch.object(launcher, "run_command", return_value=(True, "27.0\n", "")),
            patch.object(launcher, "discover_services", side_effect=lambda silent=False: setattr(launcher, "services", ["web", "composer-updater", "docker-socket-proxy"]) or True),
            patch.object(launcher, "plaintext_env_candidates", return_value=[]),
            patch.object(launcher, "required_compose_vars", return_value=set()),
            patch.object(launcher, "run_docker_compose", return_value=(False, "", "")),
            patch("composer.checkup.os.path.exists", return_value=True),
            patch("composer.checkup.confirm", return_value=True),
            patch("composer.agent_installer.enable_agent", return_value={"backup_root": "/x/.xpose/b"}) as enable,
            patch("composer.agent_installer.enable_executor", return_value={"backup_root": "/x/.xpose/h"}) as harden,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            launcher.run_checkup(_args(fix=True))
        enable.assert_called_once()
        harden.assert_called_once()

    def test_fix_hardens_agent_topology_through_enable_executor(self):
        launcher = DockerComposeLauncher()
        launcher.composer_version = "1.2.5"
        with (
            patch.object(launcher, "run_command", return_value=(True, "27.0\n", "")),
            patch.object(
                launcher,
                "discover_services",
                side_effect=lambda silent=False: setattr(
                    launcher, "services", ["web", "composer-agent", "docker-socket-proxy"]
                )
                or True,
            ),
            patch.object(launcher, "plaintext_env_candidates", return_value=[]),
            patch.object(launcher, "required_compose_vars", return_value=set()),
            patch.object(launcher, "run_docker_compose", return_value=(False, "", "")),
            patch("composer.checkup.os.path.exists", return_value=True),
            patch("composer.checkup.confirm", return_value=True),
            patch(
                "composer.agent_installer.enable_executor",
                return_value={"backup_root": "/x/.xpose/h"},
            ) as harden,
            patch("composer.agent_installer.enable_agent") as legacy,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            launcher.run_checkup(_args(fix=True))
        harden.assert_called_once()  # check --fix runs executor enable
        legacy.assert_not_called()  # not the legacy path (agent already present)

    def test_fix_adds_missing_secrets_read_cap_on_hardened_stack(self):
        launcher = DockerComposeLauncher()
        launcher.services = ["web", "composer-agent", "composer-executor", "docker-socket-proxy"]
        launcher.active_compose_files = ["compose.yml"]
        calls = []

        def fake_enable_executor(path, compose_file="", apply=False, **kw):
            calls.append(apply)
            return {"files": ["compose.yml"]} if not apply else {"backup_root": "/x/.xpose/cap"}

        with (
            patch("composer.agent_installer.enable_executor", side_effect=fake_enable_executor),
            patch(
                "composer.checkup.inspect_legacy_proxy_routes",
                return_value={"unsupported": [], "recognized": []},
            ),
            patch("composer.checkup.confirm", return_value=True),
        ):
            fixes = launcher._maybe_fix(_args(fix=True), [])

        self.assertIn(True, calls)  # applied the repair
        self.assertTrue(
            any(f["name"] == "fix:secrets-read-cap" and f["level"] == OK for f in fixes)
        )

    def test_fix_adds_missing_restart_labels(self):
        launcher = DockerComposeLauncher()
        launcher.services = ["web", "celery", "composer-agent", "composer-executor"]
        launcher.active_compose_files = ["compose.yml"]
        calls = []

        def fake_labels(path, compose_file="", apply=False, **kw):
            calls.append(apply)
            return {"files": ["compose.yml"]} if not apply else {"backup_root": "/x/.xpose/labels", "files": ["compose.yml"]}

        with (
            patch("composer.agent_installer.normalize_restart_labels", side_effect=fake_labels),
            patch("composer.agent_installer.enable_executor", return_value={"files": []}),
            patch("composer.agent_installer.migrate_dlux_updater", return_value={"files": []}),
            patch(
                "composer.checkup.inspect_legacy_proxy_routes",
                return_value={"unsupported": [], "recognized": []},
            ),
            patch("composer.checkup.confirm", return_value=True),
        ):
            fixes = launcher._maybe_fix(_args(fix=True), [])

        self.assertIn(True, calls)
        self.assertTrue(any(f["name"] == "fix:restart-labels" for f in fixes))

    def test_fix_normalizes_dev_override_when_dev_mode_is_active(self):
        launcher = DockerComposeLauncher()
        launcher.services = ["web", "celery", "dlux-updater", "composer-agent", "composer-executor"]
        launcher.active_compose_files = ["compose.yml", "compose.dev.yml"]
        calls = []

        def fake_dev(path, compose_file="", base_file="", apply=False, **kw):
            calls.append((compose_file, base_file, apply))
            return {"files": ["compose.dev.yml"]} if not apply else {
                "backup_root": "/x/.xpose/dev",
                "files": ["compose.dev.yml"],
            }

        with (
            patch("composer.agent_installer.migrate_dlux_dev_override", side_effect=fake_dev),
            patch("composer.agent_installer.migrate_dlux_init_containers", return_value={"files": []}),
            patch("composer.agent_installer.enable_executor", return_value={"files": []}),
            patch("composer.agent_installer.migrate_dlux_updater", return_value={"files": []}),
            patch("composer.agent_installer.normalize_restart_labels", return_value={"files": []}),
            patch.object(launcher, "run_command", return_value=(True, "5.5.0", "")),
            patch.object(launcher, "_dlux_runtime_version", return_value=(1, 8, 0)),
            patch(
                "composer.checkup.inspect_legacy_proxy_routes",
                return_value={"unsupported": [], "recognized": []},
            ),
            patch("composer.checkup.confirm", return_value=True),
        ):
            fixes = launcher._maybe_fix(_args(fix=True, dev=True), [])

        self.assertIn(("compose.dev.yml", "compose.yml", True), calls)
        self.assertTrue(any(f["name"] == "fix:dev-compose" for f in fixes))

    def test_fix_migrates_legacy_dlux_updater_command(self):
        launcher = DockerComposeLauncher()
        launcher.services = ["web", "dlux-updater", "composer-agent", "composer-executor", "docker-socket-proxy"]
        launcher.active_compose_files = ["compose.yml"]
        calls = []

        def fake_migrate(path, compose_file="", apply=False, **kw):
            calls.append(apply)
            return {"files": ["compose.yml"]} if not apply else {"backup_root": "/x/.xpose/upd"}

        with (
            patch("composer.agent_installer.migrate_dlux_updater", side_effect=fake_migrate),
            patch("composer.agent_installer.dlux_runtime_migration_floor", return_value=(1, 6, 2)),
            patch("composer.agent_installer.enable_executor", return_value={"files": []}),
            patch.object(launcher, "_dlux_runtime_version", return_value=(1, 6, 2)),
            patch(
                "composer.checkup.inspect_legacy_proxy_routes",
                return_value={"unsupported": [], "recognized": []},
            ),
            patch("composer.checkup.confirm", return_value=True),
        ):
            fixes = launcher._maybe_fix(_args(fix=True), [])

        self.assertIn(True, calls)  # applied
        self.assertTrue(
            any(f["name"] == "fix:dlux-updater-runtime" and f["level"] == OK for f in fixes)
        )

    def test_fix_defers_updater_migration_when_image_is_too_old(self):
        launcher = DockerComposeLauncher()
        launcher.services = ["web", "dlux-updater", "composer-agent", "composer-executor", "docker-socket-proxy"]
        launcher.active_compose_files = ["compose.yml"]

        with (
            patch("composer.agent_installer.migrate_dlux_updater", return_value={"files": ["compose.yml"]}),
            patch("composer.agent_installer.dlux_runtime_migration_floor", return_value=(1, 6, 2)),
            patch("composer.agent_installer.enable_executor", return_value={"files": []}),
            patch.object(launcher, "_dlux_runtime_version", return_value=(1, 5, 11)),
            patch(
                "composer.checkup.inspect_legacy_proxy_routes",
                return_value={"unsupported": [], "recognized": []},
            ),
            patch("composer.checkup.confirm", return_value=True),
        ):
            fixes = launcher._maybe_fix(_args(fix=True), [])

        # Blocked: surfaced as a WARN, never applied (no fix:dlux-updater-runtime OK).
        self.assertTrue(any(
            f["name"] == "dlux-updater-runtime" and f["level"] == WARN
            and "update the project image" in f["message"].lower() for f in fixes
        ))
        self.assertFalse(any(f["name"] == "fix:dlux-updater-runtime" for f in fixes))

    def test_fix_uses_smtp_relay_module_floor(self):
        launcher = DockerComposeLauncher()
        launcher.services = ["web", "smtp-relay", "composer-agent", "composer-executor"]
        launcher.active_compose_files = ["compose.yml"]

        with (
            patch("composer.agent_installer.migrate_dlux_updater", return_value={"files": ["compose.yml"]}),
            patch("composer.agent_installer.dlux_runtime_migration_floor", return_value=(1, 7, 0)),
            patch("composer.agent_installer.enable_executor", return_value={"files": []}),
            patch.object(launcher, "_dlux_runtime_version", return_value=(1, 6, 2)),
            patch(
                "composer.checkup.inspect_legacy_proxy_routes",
                return_value={"unsupported": [], "recognized": []},
            ),
            patch("composer.checkup.confirm", return_value=True),
        ):
            fixes = launcher._maybe_fix(_args(fix=True), [])

        self.assertTrue(any(
            f["name"] == "dlux-updater-runtime" and f["level"] == WARN
            and "1.7.0" in f["message"] for f in fixes
        ))
        self.assertFalse(any(f["name"] == "fix:dlux-updater-runtime" for f in fixes))

    def test_fix_removes_obsolete_services(self):
        launcher = DockerComposeLauncher()
        launcher.services = ["web", "composer-agent", "docker-socket-proxy", "pgadmin", "db_backup"]
        launcher.active_compose_files = ["compose.yml"]
        outcome = {
            "removed_services": ["db_backup", "pgadmin"],
            "proxy_files": [],
            "backup_root": "/x/.xpose/check",
            "container_cleanup_applied": True,
            "postflight_verified": True,
            "preserved_volumes": [],
            "proxy_candidates_validated": [],
            "proxy_services_reloaded": [],
            "proxy_services_restarted": [],
        }
        with (
            patch.object(launcher, "plaintext_env_candidates", return_value=[]),
            patch.object(launcher, "build_compose_env", return_value={}),
            patch("composer.checkup.confirm", return_value=True),
            patch("composer.stack_cleanup.remove_obsolete_services", return_value=outcome) as remove,
        ):
            fixes = launcher._maybe_fix(_args(fix=True), [])

        remove.assert_called_once_with(".", ["compose.yml"], environment={})
        self.assertEqual(fixes[0]["level"], OK)
        self.assertIn("db_backup, pgadmin", fixes[0]["message"])

    def test_fix_fails_when_detected_service_cannot_be_located(self):
        launcher = DockerComposeLauncher()
        launcher.services = ["web", "composer-agent", "pgadmin"]
        launcher.active_compose_files = ["compose.yml"]
        outcome = {
            "removed_services": [],
            "proxy_files": [],
            "backup_root": "",
            "container_cleanup_applied": False,
            "postflight_verified": False,
            "preserved_volumes": [],
            "proxy_candidates_validated": [],
            "proxy_services_reloaded": [],
            "proxy_services_restarted": [],
        }
        with (
            patch.object(launcher, "plaintext_env_candidates", return_value=[]),
            patch.object(launcher, "build_compose_env", return_value={}),
            patch("composer.checkup.confirm", return_value=True),
            patch("composer.stack_cleanup.remove_obsolete_services", return_value=outcome),
        ):
            fixes = launcher._maybe_fix(_args(fix=True), [])

        self.assertEqual(fixes[0]["level"], FAIL)
        self.assertIn("pgadmin", fixes[0]["message"])

    def test_fix_repairs_proxy_only_already_migrated_stack(self):
        launcher = DockerComposeLauncher()
        launcher.services = ["web", "composer-agent", "docker-socket-proxy", "caddy"]
        launcher.active_compose_files = ["compose.yml"]
        outcome = {
            "removed_services": [],
            "proxy_files": [".proxy/Caddyfile"],
            "backup_root": "/x/.xpose/check",
            "container_cleanup_applied": False,
            "postflight_verified": True,
            "preserved_volumes": [],
            "proxy_candidates_validated": ["caddy"],
            "proxy_services_reloaded": ["caddy"],
            "proxy_services_restarted": [],
        }
        with (
            patch.object(launcher, "plaintext_env_candidates", return_value=[]),
            patch.object(launcher, "build_compose_env", return_value={}),
            patch(
                "composer.checkup.inspect_legacy_proxy_routes",
                return_value={
                    "recognized": [".proxy/Caddyfile"],
                    "unsupported": [],
                },
            ),
            patch("composer.checkup.confirm", return_value=True),
            patch("composer.stack_cleanup.remove_obsolete_services", return_value=outcome) as remove,
        ):
            fixes = launcher._maybe_fix(_args(fix=True), [])

        remove.assert_called_once_with(".", ["compose.yml"], environment={})
        self.assertEqual(fixes[0]["level"], OK)
        self.assertIn("reloaded caddy", fixes[0]["message"])

    def test_fix_declined_does_not_call_enable_agent(self):
        launcher = DockerComposeLauncher()
        launcher.services = ["web", "composer-updater", "docker-socket-proxy"]
        with (
            patch("composer.checkup.confirm", return_value=False),
            patch("composer.agent_installer.enable_agent") as enable,
        ):
            fixes = launcher._maybe_fix(_args(fix=True), [])
        enable.assert_not_called()
        self.assertEqual(fixes[0]["level"], WARN)

    def test_json_output_is_emitted(self):
        launcher = DockerComposeLauncher()
        with (
            patch.object(launcher, "run_command", return_value=(False, "", "no docker")),
            patch("sys.stdout", new_callable=io.StringIO) as out,
        ):
            launcher.run_checkup(_args(json=True))
        self.assertIn('"results"', out.getvalue())

    def test_fix_failure_yields_nonzero_exit(self):
        launcher = DockerComposeLauncher()
        launcher.composer_version = "1.2.6"
        with (
            patch.object(launcher, "run_command", return_value=(True, "27.0\n", "")),
            patch.object(
                launcher,
                "discover_services",
                side_effect=lambda silent=False: setattr(
                    launcher,
                    "services",
                    ["web", "composer-agent", "docker-socket-proxy", "pgadmin"],
                )
                or True,
            ),
            patch.object(launcher, "plaintext_env_candidates", return_value=[]),
            patch.object(launcher, "required_compose_vars", return_value=set()),
            patch.object(launcher, "run_docker_compose", return_value=(True, "1.2.6\n", "")),
            patch("composer.checkup.os.path.exists", return_value=True),
            patch("composer.checkup.confirm", return_value=True),
            patch(
                "composer.stack_cleanup.remove_obsolete_services",
                side_effect=StackCleanupError("invalid"),
            ),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            code = launcher.run_checkup(_args(fix=True))

        self.assertEqual(code, 1)

    def test_check_is_dispatched_before_flat_arguments(self):
        launcher = DockerComposeLauncher()
        with (
            patch.object(sys, "argv", ["composer", "check", "--fix"]),
            patch.object(launcher, "run_checkup", return_value=0),
            self.assertRaises(SystemExit) as caught,
        ):
            launcher.run()
        self.assertEqual(caught.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
