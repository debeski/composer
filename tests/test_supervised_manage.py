import unittest
from unittest.mock import patch

from composer.constants import SUPERVISED_ONE_SHOT_PREFIX
from composer.launcher import DockerComposeLauncher

SUPERVISED_WEB = {
    "services": {
        "web": {"command": ["python", "-m", "dlux.updater.supervisor", "--", "bash", "-c", "gunicorn"]},
        "db": {"command": ["postgres"]},
    }
}


class SupervisedManageTests(unittest.TestCase):
    def setUp(self):
        self.launcher = DockerComposeLauncher()

    def _exec(self, service, command, **kwargs):
        with (
            patch.object(self.launcher, "compose_config_json", return_value=SUPERVISED_WEB),
            patch.object(self.launcher, "resolve_compose_cli", return_value=["docker", "compose"]),
            patch.object(self.launcher, "build_compose_base_args", return_value=[]),
            patch.object(self.launcher, "build_compose_env", return_value={}),
            patch.object(self.launcher, "run_command_interactive", return_value=0) as run,
        ):
            self.launcher.exec_in_service(service, command, **kwargs)
        return run.call_args.args[0]

    def test_manage_command_runs_under_the_supervisor_when_the_service_does(self):
        argv = self._exec("web", ["collectstatic", "--noinput"], manage=True)
        prefix = list(SUPERVISED_ONE_SHOT_PREFIX)
        start = argv.index(prefix[0])
        self.assertEqual(argv[start:], prefix + ["python", "manage.py", "collectstatic", "--noinput"])

    def test_shell_manage_command_wraps_the_shell(self):
        argv = self._exec("web", ["migrate"], manage=True, shell=True)
        self.assertEqual(argv[-len(SUPERVISED_ONE_SHOT_PREFIX) - 3:-3], list(SUPERVISED_ONE_SHOT_PREFIX))
        self.assertEqual(argv[-3:], ["sh", "-c", "python manage.py migrate"])

    def test_unsupervised_service_and_plain_commands_are_unchanged(self):
        self.assertNotIn("dlux.updater.supervisor", self._exec("db", ["shell"], manage=True))
        self.assertNotIn("dlux.updater.supervisor", self._exec("web", ["ls"]))

    def test_an_already_supervised_command_is_not_wrapped_twice(self):
        command = list(SUPERVISED_ONE_SHOT_PREFIX) + ["python", "manage.py", "migrator"]
        with patch.object(self.launcher, "compose_config_json", return_value=SUPERVISED_WEB):
            self.assertEqual(self.launcher.supervised_prefix("web", command), [])

    def test_string_commands_are_detected(self):
        config = {"services": {"web": {"command": "python -m dlux.updater.supervisor -- gunicorn"}}}
        with patch.object(self.launcher, "compose_config_json", return_value=config):
            self.assertEqual(self.launcher.supervised_prefix("web", ["python"]), list(SUPERVISED_ONE_SHOT_PREFIX))

    def test_unreadable_config_falls_back_to_the_bare_command(self):
        with patch.object(self.launcher, "compose_config_json", return_value=None):
            self.assertEqual(self.launcher.supervised_prefix("web", ["python"]), [])

    def test_deep_doctor_runs_under_the_supervisor(self):
        with (
            patch.object(self.launcher, "compose_config_json", return_value=SUPERVISED_WEB),
            patch.object(self.launcher, "run_docker_compose", return_value=(True, "{}", "")) as run,
        ):
            self.launcher._run_deep("web", "python manage.py dlux_doctor")
        self.assertEqual(
            run.call_args.args[0],
            ["exec", "-T", "web", *SUPERVISED_ONE_SHOT_PREFIX, "python", "manage.py", "dlux_doctor"],
        )
