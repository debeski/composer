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
    (root / "manage.py").write_text(
        "# Generated with django-lux 1.4.15.\n",
        encoding="utf-8",
    )
    (root / "requirements.txt").write_text(
        f"django-lux[updater]=={dlux_version}\n",
        encoding="utf-8",
    )
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


if __name__ == "__main__":
    unittest.main()
