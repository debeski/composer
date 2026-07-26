import io
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
        self.assertIn("enable-agent", result["fix"])

    def test_topology_agent_without_proxy_warns(self):
        self.launcher.services = ["web", "composer-agent"]
        result = self.launcher._check_topology()
        self.assertEqual(result["level"], WARN)

    def test_topology_healthy_agent_is_ok(self):
        self.launcher.services = ["web", "composer-agent", "docker-socket-proxy"]
        self.assertEqual(self.launcher._check_topology()["level"], OK)

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
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            launcher.run_checkup(_args(fix=True))
        enable.assert_called_once()

    def test_fix_removes_obsolete_services(self):
        launcher = DockerComposeLauncher()
        launcher.services = ["web", "composer-agent", "docker-socket-proxy", "pgadmin", "db_backup"]
        launcher.active_compose_files = ["compose.yml"]
        outcome = {
            "removed_services": ["db_backup", "pgadmin"],
            "backup_root": "/x/.xpose/check",
            "container_cleanup_applied": True,
            "postflight_verified": True,
            "preserved_volumes": [],
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
            "backup_root": "",
            "container_cleanup_applied": False,
            "postflight_verified": False,
            "preserved_volumes": [],
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
