import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from composer.agent_installer import AgentInstallError, enable_agent, run_enable_agent
from composer.cli import parse_enable_agent_args


COMPOSE = """name: demo_project

services:
  db:
    image: postgres:17
    networks:
      - internal
  redis:
    image: redis:7
    networks:
      - internal
  web:
    image: ${WEB_IMAGE:-registry.example/demo:latest}
    networks:
      - internal
  celery:
    image: ${WEB_IMAGE:-registry.example/demo:latest}
    networks:
      - egress
      - internal
  dlux-updater:
    image: ${WEB_IMAGE:-registry.example/demo:latest}
    networks:
      - egress
  caddy:
    image: caddy:latest
    networks:
      - frontend
  # Composer-as-updater start
  docker-socket-proxy:
    image: tecnativa/docker-socket-proxy:latest
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks:
      - docker_proxy

  composer-updater:
    image: debeski/composer:latest
    command:
      - watch
      - --check-image
      - ${WEB_IMAGE:-registry.example/demo:latest}
    environment:
      WEB_IMAGE: "${WEB_IMAGE:-registry.example/demo:latest}"
      COMPOSER_VERSION_LABEL: "org.example.dlux_baked_version"
    networks:
      - egress
      - docker_proxy
  # Composer-as-updater end

volumes:
  postgres_data:
  dlux_runtime:
  caddy_data:

networks:
  frontend:
    driver: bridge
  egress:
    driver: bridge
  internal:
    internal: true
  # Isolated path from composer-updater to the docker-socket-proxy only.
  docker_proxy:
    internal: true
"""


def create_project(root: Path, dlux_version="1.5.0"):
    (root / "requirements.txt").write_text(
        f"django-lux[updater]=={dlux_version}\n",
        encoding="utf-8",
    )
    (root / "compose.yml").write_text(COMPOSE, encoding="utf-8")


def create_deployment(root: Path):
    (root / "compose.yml").write_text(COMPOSE, encoding="utf-8")


class AgentInstallerTests(unittest.TestCase):
    def test_dry_run_is_read_only_and_reports_the_candidate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            create_project(root)

            result = enable_agent(str(root), include_diff=True)

            self.assertFalse(result["applied"])
            self.assertEqual(result["files"], ["compose.yml"])
            self.assertEqual(result["warnings"], [])
            self.assertIn("--- a/compose.yml", result["diff"])
            self.assertIn("+  composer-agent:", result["diff"])
            self.assertEqual((root / "compose.yml").read_text(encoding="utf-8"), COMPOSE)
            self.assertFalse((root / ".xpose").exists())

    def test_apply_validates_before_atomic_write_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            create_project(root)
            (root / "compose.yml").chmod(0o600)
            completed = SimpleNamespace(returncode=0, stdout="ok", stderr="")
            runner = Mock(return_value=completed)

            with patch("composer.agent_installer.shutil.which", return_value="/usr/bin/docker"):
                result = enable_agent(str(root), apply=True, command_runner=runner)

            updated = (root / "compose.yml").read_text(encoding="utf-8")
            self.assertTrue(result["applied"])
            self.assertNotIn("composer-updater:", updated)
            self.assertIn("composer-agent:", updated)
            # The agent-only topology deploys from the agent itself, so it must
            # keep the read-only file override to read the project's 0600 secrets.
            self.assertIn("cap_drop:\n      - ALL", updated)
            self.assertIn("cap_add:\n      - DAC_READ_SEARCH", updated)
            self.assertIn("composer_agent_state:", updated)
            self.assertIn('COMPOSER_AGENT_RESTART_SERVICES: "web,celery,caddy"', updated)
            self.assertIn(
                'COMPOSER_EXCLUDE_SERVICES: "composer-agent,docker-socket-proxy,db,redis"',
                updated,
            )
            self.assertEqual((root / "compose.yml").stat().st_mode & 0o777, 0o600)
            backup = Path(result["backup_root"]) / "compose.yml"
            self.assertEqual(backup.read_text(encoding="utf-8"), COMPOSE)
            validation = runner.call_args_list[1]
            self.assertEqual(validation.args[0][-3:], ["-f", "-", "config"])
            self.assertEqual(validation.kwargs["input"], updated)

            with patch("composer.agent_installer.shutil.which", return_value="/usr/bin/docker"):
                repeated = enable_agent(str(root), apply=True, command_runner=runner)
            self.assertTrue(repeated["applied"])
            self.assertEqual(repeated["files"], [])
            self.assertEqual(repeated["backup_root"], "")
            self.assertEqual(repeated["command"], "")

    def test_legacy_networks_image_and_label_are_carried_forward(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            create_project(root)
            completed = SimpleNamespace(returncode=0, stdout="ok", stderr="")

            with patch("composer.agent_installer.shutil.which", return_value="/usr/bin/docker"):
                enable_agent(str(root), apply=True, command_runner=Mock(return_value=completed))

            updated = (root / "compose.yml").read_text(encoding="utf-8")
            proxy, agent = updated.split("  composer-agent:\n")
            self.assertIn("    networks:\n      - docker_proxy\n", proxy)
            self.assertIn("    networks:\n      - egress\n      - docker_proxy\n", agent)
            self.assertIn('COMPOSER_VERSION_LABEL: "org.example.dlux_baked_version"', updated)
            self.assertIn('WEB_IMAGE: "${WEB_IMAGE:-registry.example/demo:latest}"', updated)
            self.assertIn("      - ${WEB_IMAGE:-registry.example/demo:latest}\n", updated)
            self.assertNotIn("dlux_update_egress", updated)
            self.assertNotIn("demo_project_docker_proxy", updated)
            self.assertNotIn("org.demo_project.dlux_baked_version", updated)

    def test_undeclared_legacy_networks_are_reported_before_any_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            create_project(root)
            contents = (root / "compose.yml").read_text(encoding="utf-8")
            contents = contents.replace("  docker_proxy:\n    internal: true\n", "")
            (root / "compose.yml").write_text(contents, encoding="utf-8")

            with self.assertRaisesRegex(AgentInstallError, "undeclared networks: docker_proxy"):
                enable_agent(str(root))

            self.assertEqual((root / "compose.yml").read_text(encoding="utf-8"), contents)

    def test_mixed_agent_and_legacy_topology_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            create_project(root)
            contents = (root / "compose.yml").read_text(encoding="utf-8")
            contents = contents.replace(
                "  # Composer-as-updater start",
                "  # DjangoLux Composer agent start\n  composer-agent:\n"
                "    image: debeski/composer:latest\n"
                "  # DjangoLux Composer agent end\n  # Composer-as-updater start",
            ).replace("  dlux_runtime:\n", "  dlux_runtime:\n  composer_agent_state:\n")
            (root / "compose.yml").write_text(contents, encoding="utf-8")

            with self.assertRaisesRegex(AgentInstallError, "both agent and legacy"):
                enable_agent(str(root))

    def test_validation_failure_leaves_project_untouched(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            create_project(root)
            runner = Mock(
                side_effect=(
                    SimpleNamespace(returncode=0, stdout="ok", stderr=""),
                    SimpleNamespace(returncode=1, stdout="", stderr="invalid compose"),
                )
            )

            with patch("composer.agent_installer.shutil.which", return_value="/usr/bin/docker"):
                with self.assertRaisesRegex(AgentInstallError, "no project files were changed"):
                    enable_agent(str(root), apply=True, command_runner=runner)

            self.assertEqual((root / "compose.yml").read_text(encoding="utf-8"), COMPOSE)
            self.assertFalse((root / ".xpose").exists())

    def test_apply_refuses_an_old_or_unverified_dlux_bridge(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            create_project(root, dlux_version="1.4.15")
            dry_run = enable_agent(str(root))
            self.assertIn("1.5.0", dry_run["warnings"][0])
            with self.assertRaisesRegex(AgentInstallError, "Upgrade DjangoLux first"):
                enable_agent(str(root), apply=True)

    def test_deployment_directory_without_project_sources_can_migrate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            create_deployment(root)
            completed = SimpleNamespace(returncode=0, stdout="ok", stderr="")
            runner = Mock(return_value=completed)

            dry_run = enable_agent(str(root))
            self.assertEqual(dry_run["files"], ["compose.yml"])
            self.assertIn("No dependency manifest", dry_run["warnings"][0])

            with patch("composer.agent_installer.shutil.which", return_value="/usr/bin/docker"):
                result = enable_agent(str(root), apply=True, command_runner=runner)

            self.assertTrue(result["applied"])
            self.assertIn("composer-agent:", (root / "compose.yml").read_text(encoding="utf-8"))

    def test_json_cli_contract_is_machine_forwardable(self):
        args = parse_enable_agent_args(["--json"])
        result = {
            "applied": False,
            "files": ["compose.yml"],
            "command": "redeploy",
            "backup_root": "",
            "warnings": [],
        }
        with patch("composer.agent_installer.enable_agent", return_value=result), patch(
            "builtins.print"
        ) as output:
            self.assertEqual(run_enable_agent(args), 0)
        self.assertEqual(json.loads(output.call_args.args[0]), result)


DLUX_UPDATER_LEGACY = '''name: demo_project

services:
  web:
    image: ${WEB_IMAGE:-demo:latest}
    command: ["python", "-m", "tools.dlux_runtime_supervisor", "--", "gunicorn"]
  dlux-updater:
    image: ${WEB_IMAGE:-demo:latest}
    command: ["python", "-m", "tools.dlux_runtime_supervisor", "--no-watch", "--", "bash", "-c", "python manage.py migrator && exec python manage.py dlux_update_worker"]

volumes:
  dlux_runtime:
'''


class DluxUpdaterMigrationTests(unittest.TestCase):
    def test_parse_dlux_version(self):
        from composer.agent_installer import parse_dlux_version

        self.assertEqual(parse_dlux_version("1.6.2"), (1, 6, 2))
        self.assertEqual(parse_dlux_version("dlux 1.7\n"), (1, 7, 0))
        self.assertIsNone(parse_dlux_version("nope"))

    def test_transform_is_surgical_and_idempotent(self):
        from composer.agent_installer import _migrate_dlux_updater_command

        migrated = _migrate_dlux_updater_command(DLUX_UPDATER_LEGACY, "demo_project")
        self.assertNotIn("tools.dlux_runtime_supervisor", migrated)
        self.assertIn('"python", "-m", "dlux.updater.supervisor"', migrated)
        self.assertIn("python manage.py dlux_reconcile; python manage.py migrator", migrated)
        # Idempotent — no double reconcile, no re-rename.
        self.assertEqual(_migrate_dlux_updater_command(migrated, "demo_project"), migrated)
        self.assertEqual(migrated.count("dlux_reconcile"), 1)

    def test_migration_reports_legacy_command_as_a_change(self):
        from composer.agent_installer import migrate_dlux_updater

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "compose.yml").write_text(DLUX_UPDATER_LEGACY, encoding="utf-8")
            self.assertEqual(migrate_dlux_updater(str(root), apply=False)["files"], ["compose.yml"])

    def test_already_current_compose_is_a_noop(self):
        from composer.agent_installer import migrate_dlux_updater, _migrate_dlux_updater_command

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current = _migrate_dlux_updater_command(DLUX_UPDATER_LEGACY, "demo_project")
            (root / "compose.yml").write_text(current, encoding="utf-8")
            self.assertEqual(migrate_dlux_updater(str(root), apply=False)["files"], [])


if __name__ == "__main__":
    unittest.main()
