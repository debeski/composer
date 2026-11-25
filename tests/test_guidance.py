"""`check --fix` asks an operator at a terminal, and never blocks anyone else (stack recovery B).

Operator-choice findings become short questions with the recommended answer as
the default. They appear only with a terminal on stdin and without `-y` /
`COMPOSER_ASSUME_YES`; scripted, agent and panel runs keep the WARN and its
advice. A skipped question changes nothing and is listed in the summary.
"""

import io
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from composer.checkup import FAIL, OK, WARN
from composer.launcher import DockerComposeLauncher


def _args(**over):
    base = dict(file=None, dev=False, fix=True, yes=False, deep=False, deep_service="web",
                deep_command="python manage.py dlux_doctor", json=False, beta=False, stable=False,
                no_recreate=False, resident_apply=False)
    base.update(over)
    return SimpleNamespace(**base)


class _Tty(io.StringIO):
    def isatty(self):
        return True


def _reader(*answers):
    queue = list(answers)
    reader = Mock(side_effect=lambda prompt="": queue.pop(0) if queue else (_ for _ in ()).throw(EOFError()))
    return reader


class InteractiveTests(unittest.TestCase):
    def test_only_a_terminal_without_yes_is_asked(self):
        from composer.guidance import interactive

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COMPOSER_ASSUME_YES", None)
            self.assertTrue(interactive(_args(), stdin=_Tty()))
            self.assertFalse(interactive(_args(yes=True), stdin=_Tty()))
            self.assertFalse(interactive(_args(json=True), stdin=_Tty()))
            self.assertFalse(interactive(_args(), stdin=io.StringIO()))
            self.assertFalse(interactive(_args(), stdin=_Tty(), resident=True))
            self.assertFalse(interactive(_args(fix=False), stdin=_Tty()))
        with patch.dict(os.environ, {"COMPOSER_ASSUME_YES": "1"}):
            self.assertFalse(interactive(_args(), stdin=_Tty()))


class AskTests(unittest.TestCase):
    OPTIONS = [("1", "one"), ("2", "two")]

    def _ask(self, *answers, default="2"):
        from composer.guidance import ask

        self.stream = io.StringIO()
        return ask("Pick?", self.OPTIONS, default, reader=_reader(*answers), stream=self.stream)

    def test_enter_takes_the_default(self):
        self.assertEqual(self._ask(""), "2")
        self.assertIn("[s] skip", self.stream.getvalue())

    def test_a_choice_and_a_skip(self):
        self.assertEqual(self._ask("1"), "1")
        self.assertIsNone(self._ask("s"))

    def test_eof_and_three_wrong_answers_change_nothing(self):
        self.assertIsNone(self._ask())
        self.assertIsNone(self._ask("x", "y", "z"))
        self.assertEqual(self._ask("x", "1"), "1")


class FrontProxyQuestionTests(unittest.TestCase):
    def _ask(self, *answers):
        from composer.guidance import ask_front_proxy

        return ask_front_proxy(reader=_reader(*answers), stream=io.StringIO())

    def test_answers(self):
        self.assertEqual(self._ask("1", "10.0.0.5 192.168.10.0/24"), "10.0.0.5 192.168.10.0/24")
        self.assertEqual(self._ask("1", "10.0.0.5,10.0.0.6", "10.0.0.5"), "10.0.0.5")
        self.assertEqual(self._ask("2"), "")
        self.assertEqual(self._ask("3"), "private_ranges")
        self.assertEqual(self._ask(""), "private_ranges", "the default keeps today's behaviour")
        self.assertIsNone(self._ask("s"))
        self.assertIsNone(self._ask("1", ""), "no address given is a skip")


class UpdatePolicyQuestionTests(unittest.TestCase):
    def _ask(self, current, *answers):
        from composer.guidance import ask_update_policy

        return ask_update_policy(current, reader=_reader(*answers), stream=io.StringIO())

    def test_answers(self):
        self.assertEqual(self._ask("all", ""), "all")
        self.assertEqual(self._ask("none", ""), "none")
        self.assertEqual(self._ask("all", "2"), "inline")
        self.assertEqual(self._ask("all", "3"), "none")
        self.assertIsNone(self._ask("all", "s"))


class RedisQuestionTests(unittest.TestCase):
    def test_yes_is_the_default_and_no_keeps_the_file(self):
        from composer.guidance import ask_redis

        self.assertTrue(ask_redis(reader=_reader(""), stream=io.StringIO()))
        self.assertFalse(ask_redis(reader=_reader("n"), stream=io.StringIO()))
        self.assertFalse(ask_redis(reader=_reader(), stream=io.StringIO()))


class SecretsWriterTests(unittest.TestCase):
    def setUp(self):
        holder = TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        (self.root / ".secrets").mkdir()
        self.path = self.root / ".secrets" / ".env"

    def test_it_replaces_appends_and_keeps_everything_else(self):
        from composer.guidance import set_secret_values

        self.path.write_text("DJANGO_SECRET_KEY=abc\n# DLUX_FRONT_PROXY=10.0.0.5\nDLUX_UPDATE_POLICY=all\nTIME_ZONE=UTC", encoding="utf-8")
        self.path.chmod(0o600)
        inode = self.path.stat().st_ino

        set_secret_values(self.root, {"DLUX_UPDATE_POLICY": "inline", "DLUX_FRONT_PROXY": "10.0.0.5"})

        self.assertEqual(
            self.path.read_text(encoding="utf-8"),
            "DJANGO_SECRET_KEY=abc\n# DLUX_FRONT_PROXY=10.0.0.5\nDLUX_UPDATE_POLICY=inline\nTIME_ZONE=UTC\n"
            "DLUX_FRONT_PROXY=10.0.0.5\n",
        )
        self.assertEqual(self.path.stat().st_ino, inode, "rewritten in place")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.root / ".xclude").exists(), "the secrets file is never copied")

    def test_crlf_and_an_empty_value(self):
        from composer.guidance import set_secret_values

        self.path.write_bytes(b"A=1\r\nB=2\r\n")
        set_secret_values(self.root, {"DLUX_FRONT_PROXY": ""})

        self.assertEqual(self.path.read_bytes(), b"A=1\r\nB=2\r\nDLUX_FRONT_PROXY=\r\n")

    def test_a_value_that_would_break_the_file_is_refused(self):
        from composer.guidance import set_secret_values

        self.path.write_text("A=1\n", encoding="utf-8")
        for value in ("a\nB=2", '"quoted"', "x # comment"):
            with self.assertRaises(ValueError):
                set_secret_values(self.root, {"DLUX_FRONT_PROXY": value})
        self.assertEqual(self.path.read_text(encoding="utf-8"), "A=1\n")


class _Project(unittest.TestCase):
    def setUp(self):
        holder = TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        previous = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        (self.root / ".secrets").mkdir()
        (self.root / ".secrets" / ".env").write_text("DJANGO_SECRET_KEY=abc\n", encoding="utf-8")


class QuestionWiringTests(_Project):
    RESULTS = [
        {"level": WARN, "name": "front-proxy", "message": "Caddy trusts every private network"},
        {"level": OK, "name": "update-policy", "message": "Update policy all"},
        {"level": WARN, "name": "redis-persistence", "message": "append-only file"},
    ]

    def _ask(self, *answers, results=None):
        launcher = DockerComposeLauncher()
        launcher.services = ["web", "caddy", "redis", "composer-agent", "composer-executor"]
        launcher.active_compose_files = ["compose.yml"]
        state = {"set": False, "compose": "passed", "caddyfile": "managed", "unsupported": "", "quoted": False,
                 "invalid": []}
        reader = _reader(*answers)
        with patch("composer.front_proxy.inspect", return_value=state), \
                patch("composer.front_proxy.blocked_reason", return_value=""), \
                patch.object(launcher, "build_compose_env", return_value={}):
            launcher._ask_operator_questions(results or self.RESULTS, reader=reader, stream=io.StringIO())
        return launcher

    def test_each_choice_is_recorded_and_a_skip_is_listed(self):
        launcher = self._ask("1", "10.0.0.5", "s", "n")

        self.assertEqual(launcher._operator_choices, {"DLUX_FRONT_PROXY": "10.0.0.5", "redis": "keep"})
        self.assertEqual([entry["name"] for entry in launcher._skipped_questions], ["update-policy", "redis-persistence"])
        self.assertTrue(all(entry["command"].startswith("./start.sh") for entry in launcher._skipped_questions))

    def test_a_policy_already_in_the_secrets_file_is_not_asked(self):
        (self.root / ".secrets" / ".env").write_text("DLUX_UPDATE_POLICY=inline\n", encoding="utf-8")
        launcher = self._ask("3", "")

        self.assertEqual(launcher._operator_choices, {"DLUX_FRONT_PROXY": "private_ranges"})

    def test_collect_checkup_asks_only_when_interactive(self):
        launcher = DockerComposeLauncher()
        launcher.services = []
        stubs = {"_check_docker": [], "_check_wrappers": [], "_check_composer_channel": {"level": OK, "name": "c", "message": ""},
                 "_check_compose_files": {"level": OK, "name": "f", "message": ""},
                 "_check_compose_parses": {"level": OK, "name": "p", "message": ""},
                 "_check_secrets": {"level": OK, "name": "s", "message": ""}, "resolve_active_compose_files": None,
                 "discover_services": True, "_fix_composer_pin": [], "_maybe_fix": []}
        for name, value in stubs.items():
            patch.object(launcher, name, return_value=value).start()
        hold = patch("composer.checkup.check_lock.hold").start()
        hold.return_value.__enter__ = lambda *a: None
        hold.return_value.__exit__ = lambda *a: False
        self.addCleanup(patch.stopall)
        asked = patch.object(launcher, "_ask_operator_questions").start()

        with patch("composer.guidance.interactive", return_value=False):
            launcher.collect_checkup(_args(yes=True))
        asked.assert_not_called()
        with patch("composer.guidance.interactive", return_value=True):
            launcher.collect_checkup(_args())
        asked.assert_called_once()


class ChoiceApplyTests(_Project):
    def _fix(self, choices):
        launcher = DockerComposeLauncher()
        launcher.services = ["web", "caddy", "composer-agent", "composer-executor"]
        launcher.active_compose_files = ["compose.yml"]
        launcher._operator_choices = dict(choices)
        order, seen = [], {}

        def confirm(action, consequences, assume_yes=False):
            seen["lines"] = list(consequences)
            order.append("confirm")
            return True

        def migrate(*a, **k):
            order.append(("front-proxy", (self.root / ".secrets" / ".env").read_text(encoding="utf-8")))
            return {"applied": True, "files": [".proxy/Caddyfile"], "backup_root": "/x", "recreated": [], "notes": []}

        with (
            patch("composer.agent_installer.enable_executor", return_value={"files": []}),
            patch("composer.agent_installer.enable_post_start_label", return_value={"files": []}),
            patch("composer.agent_installer.migrate_dlux_updater", return_value={"files": []}),
            patch("composer.agent_installer.normalize_restart_labels", return_value={"files": []}),
            patch("composer.checkup.inspect_legacy_proxy_routes", return_value={"recognized": [], "unsupported": []}),
            patch("composer.wrappers.inspect_wrappers", return_value=[]),
            patch("composer.front_proxy.inspect", return_value={"files": [".proxy/Caddyfile"]}),
            patch("composer.front_proxy.blocked_reason", return_value=""),
            patch("composer.front_proxy.migrate", side_effect=migrate),
            patch("composer.redis_persistence.preview", return_value={"files": ["compose.yml"], "diff": "", "note": ""}),
            patch("composer.checkup.confirm", side_effect=confirm),
            patch.object(launcher, "build_compose_env", return_value={}),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            fixes = launcher._maybe_fix(_args(), [])
        return launcher, fixes, order, "\n".join(seen.get("lines", []))

    def test_the_answer_is_listed_written_first_and_kept_for_the_recreate(self):
        launcher, fixes, order, text = self._fix({"DLUX_FRONT_PROXY": "10.0.0.5"})

        self.assertIn("DLUX_FRONT_PROXY=10.0.0.5", text)
        self.assertIn("not copied", text)
        self.assertEqual(order[0], "confirm")
        self.assertIn("DLUX_FRONT_PROXY=10.0.0.5\n", order[1][1], "written before the front-proxy repair")
        self.assertEqual(launcher._written_secrets, {"DLUX_FRONT_PROXY": "10.0.0.5"})
        written = [f for f in fixes if f["name"] == "fix:secrets"][0]
        self.assertEqual(written["level"], OK)
        self.assertNotIn("10.0.0.5", written["message"], "values are not echoed")

    def test_keeping_the_append_only_file_drops_the_redis_rewrite(self):
        launcher = DockerComposeLauncher()
        launcher.services = ["web", "redis"]
        launcher.active_compose_files = ["compose.yml"]
        launcher._operator_choices = {"redis": "keep"}
        with (
            patch("composer.agent_installer.enable_post_start_label", return_value={"files": []}),
            patch("composer.agent_installer.migrate_dlux_updater", return_value={"files": []}),
            patch("composer.agent_installer.normalize_restart_labels", return_value={"files": []}),
            patch("composer.agent_installer.install_composer_stack", return_value={"files": []}),
            patch("composer.checkup.inspect_legacy_proxy_routes", return_value={"recognized": [], "unsupported": []}),
            patch("composer.wrappers.inspect_wrappers", return_value=[]),
            patch("composer.redis_persistence.preview", return_value={"files": ["compose.yml"], "diff": "", "note": ""}),
            patch.object(launcher, "build_compose_env", return_value={}),
        ):
            plan = launcher._fix_plan(_args(), [])

        self.assertEqual(plan["redis_files"], [])

    def test_the_overlay_reaches_the_recreate_and_is_restored(self):
        launcher = DockerComposeLauncher()
        launcher._written_secrets = {"DLUX_FRONT_PROXY": "10.0.0.5"}
        os.environ.pop("DLUX_FRONT_PROXY", None)
        with patch.object(launcher, "resolve_secrets", return_value=(True, "")):
            with launcher._deployment_environment():
                self.assertEqual(os.environ["DLUX_FRONT_PROXY"], "10.0.0.5")
        self.assertNotIn("DLUX_FRONT_PROXY", os.environ)


class SummaryTests(unittest.TestCase):
    RESULTS = [
        {"level": OK, "name": "front-proxy", "message": ""},
        {"level": WARN, "name": "registry-login", "message": "needs a login",
         "fix": "Put a read-only login in the secrets file, then run './start.sh' so they read it."},
        {"level": FAIL, "name": "services", "message": "redis keeps restarting (7 restart(s)); worker is unhealthy",
         "fix": "Read the service's log ('./start.sh logs <service>')"},
        {"level": WARN, "name": "update-policy", "message": "x"},
    ]
    FIXES = [
        {"level": OK, "name": "fix:front-proxy", "message": "done"},
        {"level": OK, "name": "fix:recreate", "message": "Recreated caddy; healthy and up 10 s."},
        {"level": WARN, "name": "fix:recreate-resident",
         "message": "composer-agent changed ... Run './start.sh' on the server: it recreates them."},
    ]
    SKIPPED = [{"name": "update-policy", "command": "./start.sh check --fix asks again"}]

    def test_the_three_lists(self):
        from composer.guidance import summary

        report = summary(self.RESULTS, self.FIXES, self.SKIPPED, before={"front-proxy": WARN})

        self.assertEqual(report["healthy"], ["front-proxy"])
        self.assertEqual(dict(report["still"]), {
            "registry-login": "./start.sh",
            "services": "./start.sh logs redis worker",
            "fix:recreate-resident": "./start.sh",
        })
        self.assertEqual(report["skipped"], [("update-policy", "./start.sh check --fix asks again")])

    def test_it_prints_after_a_text_fix_run_only(self):
        launcher = DockerComposeLauncher()
        launcher._fix_summary = {"healthy": ["front-proxy"], "still": [("registry-login", "./start.sh")],
                                 "skipped": [("update-policy", "./start.sh check --fix asks again")]}
        with patch.object(launcher, "collect_checkup", return_value=(self.RESULTS[:1], [])), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            launcher.run_checkup(_args())
        text = out.getvalue()
        self.assertIn("Healthy now: front-proxy", text)
        self.assertIn("Still needs you:", text)
        self.assertIn("registry-login: ./start.sh", text)
        self.assertIn("Skipped by your choice:", text)

        with patch.object(launcher, "collect_checkup", return_value=(self.RESULTS[:1], [])), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            launcher.run_checkup(_args(json=True))
        self.assertNotIn("Healthy now", out.getvalue())

        launcher._fix_summary = None
        with patch.object(launcher, "collect_checkup", return_value=(self.RESULTS[:1], [])), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            launcher.run_checkup(_args(fix=False))
        self.assertNotIn("Still needs you", out.getvalue())


if __name__ == "__main__":
    unittest.main()
