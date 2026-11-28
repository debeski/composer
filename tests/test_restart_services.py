"""`./start.sh restart web worker` was refused: `restart` took one service (break-it b3 re-run)."""
import os
import unittest
from unittest.mock import patch

from composer.launcher import DockerComposeLauncher


class RestartServicesTests(unittest.TestCase):
    def _configure(self, argv, env=None):
        launcher = DockerComposeLauncher()
        with patch.dict(os.environ, {"COMPOSER_RESTART_SERVICES": "", "COMPOSER_EXCLUDE_SERVICES": "", **(env or {})}):
            launcher.configure_restart(argv)
        return launcher

    def _compose_args(self, launcher):
        calls = []
        launcher.run_docker_compose_streaming = lambda args, **kwargs: (calls.append(args) or (True, "", ""))
        launcher.restart_containers()
        return calls[0]

    def test_several_services_restart_together_in_order(self):
        launcher = self._configure(["worker", "web", "worker"])
        self.assertEqual(self._compose_args(launcher), ["restart", "worker", "web"])

    def test_one_service_keeps_the_single_form(self):
        launcher = self._configure(["web"])
        self.assertEqual(launcher.restart_service, "web")
        self.assertEqual(self._compose_args(launcher), ["restart", "web"])

    def test_named_services_beat_the_environment_list(self):
        launcher = self._configure(["web", "worker"], env={"COMPOSER_RESTART_SERVICES": "caddy"})
        self.assertEqual(self._compose_args(launcher), ["restart", "web", "worker"])

    def test_no_service_still_reads_the_environment_list(self):
        launcher = self._configure([], env={"COMPOSER_RESTART_SERVICES": "web,caddy"})
        self.assertEqual(self._compose_args(launcher), ["restart", "web", "caddy"])


if __name__ == "__main__":
    unittest.main()
