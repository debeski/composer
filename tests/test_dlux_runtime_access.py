"""`composer dlux update` from the project root, where nothing mounts the volume.

`/opt/dlux-runtime` only exists inside the stack's containers, so the deployer
CLI has to find the Docker volume behind it and re-run itself with that volume
attached. The failure modes matter as much as the happy path: a volume that does
not exist yet must never be created by the `docker run`, and the child must not
be able to delegate again.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from composer import dlux_runtime_access as access
from composer.dlux_package_cli import parse_dlux_update_args, run_dlux_update


CONFIG = {
    "name": "app",
    "services": {
        "web": {"volumes": [{"type": "volume", "source": "static", "target": "/app/static"}]},
        "celery": {
            "volumes": [
                {"type": "bind", "source": "/srv/app", "target": "/srv/app"},
                {"type": "volume", "source": "dlux_runtime", "target": "/opt/dlux-runtime"},
            ]
        },
    },
    "volumes": {"dlux_runtime": {}, "static": {}},
}


class _Launcher:
    """Just the surface `delegate_dlux_update` uses."""

    def __init__(self, config=CONFIG, volume_ok=True):
        self.config = config
        self.volume_ok = volume_ok
        self.last_runtime_diagnostic = ""
        self.interactive_calls = []

    def compose_config_json(self):
        return self.config

    def run_command(self, cmd, timeout=None, env=None):
        if cmd[:3] == ["docker", "volume", "inspect"]:
            return self.volume_ok, "[]", ""
        if cmd[:2] == ["docker", "inspect"]:
            return True, "sha256:abc\n", ""
        return True, "", ""

    def run_command_interactive(self, cmd, env=None):
        self.interactive_calls.append(cmd)
        return 0


class VolumeDiscoveryTests(unittest.TestCase):
    def test_the_volume_is_found_by_the_mount_that_targets_the_runtime_root(self):
        self.assertEqual(access.find_runtime_volume(CONFIG, "/opt/dlux-runtime"), "dlux_runtime")

    def test_a_trailing_slash_does_not_hide_the_mount(self):
        self.assertEqual(access.find_runtime_volume(CONFIG, "/opt/dlux-runtime/"), "dlux_runtime")

    def test_a_bind_mount_at_the_same_path_is_not_a_volume(self):
        config = {"services": {"web": {"volumes": [
            {"type": "bind", "source": "/opt/dlux-runtime", "target": "/opt/dlux-runtime"},
        ]}}}
        self.assertEqual(access.find_runtime_volume(config, "/opt/dlux-runtime"), "")

    def test_short_string_mounts_are_read_too(self):
        config = {"services": {"celery": {"volumes": ["dlux_runtime:/opt/dlux-runtime:ro"]}}}
        self.assertEqual(access.find_runtime_volume(config, "/opt/dlux-runtime"), "dlux_runtime")

    def test_the_docker_name_is_the_project_prefixed_one(self):
        self.assertEqual(access.qualified_volume_name(CONFIG, "dlux_runtime"), "app_dlux_runtime")

    def test_an_explicit_volume_name_wins_over_the_prefix(self):
        config = {"name": "app", "volumes": {"dlux_runtime": {"name": "shared_dlux"}}}
        self.assertEqual(access.qualified_volume_name(config, "dlux_runtime"), "shared_dlux")

    def test_a_stack_without_the_mount_is_reported_not_guessed(self):
        launcher = _Launcher(config={"name": "app", "services": {"web": {}}})
        with self.assertRaises(access.RuntimeVolumeError) as raised:
            access.resolve_runtime_volume(launcher, "/opt/dlux-runtime")
        self.assertIn("No service in this stack mounts", str(raised.exception))

    def test_a_declared_but_absent_volume_is_refused(self):
        """`docker run -v missing:/path` would create an empty one silently."""
        launcher = _Launcher(volume_ok=False)
        with self.assertRaises(access.RuntimeVolumeError) as raised:
            access.resolve_runtime_volume(launcher, "/opt/dlux-runtime")
        self.assertIn("app_dlux_runtime", str(raised.exception))


class DelegatedCommandTests(unittest.TestCase):
    def _command(self, argv, **overrides):
        options = dict(
            image="composer:test",
            action="check",
            volume="app_dlux_runtime",
            runtime_root="/opt/dlux-runtime",
            argv=argv,
            project_dir="/srv/app",
            interactive=False,
            socket_path="/nonexistent.sock",
            env={},
        )
        options.update(overrides)
        return access.build_delegated_command(**options)

    def test_the_runtime_volume_is_mounted_where_the_child_looks_for_it(self):
        command = self._command([])
        self.assertIn("app_dlux_runtime:/opt/dlux-runtime:rw", command)
        root = command.index("--runtime-root")
        self.assertEqual(command[root + 1], "/opt/dlux-runtime")

    def test_the_child_cannot_delegate_again(self):
        self.assertIn("--no-delegate", self._command([]))

    def test_the_original_arguments_are_forwarded_in_order(self):
        command = self._command(["--version", "1.8.7"], action="update")
        tail = command[command.index("dlux"):]
        self.assertEqual(tail[:4], ["dlux", "update", "--version", "1.8.7"])

    def test_the_project_directory_is_mounted_at_its_own_path(self):
        command = self._command([])
        self.assertIn("/srv/app:/srv/app", command)
        self.assertEqual(command[command.index("-w") + 1], "/srv/app")

    def test_the_docker_socket_is_mounted_when_this_composer_has_one(self):
        command = self._command([], action="update", socket_path=__file__)
        self.assertIn(f"{__file__}:{__file__}", command)

    def test_inherited_secrets_are_forwarded_by_name_not_by_value(self):
        env = {"COMPOSER_INHERITED_SECRET_KEYS": "SECRET_KEY,DB_PASSWORD"}
        command = self._command([], env=env)
        self.assertEqual(command.count("-e"), 3)
        self.assertIn("SECRET_KEY", command)
        self.assertNotIn("--env-file", command)
        self.assertFalse([token for token in command if "=" in token and "SECRET" in token])

    def test_a_tty_is_only_requested_when_there_is_one(self):
        self.assertNotIn("-t", self._command([]))
        self.assertIn("-t", self._command([], interactive=True))


class RunDluxUpdateTests(unittest.TestCase):
    def test_leading_global_file_flag_still_reaches_the_dlux_group(self):
        from composer.launcher import DockerComposeLauncher

        launcher = DockerComposeLauncher()
        with (
            patch.object(sys, "argv", ["composer", "-f", "compose.yml", "dlux", "check", "--runtime-root", "/nope/dlux-runtime"]),
            patch("composer.dlux_runtime_access.delegate_dlux_update", return_value=7) as delegate,
            self.assertRaises(SystemExit) as exit_code,
        ):
            launcher.run()

        self.assertEqual(exit_code.exception.code, 7)
        self.assertEqual(delegate.call_args[0][0].file, "compose.yml")

    def test_a_missing_runtime_root_delegates_instead_of_failing(self):
        args = parse_dlux_update_args(["--runtime-root", "/nope/dlux-runtime"], action="check")
        with patch("composer.dlux_runtime_access.delegate_dlux_update", return_value=7) as delegate:
            self.assertEqual(run_dlux_update(args, []), 7)
        self.assertEqual(delegate.call_args[0][1], [])

    def test_the_delegated_child_reports_the_missing_volume_instead_of_looping(self):
        args = parse_dlux_update_args(
            ["--runtime-root", "/nope/dlux-runtime", "--no-delegate"],
            action="check",
        )
        with patch("composer.dlux_runtime_access.delegate_dlux_update") as delegate:
            self.assertEqual(run_dlux_update(args, []), 2)
        delegate.assert_not_called()

    def test_delegation_runs_the_sibling_container_and_returns_its_status(self):
        args = parse_dlux_update_args(["--runtime-root", "/opt/dlux-runtime"], action="check")
        launcher = _Launcher()
        with patch.object(access, "self_image", return_value="composer:test"):
            code = access.delegate_dlux_update(args, [], launcher=launcher)
        self.assertEqual(code, 0)
        command = launcher.interactive_calls[0]
        self.assertEqual(command[:3], ["docker", "run", "--rm"])
        self.assertIn("app_dlux_runtime:/opt/dlux-runtime:rw", command)
        self.assertIn("composer:test", command)

    def test_a_discovery_failure_is_a_message_not_a_traceback(self):
        args = parse_dlux_update_args(["--runtime-root", "/nope/dlux-runtime"], action="check")
        error = access.RuntimeVolumeError("no volume here")
        with patch("composer.dlux_runtime_access.delegate_dlux_update", side_effect=error):
            self.assertEqual(run_dlux_update(args, []), 2)


class SelfImageTests(unittest.TestCase):
    def test_the_running_image_id_is_preferred(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COMPOSER_SELF_IMAGE", None)
            image = access.self_image(lambda cmd: (True, "sha256:abc\n", ""))
        self.assertEqual(image, "sha256:abc")

    def test_an_explicit_self_image_wins(self):
        with patch.dict(os.environ, {"COMPOSER_SELF_IMAGE": "local/composer:dev"}):
            self.assertEqual(access.self_image(lambda cmd: (True, "sha256:abc", "")),
                             "local/composer:dev")

    def test_a_native_composer_falls_back_to_the_wrapper_image(self):
        from composer.launcher import DEFAULT_SELF_IMAGE

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COMPOSER_SELF_IMAGE", None)
            image = access.self_image(lambda cmd: (False, "", "no such object"))
        self.assertEqual(image, DEFAULT_SELF_IMAGE)


if __name__ == "__main__":
    unittest.main()
