import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from composer.agent_installer import (
    AgentInstallError,
    _transform_compose,
    _transform_to_hardened,
    enable_executor,
)

# A legacy composer-updater stack; _transform_compose turns it into the current
# composer-agent topology, which is the starting point enable-executor hardens.
LEGACY_COMPOSE = """name: demo_project

services:
  db:
    image: postgres:17
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


def _agent_compose():
    return _transform_compose(LEGACY_COMPOSE, "demo_project")


class TransformToHardenedTests(unittest.TestCase):
    def setUp(self):
        self.agent = _agent_compose()
        self.hardened = _transform_to_hardened(self.agent, "demo_project")

    def test_adds_executor_and_socket_volume(self):
        self.assertIn("  composer-executor:\n", self.hardened)
        # The shared socket volume is declared once, next to composer_agent_state.
        self.assertIn("  composer_agent_state:\n  composer_exec_sock:", self.hardened)

    def test_demotes_proxy_and_agent_delegates(self):
        self.assertIn("POST: 0", self.hardened)
        self.assertIn("EXEC: 0", self.hardened)
        self.assertNotIn("POST: 1", self.hardened)
        self.assertIn("COMPOSER_EXECUTOR_SOCKET", self.hardened)

    def test_carries_forward_networks_and_image(self):
        # No invented network references: everything referenced is declared.
        self.assertIn("docker_proxy", self.hardened)
        self.assertIn("registry.example/demo:latest", self.hardened)

    def test_is_idempotent(self):
        again = _transform_to_hardened(self.hardened, "demo_project")
        self.assertEqual(again, self.hardened)

    def test_refuses_a_legacy_updater_stack(self):
        with self.assertRaises(AgentInstallError):
            _transform_to_hardened(LEGACY_COMPOSE, "demo_project")


class HardenedComposeValidityTests(unittest.TestCase):
    def test_generated_compose_is_valid_yaml(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("pyyaml not installed")
        doc = yaml.safe_load(_transform_to_hardened(_agent_compose(), "demo_project"))
        self.assertIn("composer-executor", doc["services"])
        self.assertIn("composer_exec_sock", doc["volumes"])
        proxy = doc["services"]["docker-socket-proxy"]["environment"]
        self.assertEqual(proxy["POST"], 0)
        self.assertEqual(proxy["EXEC"], 0)


class EnableExecutorTests(unittest.TestCase):
    def _project(self, root: Path, compose: str, dlux="1.5.0"):
        (root / "requirements.txt").write_text(f"django-lux=={dlux}\n", encoding="utf-8")
        (root / "compose.yml").write_text(compose, encoding="utf-8")

    def test_dry_run_reports_change_and_redeploy_command(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._project(root, _agent_compose())
            result = enable_executor(str(root), include_diff=True)
            self.assertFalse(result["applied"])
            self.assertEqual(result["files"], ["compose.yml"])
            self.assertIn("composer-executor", result["command"])
            self.assertIn("composer-executor", result["diff"])

    def test_apply_validates_backs_up_and_writes(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._project(root, _agent_compose())
            runner = Mock(return_value=Mock(returncode=0, stderr=""))
            result = enable_executor(str(root), apply=True, command_runner=runner)
            self.assertTrue(result["applied"])
            self.assertTrue(result["backup_root"])
            written = (root / "compose.yml").read_text(encoding="utf-8")
            self.assertIn("  composer-executor:\n", written)
            self.assertIn("  composer_exec_sock:", written)

    def test_apply_is_a_noop_when_already_hardened(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._project(root, _transform_to_hardened(_agent_compose(), "demo_project"))
            runner = Mock(return_value=Mock(returncode=0, stderr=""))
            result = enable_executor(str(root), apply=True, command_runner=runner)
            self.assertEqual(result["files"], [])
            self.assertEqual(result["command"], "")


if __name__ == "__main__":
    unittest.main()
