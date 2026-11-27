"""R2-002 / R2-126 (3): a failed deploy blamed `web` and quoted only `~~~~^^`.

`worker`'s `pre_start` hook crashed with a Python traceback; `web` waited for the
migrations it never applied and turned unhealthy. Composer quoted the caret line
Compose took as the hook's last output, and advised excluding `web`.
"""
import unittest

from composer import failure_hints
from composer.launcher import DockerComposeLauncher

COMPOSE_ERR = (
    'Container testbed-breakit2-runtime-worker-1 service "worker" pre_start[0] exited with code 1: ~~~~^^\n'
    "AttributeError: 'NoneType' object has no attribute 'get' (hook container f87c3b0bc536 retained for inspection)\n"
    'service "worker" pre_start[0] exited with code 1: ~~~~^^\n'
)
WORKER_LOGS = (
    'worker-1  |   File "/usr/local/lib/python3.14/site-packages/dlux/updater/supervisor.py", line 344, in <module>\n'
    "worker-1  |     raise SystemExit(main())\n"
    "worker-1  |                      ~~~~^^\n"
    'worker-1  |   File "/usr/local/lib/python3.14/site-packages/dlux/updater/supervisor.py", line 191, in resolve_release\n'
    'worker-1  |     version = str(payload.get("version") or "").strip()\n'
    "worker-1  |                   ^^^^^^^^^^^\n"
    "worker-1  | AttributeError: 'NoneType' object has no attribute 'get'\n"
    "worker-1  | Migrations failed; refusing to serve against an unmigrated database.\n"
    "worker-1  |  \n"
)
WEB_LOGS = "web-1  | Waiting for migrations (worker applies them)...\n" * 5


class CulpritTests(unittest.TestCase):
    def setUp(self):
        self.launcher = DockerComposeLauncher()
        self.launcher.get_compose_ps_entries = lambda include_all=False: (True, [
            {"Service": "web", "State": "running", "Health": "unhealthy"},
            {"Service": "caddy", "State": "running", "Health": ""},
        ], "")
        logs = {"worker": WORKER_LOGS, "web": WEB_LOGS}
        self.launcher.run_docker_compose = lambda args, timeout=None: (True, logs.get(args[-1], ""), "")

    def test_the_hook_line_quotes_the_exception_not_the_caret(self):
        clarified = failure_hints.clarify_hook_lines(COMPOSE_ERR)
        self.assertNotIn("~~~~^^", clarified)
        self.assertIn(
            "pre_start[0] exited with code 1: AttributeError: 'NoneType' object has no attribute 'get'", clarified)

    def test_the_failed_hooks_service_is_the_culprit(self):
        self.assertEqual(failure_hints.hook_failures(COMPOSE_ERR), ["worker"])
        diagnostics = self.launcher.collect_service_diagnostics(culprits=failure_hints.hook_failures(COMPOSE_ERR))
        self.assertIn("COMPOSER_EXCLUDE_SERVICES=worker ", diagnostics)
        self.assertNotIn("COMPOSER_EXCLUDE_SERVICES=web", diagnostics)
        self.assertIn("./start.sh logs worker\n", diagnostics)
        self.assertLess(diagnostics.index("worker logs:"), diagnostics.index("web logs:"))

    def test_quoted_logs_carry_no_caret_or_blank_lines(self):
        diagnostics = self.launcher.collect_service_diagnostics(culprits=["worker"])
        worker = diagnostics.split("worker logs:\n", 1)[1].split("\n\n", 1)[0]
        self.assertNotIn("~~~~^^", worker)
        self.assertNotIn("^^^^^^^^^^^", worker)
        self.assertIn("AttributeError: 'NoneType' object has no attribute 'get'", worker)

    def test_a_crash_looping_service_comes_before_the_unhealthy_one(self):
        self.launcher.get_compose_ps_entries = lambda include_all=False: (True, [
            {"Service": "web", "State": "running", "Health": "unhealthy"},
            {"Service": "worker", "State": "restarting", "Health": ""},
        ], "")
        diagnostics = self.launcher.collect_service_diagnostics()
        self.assertIn("COMPOSER_EXCLUDE_SERVICES=worker ", diagnostics)
        self.assertLess(diagnostics.index("worker logs:"), diagnostics.index("web logs:"))

    def test_without_a_culprit_the_unhealthy_service_is_named(self):
        diagnostics = self.launcher.collect_service_diagnostics()
        self.assertIn("COMPOSER_EXCLUDE_SERVICES=web ", diagnostics)

    def test_the_meaningful_line(self):
        self.assertEqual(failure_hints.meaningful_line(WORKER_LOGS),
                         "AttributeError: 'NoneType' object has no attribute 'get'")
        self.assertEqual(failure_hints.meaningful_line("starting\n  ~~~^^\n\n"), "starting")
        self.assertEqual(failure_hints.meaningful_line("~~^^\n"), "")


if __name__ == "__main__":
    unittest.main()
