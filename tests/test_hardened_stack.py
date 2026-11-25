import os
import re
import unittest
from pathlib import Path

from composer.agent_installer import (
    COMPOSER_AGENT_END,
    COMPOSER_AGENT_START,
    COMPOSER_EXEC_SOCKET_PATH,
    _drop_docker_proxy,
    _hardened_stack,
)


def _topology():
    return {
        "web_image": "debeski/decrees:latest",
        "version_label": "org.decrees.dlux_baked_version",
        "executor_networks": ["docker_proxy"],
        "agent_networks": ["egress", "docker_proxy"],
    }


def _section(block, name):
    """Return the YAML text for one service in the generated block."""
    chunks = block.split("\n\n")
    for chunk in chunks:
        if f"  {name}:\n" in chunk or chunk.lstrip().startswith(f"{name}:"):
            return chunk
    raise AssertionError(f"service {name} not found in block")


class HardenedStackTests(unittest.TestCase):
    def setUp(self):
        self.block = _hardened_stack("decrees", {"web", "celery", "db", "redis"}, _topology())

    def test_both_roles_present_between_markers(self):
        self.assertTrue(self.block.startswith(COMPOSER_AGENT_START))
        self.assertTrue(self.block.rstrip().endswith(COMPOSER_AGENT_END))
        for name in ("composer-executor", "composer-agent"):
            self.assertIn(f"  {name}:\n", self.block)

    def test_there_is_no_docker_proxy(self):
        """Its container inspect would hand the agent every service's environment."""
        self.assertNotIn("docker-socket-proxy", self.block)
        mounts = [line for line in self.block.splitlines() if "/var/run/docker.sock" in line]
        self.assertEqual(len(mounts), 1, "only the executor mounts the socket")

    def test_executor_holds_the_real_socket_and_is_not_network_facing(self):
        ex = _section(self.block, "composer-executor")
        self.assertIn("- executor", ex)
        self.assertIn("/var/run/docker.sock:/var/run/docker.sock:rw", ex)  # real write authority
        self.assertIn(f'COMPOSER_EXECUTOR_SOCKET: "{COMPOSER_EXEC_SOCKET_PATH}"', ex)
        self.assertIn("composer_exec_sock:/run/composer-exec:rw", ex)
        # Not network-facing: no control-plane enrollment on the privileged role.
        self.assertNotIn("COMPOSER_CONTROL_URL", ex)
        self.assertNotIn("COMPOSER_ENROLLMENT_TOKEN", ex)
        self.assertIn("no-new-privileges:true", ex)

    def test_executor_can_read_the_projects_secrets_to_deploy(self):
        # The executor runs the deploy (docker compose up), which reads the
        # project's 0600 .secrets/.env. cap_drop:ALL strips DAC_OVERRIDE, so UID 0
        # can't read a file it doesn't own; the read cap must be added back or
        # every inline deploy fails on the secrets guard. Never DAC_READ_SEARCH:
        # it unlocks open_by_handle_at, an escape from the project bind mount.
        ex = _section(self.block, "composer-executor")
        self.assertIn("cap_drop:\n      - ALL", ex)
        self.assertIn("cap_add:\n      - DAC_OVERRIDE\n", ex)
        self.assertNotIn("DAC_READ_SEARCH", self.block)

    def test_restart_targets_follow_the_task_services_present(self):
        """worker from stack schema 5, right after web; celery where it stays."""
        cases = {
            frozenset({"web", "celery", "db"}): "web,celery",
            frozenset({"web", "worker", "celery", "db"}): "web,worker,celery",
            frozenset({"web", "worker", "smtp-relay", "caddy", "ocr", "db"}): "web,worker,smtp-relay,caddy,ocr",
        }
        for services, expected in cases.items():
            block = _hardened_stack("decrees", set(services), _topology())
            self.assertEqual(block.count(f'COMPOSER_AGENT_RESTART_SERVICES: "{expected}"'), 2, expected)

    def test_agent_stays_read_only_without_the_file_override(self):
        # The network-facing agent never deploys, so it must NOT carry the read
        # override — least privilege for the internet-reachable role.
        agent = _section(self.block, "composer-agent")
        self.assertIn("cap_drop:\n      - ALL", agent)
        self.assertNotIn("cap_add", agent)

    def test_missing_read_cap_is_healed_on_the_executor_only(self):
        from composer.agent_installer import _ensure_deployer_read_cap

        stripped = self.block.replace("    cap_add:\n      - DAC_OVERRIDE\n", "")
        self.assertNotIn("cap_add", stripped)
        healed = _ensure_deployer_read_cap(stripped, "decrees")
        self.assertIn("cap_add:\n      - DAC_OVERRIDE\n", _section(healed, "composer-executor"))
        self.assertNotIn("cap_add", _section(healed, "composer-agent"))
        # Idempotent once the cap is present.
        self.assertEqual(_ensure_deployer_read_cap(healed, "decrees"), healed)

    def test_retired_read_cap_is_swapped_on_every_role(self):
        from composer.agent_installer import _ensure_deployer_read_cap

        old = self.block.replace("      - DAC_OVERRIDE\n", "      - DAC_READ_SEARCH\n").replace(
            "    cap_drop:\n      - ALL\n    working_dir",
            "    cap_drop:\n      - ALL\n    cap_add:\n      - DAC_READ_SEARCH\n    working_dir",
        )
        self.assertIn("DAC_READ_SEARCH", _section(old, "composer-agent"))

        healed = _ensure_deployer_read_cap(old, "decrees")

        self.assertEqual(healed, self.block)

    def test_agent_has_no_docker_api_and_delegates_to_the_executor(self):
        agent = _section(self.block, "composer-agent")
        self.assertNotIn("DOCKER_HOST", agent)
        # No real Docker socket on the network-facing agent.
        self.assertNotIn("/var/run/docker.sock:/var/run/docker.sock", agent)
        # Delegates writes over the shared unix socket.
        self.assertIn(f'COMPOSER_EXECUTOR_SOCKET: "{COMPOSER_EXEC_SOCKET_PATH}"', agent)
        self.assertIn("composer_exec_sock:/run/composer-exec:rw", agent)
        self.assertIn("composer-executor:\n        condition: service_started", agent)

    def test_executor_is_excluded_from_managed_operations(self):
        ex = _section(self.block, "composer-executor")
        self.assertIn("composer-executor", ex.split("COMPOSER_EXCLUDE_SERVICES")[1].split("\n")[0])


class DropDockerProxyTests(unittest.TestCase):
    """`check --fix` takes a leftover proxy out of an already-hardened block."""

    PROXY = """  # Read-only Docker gateway (POST=0, EXEC=0): the agent reads container/image
  # state through it but can never mutate. Writes go through composer-executor.
  docker-socket-proxy:
    image: tecnativa/docker-socket-proxy:v0.5.0@sha256:1f5038b54f06c3e18422902cf00ba21803d1c97805aae032e5e6673d532d3459
    restart: always
    environment:
      CONTAINERS: 1
      POST: 0
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks:
      - docker_proxy

"""

    def setUp(self):
        self.block = _hardened_stack("decrees", {"web", "worker", "db", "redis"}, _topology())
        self.contents = f"name: decrees\nservices:\n  web:\n    image: x\n\n{self.block}\n\nvolumes:\n  dlux_runtime:\n"
        old = self.block.replace(COMPOSER_AGENT_START + "\n", COMPOSER_AGENT_START + "\n" + self.PROXY, 1)
        old = old.replace(
            '    environment:\n      WEB_IMAGE: "debeski/decrees:latest"\n      COMPOSER_REGISTRY_USERNAME: "${COMPOSER_REGISTRY_USERNAME:-}"\n      COMPOSER_REGISTRY_PASSWORD: "${COMPOSER_REGISTRY_PASSWORD:-}"\n      COMPOSER_REGISTRY_HOST: "${COMPOSER_REGISTRY_HOST:-}"\n      COMPOSER_CONTROL_URL',
            '    environment:\n      DOCKER_HOST: "tcp://docker-socket-proxy:2375"\n      WEB_IMAGE: "debeski/decrees:latest"\n      COMPOSER_REGISTRY_USERNAME: "${COMPOSER_REGISTRY_USERNAME:-}"\n      COMPOSER_REGISTRY_PASSWORD: "${COMPOSER_REGISTRY_PASSWORD:-}"\n      COMPOSER_REGISTRY_HOST: "${COMPOSER_REGISTRY_HOST:-}"\n      COMPOSER_CONTROL_URL', 1)
        old = old.replace(
            "    depends_on:\n      composer-executor:",
            "    depends_on:\n      docker-socket-proxy:\n        condition: service_started\n      composer-executor:", 1)
        self.assertIn('DOCKER_HOST: "tcp://docker-socket-proxy:2375"', old)
        self.old = self.contents.replace(self.block, old)

    def test_the_proxy_its_route_and_its_dependency_go(self):
        self.assertEqual(_drop_docker_proxy(self.old, "decrees"), self.contents)

    def test_the_proxy_leaves_the_exclusion_lists(self):
        listed = self.old.replace(
            'COMPOSER_EXCLUDE_SERVICES: "composer-agent,composer-executor,db,redis"',
            'COMPOSER_EXCLUDE_SERVICES: "composer-agent,composer-executor,docker-socket-proxy,db,redis"')
        self.assertEqual(listed.count("composer-executor,docker-socket-proxy,db"), 2)
        self.assertEqual(_drop_docker_proxy(listed, "decrees"), self.contents)

    def test_idempotent_on_a_block_without_a_proxy(self):
        self.assertEqual(_drop_docker_proxy(self.contents, "decrees"), self.contents)

    def test_a_proxy_outside_the_generated_block_is_left_alone(self):
        outside = self.contents.replace("services:\n", "services:\n" + self.PROXY, 1)
        self.assertEqual(_drop_docker_proxy(outside, "decrees"), outside)

    def test_a_legacy_agent_without_an_executor_keeps_its_proxy(self):
        legacy = self.old.replace("  composer-executor:\n", "  composer-executor-off:\n", 1)
        self.assertEqual(_drop_docker_proxy(legacy, "decrees"), legacy)


DLUX_CHECKOUT = Path(
    os.environ.get("DLUX_CHECKOUT") or Path(__file__).resolve().parents[3] / "pkg-django-lux" / "main"
)
DLUX_COMPOSE = DLUX_CHECKOUT / "dlux" / "scaffold" / "templates" / "project" / "compose.yml.tmpl"


class DjangoLuxScaffoldMirrorTests(unittest.TestCase):
    """DjangoLux's scaffold is born with the topology ``check --fix`` leaves."""

    def test_the_scaffold_has_no_docker_proxy(self):
        if not DLUX_COMPOSE.is_file():
            self.skipTest(f"No DjangoLux checkout at {DLUX_CHECKOUT}")
        text = DLUX_COMPOSE.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"(?m)^  docker-socket-proxy:", text))
        self.assertNotIn("DOCKER_HOST", text)


if __name__ == "__main__":
    unittest.main()
