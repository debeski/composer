import io
import os
import sys
import unittest
from unittest.mock import patch

from composer.launcher import DockerComposeLauncher


class AgentLifecycleCommandTests(unittest.TestCase):
    def test_agent_update_targets_only_the_agent_and_skips_app_hooks(self):
        launcher = DockerComposeLauncher()
        with patch.dict(
            os.environ,
            {
                "COMPOSER_EXCLUDE_SERVICES": "composer-agent,db",
                "COMPOSER_ACTIVE_VERSION_FILE": "/state/active.json",
            },
            clear=True,
        ):
            launcher.configure_agent_update(
                ["-d", "-f", "compose.alt.yml", "--status-file", "agent.json"]
            )

        self.assertEqual(launcher.pull_service, ["composer-agent"])
        self.assertEqual(launcher.up_service, ["composer-agent"])
        self.assertTrue(launcher.no_migrate)
        self.assertIsNone(launcher.active_version_file)
        self.assertEqual(launcher.monitored_services, ["composer-agent"])
        self.assertEqual(launcher.exclude_services, ["db"])
        self.assertEqual(launcher.status_file, "agent.json")

    def test_agent_restart_targets_only_the_agent(self):
        launcher = DockerComposeLauncher()
        with patch.dict(
            os.environ,
            {"COMPOSER_EXCLUDE_SERVICES": "composer-agent"},
            clear=True,
        ):
            launcher.configure_agent_restart(["-f", "compose.alt.yml"])

        self.assertEqual(launcher.restart_service, "composer-agent")
        self.assertEqual(launcher.exclude_services, [])

    def test_agent_off_uses_compose_stop_not_project_down(self):
        launcher = DockerComposeLauncher()
        launcher.configure_agent_off([])

        with patch.object(
            launcher,
            "run_docker_compose",
            return_value=(True, "", ""),
        ) as run:
            launcher.down_containers()

        self.assertEqual(run.call_args.args[0], ["stop", "composer-agent"])

    def test_agent_lifecycle_commands_dispatch_before_flat_arguments(self):
        methods = {
            "agent-update": "configure_agent_update",
            "agent-restart": "configure_agent_restart",
            "agent-off": "configure_agent_off",
        }
        for command, method in methods.items():
            with self.subTest(command=command):
                launcher = DockerComposeLauncher()
                with (
                    patch.object(sys, "argv", ["composer", command, "-d"]),
                    patch.object(
                        launcher,
                        method,
                        side_effect=SystemExit(31),
                    ) as configure,
                    self.assertRaisesRegex(SystemExit, "31"),
                ):
                    launcher.run()

                configure.assert_called_once_with(["-d"])


class UpdateSelfCommandTests(unittest.TestCase):
    def test_update_self_pulls_and_reports_the_deployer_image(self):
        launcher = DockerComposeLauncher()
        with (
            patch.object(launcher, "run_command_interactive", return_value=0) as pull,
            patch.object(
                launcher,
                "run_command",
                return_value=(True, "1.2.9\n", ""),
            ) as version,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            launcher.handle_update_self([])

        pull.assert_called_once_with(["docker", "pull", "debeski/composer:latest"])
        self.assertEqual(
            version.call_args.args[0],
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "cat",
                "debeski/composer:latest",
                "/app/VERSION",
            ],
        )

    def test_update_self_and_legacy_alias_dispatch_before_flat_update(self):
        for argv in (["composer", "update-self"], ["composer", "--update"]):
            with self.subTest(argv=argv):
                launcher = DockerComposeLauncher()
                with (
                    patch.object(sys, "argv", argv),
                    patch.object(
                        launcher,
                        "handle_update_self",
                        side_effect=SystemExit(32),
                    ) as update_self,
                    self.assertRaisesRegex(SystemExit, "32"),
                ):
                    launcher.run()

                update_self.assert_called_once_with([])


if __name__ == "__main__":
    unittest.main()
