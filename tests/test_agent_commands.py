import io
import os
import sys
import unittest
from unittest.mock import patch

from composer.launcher import DockerComposeLauncher


class AgentLifecycleCommandTests(unittest.TestCase):
    def test_agent_update_targets_only_the_agent_when_no_executor(self):
        launcher = DockerComposeLauncher()
        with patch.object(launcher, "_resident_pair_scope", return_value=["composer-agent"]):
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

    def test_agent_update_targets_the_pair_when_executor_present(self):
        launcher = DockerComposeLauncher()
        pair = ["composer-agent", "composer-executor"]
        with patch.object(launcher, "_resident_pair_scope", return_value=pair):
            with patch.dict(os.environ, {}, clear=True):
                launcher.configure_agent_update(["--status-file", "agent.json"])
        # Both resident roles are pulled + recreated from the one shared image so
        # they can never drift to different versions.
        self.assertEqual(launcher.pull_service, pair)
        self.assertEqual(launcher.up_service, pair)
        self.assertEqual(launcher.monitored_services, pair)

    def test_agent_restart_targets_only_the_agent_when_no_executor(self):
        launcher = DockerComposeLauncher()
        with patch.object(launcher, "_resident_pair_scope", return_value=["composer-agent"]):
            with patch.dict(os.environ, {"COMPOSER_EXCLUDE_SERVICES": "composer-agent"}, clear=True):
                launcher.configure_agent_restart(["-f", "compose.alt.yml"])

        self.assertIsNone(launcher.restart_service)  # uses the list form
        self.assertEqual(launcher.restart_services, ["composer-agent"])
        self.assertEqual(launcher.exclude_services, [])

    def test_agent_restart_targets_the_pair_when_executor_present(self):
        launcher = DockerComposeLauncher()
        pair = ["composer-agent", "composer-executor"]
        with patch.object(launcher, "_resident_pair_scope", return_value=pair):
            with patch.dict(os.environ, {}, clear=True):
                launcher.configure_agent_restart([])
        self.assertEqual(launcher.restart_services, pair)

    def test_agent_off_stops_the_agent_when_no_executor(self):
        launcher = DockerComposeLauncher()
        with patch.object(launcher, "_resident_pair_scope", return_value=["composer-agent"]):
            launcher.configure_agent_off([])

        with patch.object(launcher, "run_docker_compose", return_value=(True, "", "")) as run:
            launcher.down_containers()
        self.assertEqual(run.call_args.args[0], ["stop", "composer-agent"])

    def test_agent_off_stops_the_pair_when_executor_present(self):
        launcher = DockerComposeLauncher()
        pair = ["composer-agent", "composer-executor"]
        with patch.object(launcher, "_resident_pair_scope", return_value=pair):
            launcher.configure_agent_off([])

        with patch.object(launcher, "run_docker_compose", return_value=(True, "", "")) as run:
            launcher.down_containers()
        self.assertEqual(run.call_args.args[0], ["stop", "composer-agent", "composer-executor"])

    def test_resident_pair_scope_detects_executor_from_compose_services(self):
        launcher = DockerComposeLauncher()
        launcher.active_compose_files = ["compose.yml"]
        with patch.object(
            launcher, "run_docker_compose",
            return_value=(True, "web\ncomposer-agent\ncomposer-executor\ndb\n", ""),
        ):
            self.assertEqual(launcher._resident_pair_scope(), ["composer-agent", "composer-executor"])

    def test_resident_pair_scope_is_agent_only_for_legacy_stack(self):
        launcher = DockerComposeLauncher()
        launcher.active_compose_files = ["compose.yml"]
        with patch.object(
            launcher, "run_docker_compose",
            return_value=(True, "web\ncomposer-agent\ndocker-socket-proxy\n", ""),
        ):
            self.assertEqual(launcher._resident_pair_scope(), ["composer-agent"])

    def test_resident_pair_scope_falls_back_to_agent_on_discovery_failure(self):
        launcher = DockerComposeLauncher()
        launcher.active_compose_files = ["compose.yml"]
        with patch.object(launcher, "run_docker_compose", return_value=(False, "", "boom")):
            self.assertEqual(launcher._resident_pair_scope(), ["composer-agent"])

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
            patch.object(
                launcher, "run_command_streaming", return_value=(True, "", "")
            ) as pull,
            patch.object(
                launcher,
                "run_command",
                return_value=(True, "1.2.9\n", ""),
            ) as version,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            launcher.handle_update_self([])

        self.assertEqual(
            pull.call_args.args[0], ["docker", "pull", "debeski/composer:latest"]
        )
        self.assertIsNotNone(pull.call_args.kwargs["progress_callback"])
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

    def test_update_self_shows_the_pull_bar_and_stops_on_failure(self):
        launcher = DockerComposeLauncher()
        drawn = io.StringIO()

        def stream(cmd, **kwargs):
            for line in (
                "latest: Pulling from debeski/composer",
                "9824c27679d3: Pulling fs layer",
                "9824c27679d3: Pull complete",
            ):
                kwargs["progress_callback"](line)
            return False, "no space left on device", ""

        with (
            patch.object(launcher, "run_command_streaming", side_effect=stream),
            patch.object(launcher, "run_command") as version,
            patch("sys.stdout", new=drawn),
            patch("sys.stderr", new_callable=io.StringIO) as errors,
            self.assertRaises(SystemExit) as exit_code,
        ):
            launcher.handle_update_self([])

        self.assertEqual(exit_code.exception.code, 1)
        self.assertIn("█", drawn.getvalue())
        self.assertIn("no space left on device", errors.getvalue())
        version.assert_not_called()

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
