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

# A legacy composer-updater stack. `agent enable` (_transform_compose) takes it
# straight to the hardened topology.
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


# The agent-only topology `agent enable` produced through 1.5.3 (write-enabled
# proxy, DAC_READ_SEARCH on the agent). Deployments still run it, and it is
# what executor enable hardens.
AGENT_ONLY_COMPOSE = (Path(__file__).parent / "fixtures" / "agent-only-compose.yml").read_text(
    encoding="utf-8"
)


def _agent_compose():
    return AGENT_ONLY_COMPOSE


class TransformToHardenedTests(unittest.TestCase):
    def setUp(self):
        self.agent = _agent_compose()
        self.hardened = _transform_to_hardened(self.agent, "demo_project")

    def test_adds_executor_and_socket_volume(self):
        self.assertIn("  composer-executor:\n", self.hardened)
        # The shared socket volume is declared once, next to composer_agent_state.
        self.assertIn("  composer_agent_state:\n  composer_exec_sock:", self.hardened)

    def test_drops_the_proxy_and_agent_delegates(self):
        self.assertNotIn("docker-socket-proxy", self.hardened)
        self.assertNotIn("DOCKER_HOST", self.hardened)
        self.assertNotIn("POST: 1", self.hardened)
        self.assertIn("COMPOSER_EXECUTOR_SOCKET", self.hardened)

    def test_carries_forward_networks_and_image(self):
        # No invented network references: everything referenced is declared.
        self.assertIn("docker_proxy", self.hardened)
        self.assertIn("registry.example/demo:latest", self.hardened)

    def test_is_idempotent(self):
        again = _transform_to_hardened(self.hardened, "demo_project")
        self.assertEqual(again, self.hardened)

    def test_already_hardened_stack_gets_nested_role_commands(self):
        old = self.hardened.replace(
            "    command:\n      - agent\n      - run\n",
            "    command:\n      - agent\n",
        ).replace(
            "    command:\n      - executor\n      - run\n",
            "    command:\n      - executor\n",
        )

        updated = _transform_to_hardened(old, "demo_project")

        self.assertIn("    command:\n      - agent\n      - run\n", updated)
        self.assertIn("    command:\n      - executor\n      - run\n", updated)

    def test_hardened_output_has_no_retired_cap(self):
        self.assertNotIn("DAC_READ_SEARCH", self.hardened)

    def test_already_hardened_stack_is_normalized(self):
        old = self.hardened.replace("      - DAC_OVERRIDE\n", "      - DAC_READ_SEARCH\n")
        self.assertNotEqual(old, self.hardened)

        self.assertEqual(_transform_to_hardened(old, "demo_project"), self.hardened)

    def test_refuses_a_legacy_updater_stack(self):
        with self.assertRaises(AgentInstallError):
            _transform_to_hardened(LEGACY_COMPOSE, "demo_project")


class AgentEnableHardensTests(unittest.TestCase):
    """`agent enable` never leaves a write-enabled proxy behind."""

    def test_legacy_updater_goes_straight_to_hardened(self):
        updated = _transform_compose(LEGACY_COMPOSE, "demo_project")
        self.assertIn("  composer-executor:\n", updated)
        self.assertIn("  composer_agent_state:\n  composer_exec_sock:", updated)
        self.assertNotIn("POST: 1", updated)
        self.assertNotIn("EXEC: 1", updated)
        self.assertEqual(_transform_to_hardened(updated, "demo_project"), updated)

    def test_an_agent_only_block_is_hardened(self):
        updated = _transform_compose(AGENT_ONLY_COMPOSE, "demo_project")
        self.assertEqual(updated, _transform_to_hardened(AGENT_ONLY_COMPOSE, "demo_project"))
        self.assertNotIn("POST: 1", updated)
        # worker (stack schema 5) right after web, celery kept for project tasks.
        self.assertEqual(updated.count('COMPOSER_AGENT_RESTART_SERVICES: "web,worker,celery,caddy"'), 2)


class HardenedComposeValidityTests(unittest.TestCase):
    def test_generated_compose_is_valid_yaml(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("pyyaml not installed")
        doc = yaml.safe_load(_transform_to_hardened(_agent_compose(), "demo_project"))
        self.assertIn("composer-executor", doc["services"])
        self.assertIn("composer_exec_sock", doc["volumes"])
        self.assertNotIn("docker-socket-proxy", doc["services"])
        self.assertNotIn("DOCKER_HOST", doc["services"]["composer-agent"]["environment"])


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
            self.assertEqual(result["command"], "./start.sh")
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

    def test_apply_removes_the_proxy_container_before_writing(self):
        """A deploy does not remove orphans: a proxy left running stays reachable."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._project(root, _agent_compose())
            seen = []

            def runner(argv, **kwargs):
                text = (root / "compose.yml").read_text(encoding="utf-8")
                seen.append((list(argv), "  docker-socket-proxy:\n" in text))
                return Mock(returncode=0, stderr="", stdout="")

            enable_executor(str(root), apply=True, command_runner=runner)
            removals = [(argv, defined) for argv, defined in seen if "rm" in argv]
            self.assertEqual(len(removals), 1)
            argv, still_defined = removals[0]
            self.assertEqual(argv[-4:], ["rm", "-s", "-f", "docker-socket-proxy"])
            self.assertIn("demo_project", argv)
            self.assertTrue(still_defined, "removed while the file still names the service")
            self.assertNotIn("docker-socket-proxy", (root / "compose.yml").read_text(encoding="utf-8"))

    def test_a_failed_container_removal_writes_nothing(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._project(root, _agent_compose())

            def runner(argv, **kwargs):
                return Mock(returncode=1 if "rm" in argv else 0, stderr="daemon gone", stdout="")

            with self.assertRaises(AgentInstallError) as caught:
                enable_executor(str(root), apply=True, command_runner=runner)
            self.assertIn("no project files were changed", str(caught.exception))
            self.assertEqual((root / "compose.yml").read_text(encoding="utf-8"), _agent_compose())

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
