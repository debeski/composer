import unittest

from composer.agent_installer import (
    COMPOSER_AGENT_END,
    COMPOSER_AGENT_START,
    COMPOSER_EXEC_SOCKET_PATH,
    _hardened_stack,
)


def _topology():
    return {
        "web_image": "debeski/decrees:latest",
        "version_label": "org.decrees.dlux_baked_version",
        "proxy_networks": ["docker_proxy"],
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

    def test_all_three_roles_present_between_markers(self):
        self.assertTrue(self.block.startswith(COMPOSER_AGENT_START))
        self.assertTrue(self.block.rstrip().endswith(COMPOSER_AGENT_END))
        for name in ("docker-socket-proxy", "composer-executor", "composer-agent"):
            self.assertIn(f"  {name}:\n", self.block)

    def test_proxy_is_read_only(self):
        proxy = _section(self.block, "docker-socket-proxy")
        self.assertIn("POST: 0", proxy)
        self.assertIn("EXEC: 0", proxy)
        self.assertNotIn("POST: 1", proxy)
        self.assertNotIn("EXEC: 1", proxy)
        # Reads the agent still needs stay enabled.
        self.assertIn("CONTAINERS: 1", proxy)
        self.assertIn("IMAGES: 1", proxy)
        # Only a read-only bind of the socket.
        self.assertIn("/var/run/docker.sock:/var/run/docker.sock:ro", proxy)

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

    def test_agent_keeps_readonly_proxy_and_delegates_writes(self):
        agent = _section(self.block, "composer-agent")
        # Reads via the (now read-only) proxy.
        self.assertIn('DOCKER_HOST: "tcp://docker-socket-proxy:2375"', agent)
        # No real Docker socket on the network-facing agent.
        self.assertNotIn("/var/run/docker.sock:/var/run/docker.sock", agent)
        # Delegates writes over the shared unix socket.
        self.assertIn(f'COMPOSER_EXECUTOR_SOCKET: "{COMPOSER_EXEC_SOCKET_PATH}"', agent)
        self.assertIn("composer_exec_sock:/run/composer-exec:rw", agent)
        self.assertIn("composer-executor:\n        condition: service_started", agent)

    def test_executor_is_excluded_from_managed_operations(self):
        ex = _section(self.block, "composer-executor")
        self.assertIn("composer-executor", ex.split("COMPOSER_EXCLUDE_SERVICES")[1].split("\n")[0])


if __name__ == "__main__":
    unittest.main()
