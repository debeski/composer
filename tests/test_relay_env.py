"""The SMTP relay's non-secret settings are shared with web and worker.

DjangoLux's Email step shows the relay's host, port, encryption and user from
web's environment. These tests pin how `check` reports each state, how
`check --fix` moves the generated lines into the shared x-environment block
(keeping each stack's own defaults, leaving custom values alone), and that the
relay password never leaves smtp-relay or shows up in a preview, a message or
an archive.
"""

import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from composer import relay_env as re_
from composer.checkup import OK, WARN
from composer.launcher import DockerComposeLauncher

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCHEMA_4 = FIXTURES / "schema-4"
OLD_LAYOUT = FIXTURES / "old-layout"
DLUX_PROJECT = (
    Path(__file__).resolve().parents[3] / "pkg-django-lux" / "main" / "dlux" / "scaffold" / "templates" / "project"
)
COMPOSE = (SCHEMA_4 / "compose.yml").read_text(encoding="utf-8")
SECRET = "s3cret-relay-pass"

RELAY_LINES = (
    '      SMTP_RELAY_HOST: "${SMTP_RELAY_HOST:-smtp.gmail.com}"\n'
    '      SMTP_RELAY_PORT: "${SMTP_RELAY_PORT:-587}"\n'
    '      SMTP_RELAY_USE_TLS: "${SMTP_RELAY_USE_TLS:-True}"\n'
    '      SMTP_RELAY_USER: "${SMTP_RELAY_USER:-}"\n'
)
MAIL_LINES = (
    "  # The relay's provider settings. Shared so the Email step can show what the\n"
    "  # relay uses; the password itself reaches smtp-relay only, web gets set/unset.\n"
    "  # SMTP_RELAY_USE_SSL empty: SSL on port 465, STARTTLS (SMTP_RELAY_USE_TLS) otherwise.\n"
    '  SMTP_RELAY_HOST: "${SMTP_RELAY_HOST:-smtp.gmail.com}"\n'
    '  SMTP_RELAY_PORT: "${SMTP_RELAY_PORT:-587}"\n'
    '  SMTP_RELAY_USE_TLS: "${SMTP_RELAY_USE_TLS:-True}"\n'
    '  SMTP_RELAY_USE_SSL: "${SMTP_RELAY_USE_SSL:-}"\n'
    '  SMTP_RELAY_USER: "${SMTP_RELAY_USER:-}"\n'
    '  SMTP_RELAY_PASSWORD_SET: "${SMTP_RELAY_PASSWORD:+yes}"\n'
)
TLS_LINES = (
    "  # The relay signs in only to a provider whose certificate a trusted CA issued for\n"
    "  # its host. SMTP_RELAY_CA_FILE adds a private CA (a bundle mounted into smtp-relay);\n"
    "  # SMTP_RELAY_TLS_VERIFY=False switches the check off.\n"
    '  SMTP_RELAY_TLS_VERIFY: "${SMTP_RELAY_TLS_VERIFY:-True}"\n'
    '  SMTP_RELAY_CA_FILE: "${SMTP_RELAY_CA_FILE:-}"\n'
)
SHARED_LINES = MAIL_LINES + TLS_LINES
FROM_LINE = '  DEFAULT_FROM_EMAIL: "${DEFAULT_FROM_EMAIL:-webmaster@localhost}"\n'
PASSWORD_LINE = '      SMTP_RELAY_PASSWORD: "${SMTP_RELAY_PASSWORD:-}"\n'
EXPECTED = COMPOSE.replace(RELAY_LINES, "").replace(FROM_LINE, FROM_LINE + SHARED_LINES)
MAIL_ONLY = COMPOSE.replace(RELAY_LINES, "").replace(FROM_LINE, FROM_LINE + MAIL_LINES)


def _services(text):
    return yaml.safe_load(text)["services"]


def _copy(source: Path, case: unittest.TestCase) -> Path:
    holder = tempfile.TemporaryDirectory()
    case.addCleanup(holder.cleanup)
    root = Path(holder.name)
    shutil.copytree(source, root, dirs_exist_ok=True)
    return root.resolve()


def _every_file_text(root: Path) -> str:
    return "".join(
        path.read_text(encoding="utf-8", errors="replace") for path in root.rglob("*")
        if path.is_file() and ".secrets" not in path.parts
    )


class ClassifyTests(unittest.TestCase):
    def test_the_generated_stack_is_legacy_and_its_rewrite_current(self):
        state = re_.classify(COMPOSE)
        self.assertEqual((state["state"], state["pending"]), (re_.LEGACY, [re_.MAIL, re_.TLS]))
        self.assertEqual(re_.classify(EXPECTED)["state"], re_.CURRENT)

    def test_shared_mail_lines_without_the_certificate_lines_are_legacy(self):
        state = re_.classify(MAIL_ONLY)
        self.assertEqual((state["state"], state["pending"]), (re_.LEGACY, [re_.TLS]))

    def test_certificate_settings_composer_cannot_share_are_custom(self):
        literal = COMPOSE.replace(PASSWORD_LINE, PASSWORD_LINE + '      SMTP_RELAY_CA_FILE: "/etc/dlux/ca.pem"\n')
        state = re_.classify(literal)
        self.assertEqual(state["state"], re_.CUSTOM)
        self.assertIn("smtp-relay sets SMTP_RELAY_CA_FILE to its own value", state["details"])
        self.assertNotIn("/etc/dlux/ca.pem", json.dumps(state))
        anchor = EXPECTED.replace('"${SMTP_RELAY_TLS_VERIFY:-True}"', '"False"')
        self.assertIn("x-environment sets SMTP_RELAY_TLS_VERIFY differently from the generated lines",
                      re_.classify(anchor)["details"])
        half = MAIL_ONLY.replace(MAIL_LINES, MAIL_LINES + '  SMTP_RELAY_TLS_VERIFY: "${SMTP_RELAY_TLS_VERIFY:-True}"\n')
        self.assertIn("x-environment lacks SMTP_RELAY_CA_FILE", re_.classify(half)["details"])

    def test_no_relay_service_is_no_service(self):
        self.assertEqual(re_.classify((OLD_LAYOUT / "compose.yml").read_text(encoding="utf-8"))["state"],
                         re_.NO_SERVICE)
        other = COMPOSE.replace('command: ["python", "-m", "dlux.smtp_relay"]', 'command: ["postfix"]')
        self.assertEqual(re_.classify(other)["state"], re_.NO_SERVICE)

    def test_the_retired_tools_relay_counts(self):
        tools = COMPOSE.replace('"dlux.smtp_relay"', '"tools.smtp_relay"')
        self.assertEqual(re_.classify(tools)["state"], re_.LEGACY)

    def test_a_literal_value_in_smtp_relay_is_custom(self):
        state = re_.classify(COMPOSE.replace('"${SMTP_RELAY_HOST:-smtp.gmail.com}"', '"smtp.office365.com"'))
        self.assertEqual(state["state"], re_.CUSTOM)
        self.assertIn("smtp-relay sets SMTP_RELAY_HOST to its own value", state["details"])
        self.assertNotIn("office365", json.dumps(state))

    def test_a_password_written_into_the_file_is_custom_and_never_quoted(self):
        state = re_.classify(COMPOSE.replace('"${SMTP_RELAY_PASSWORD:-}"', f'"{SECRET}"'))
        self.assertEqual(state["state"], re_.CUSTOM)
        self.assertNotIn(SECRET, json.dumps(state))
        state = re_.classify(COMPOSE.replace('"${SMTP_RELAY_PASSWORD:-}"', '"${SMTP_RELAY_PASSWORD:-' + SECRET + '}"'))
        self.assertEqual(state["state"], re_.CUSTOM)
        self.assertNotIn(SECRET, json.dumps(state))
        missing = COMPOSE.replace('      SMTP_RELAY_PASSWORD: "${SMTP_RELAY_PASSWORD:-}"\n', "")
        self.assertIn("smtp-relay is not passed SMTP_RELAY_PASSWORD", re_.classify(missing)["details"])

    def test_a_shared_password_is_custom_and_flagged(self):
        text = COMPOSE.replace(FROM_LINE, FROM_LINE + '  SMTP_RELAY_PASSWORD: "${SMTP_RELAY_PASSWORD:-}"\n')
        state = re_.classify(text)
        self.assertEqual(state["state"], re_.CUSTOM)
        self.assertTrue(state["shared_password"])

    def test_an_anchor_that_sets_some_lines_its_own_way_is_custom(self):
        partial = COMPOSE.replace(FROM_LINE, FROM_LINE + '  SMTP_RELAY_USE_SSL: "True"\n')
        state = re_.classify(partial)
        self.assertEqual(state["state"], re_.CUSTOM)
        self.assertIn("x-environment sets SMTP_RELAY_USE_SSL differently from the generated lines", state["details"])
        self.assertTrue(any(detail.startswith("x-environment lacks SMTP_RELAY_HOST") for detail in state["details"]))
        flagged = EXPECTED.replace('"${SMTP_RELAY_PASSWORD:+yes}"', '"yes"')
        self.assertEqual(re_.classify(flagged)["state"], re_.CUSTOM)

    def test_smtp_relay_overriding_a_shared_line_is_custom(self):
        override = EXPECTED.replace(
            '      SMTP_RELAY_PASSWORD: "${SMTP_RELAY_PASSWORD:-}"\n',
            '      SMTP_RELAY_PASSWORD: "${SMTP_RELAY_PASSWORD:-}"\n      SMTP_RELAY_HOST: "mail.example"\n',
        )
        state = re_.classify(override)
        self.assertEqual(state["state"], re_.CUSTOM)
        self.assertIn("smtp-relay sets its own SMTP_RELAY_HOST", state["details"])

    def test_layouts_composer_cannot_edit_are_custom(self):
        self.assertEqual(re_.classify(COMPOSE.replace("      <<: *de\n      POSTGRES_USER: ${POSTGRES_USER:-admin}\n"
                                                      "      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-admin_pass}\n"
                                                      "      DJANGO_SECRET_KEY: ${DJANGO_SECRET_KEY:-local_secret}\n"
                                                      '      DJANGO_SETTINGS_MODULE: "config.settings"\n'
                                                      '      SMTP_RELAY_LISTEN_HOST: "0.0.0.0"\n',
                                                      '      SMTP_RELAY_LISTEN_HOST: "0.0.0.0"\n'))["state"],
                         re_.CUSTOM)
        listed = COMPOSE.replace('      SMTP_RELAY_LISTEN_HOST: "0.0.0.0"\n', '      - SMTP_RELAY_LISTEN_HOST=0.0.0.0\n')
        self.assertIn("not a block mapping", " ".join(re_.classify(listed)["details"]))
        unanchored = COMPOSE.replace("  &de\n", "").replace("<<: *de", "<<: *other")
        self.assertEqual(re_.classify(unanchored)["state"], re_.CUSTOM)

    def test_a_missing_generated_line_is_custom(self):
        state = re_.classify(COMPOSE.replace('      SMTP_RELAY_USER: "${SMTP_RELAY_USER:-}"\n', ""))
        self.assertEqual(state["state"], re_.CUSTOM)
        self.assertIn("smtp-relay is not passed SMTP_RELAY_USER", state["details"])


class MigrateComposeTests(unittest.TestCase):
    def test_the_lines_move_into_the_shared_block_after_default_from_email(self):
        text = re_.migrate_compose(COMPOSE)
        self.assertEqual(text, EXPECTED)
        services = _services(text)
        relay, web = services["smtp-relay"]["environment"], services["web"]["environment"]
        for key in re_.SHARED_KEYS:
            self.assertEqual(web[key], relay[key])
        self.assertEqual(web["SMTP_RELAY_PASSWORD_SET"], "${SMTP_RELAY_PASSWORD:+yes}")
        self.assertNotIn("SMTP_RELAY_PASSWORD", web)
        self.assertEqual(relay["SMTP_RELAY_PASSWORD"], "${SMTP_RELAY_PASSWORD:-}")
        self.assertEqual(relay["SMTP_RELAY_LISTEN_PORT"], "1025")
        self.assertNotIn("SMTP_RELAY_PASSWORD", services["worker"]["environment"] if "worker" in services
                         else services["celery"]["environment"])

    def test_it_is_idempotent(self):
        once = re_.migrate_compose(COMPOSE)
        self.assertEqual(re_.migrate_compose(once), once)

    def test_the_stack_s_own_defaults_are_kept(self):
        own = COMPOSE.replace("${SMTP_RELAY_HOST:-smtp.gmail.com}", "${SMTP_RELAY_HOST:-smtp.office365.com}").replace(
            "${SMTP_RELAY_PORT:-587}", "${SMTP_RELAY_PORT-465}")
        text = re_.migrate_compose(own)
        self.assertIn('  SMTP_RELAY_HOST: "${SMTP_RELAY_HOST:-smtp.office365.com}"\n', text)
        self.assertIn('  SMTP_RELAY_PORT: "${SMTP_RELAY_PORT-465}"\n', text)
        self.assertEqual(re_.classify(text)["state"], re_.CURRENT)

    def test_an_unquoted_line_and_a_use_ssl_line_are_moved(self):
        own = COMPOSE.replace('SMTP_RELAY_USER: "${SMTP_RELAY_USER:-}"', "SMTP_RELAY_USER: ${SMTP_RELAY_USER:-}  # login").replace(
            '      SMTP_RELAY_PASSWORD: "${SMTP_RELAY_PASSWORD:-}"\n',
            '      SMTP_RELAY_USE_SSL: "${SMTP_RELAY_USE_SSL:-True}"\n      SMTP_RELAY_PASSWORD: "${SMTP_RELAY_PASSWORD:-}"\n',
        )
        text = re_.migrate_compose(own)
        self.assertIn('  SMTP_RELAY_USE_SSL: "${SMTP_RELAY_USE_SSL:-True}"\n', text)
        self.assertIn('  SMTP_RELAY_USER: "${SMTP_RELAY_USER:-}"\n', text)
        relay = re_._relay_block(text.splitlines(keepends=True), text)["entries"]
        self.assertEqual(sorted(key for key in relay if key.startswith("SMTP_RELAY_")),
                         ["SMTP_RELAY_LISTEN_HOST", "SMTP_RELAY_LISTEN_PORT", "SMTP_RELAY_PASSWORD"])

    def test_a_one_line_anchor_without_default_from_email_gains_the_lines_at_its_end(self):
        own = COMPOSE.replace("x-environment:\n  # Database settings\n  &de\n", "x-environment: &de\n").replace(
            FROM_LINE, "").replace("\nservices:\n", "\n# Services\nservices:\n", 1)
        text = re_.migrate_compose(own)
        self.assertIn('  EMAIL_USE_SSL: "False"\n' + SHARED_LINES + "\n# Services\nservices:\n", text)
        self.assertEqual(re_.classify(text)["state"], re_.CURRENT)

    def test_a_stack_with_the_mail_lines_gains_the_certificate_lines_after_them(self):
        text = re_.migrate_compose(MAIL_ONLY)
        self.assertEqual(text, EXPECTED)
        self.assertEqual(re_.migrate_compose(text), text)

    def test_certificate_lines_in_smtp_relay_move_with_their_defaults(self):
        own = COMPOSE.replace(PASSWORD_LINE, PASSWORD_LINE + '      SMTP_RELAY_TLS_VERIFY: "${SMTP_RELAY_TLS_VERIFY:-False}"\n')
        text = re_.migrate_compose(own)
        self.assertIn('  SMTP_RELAY_TLS_VERIFY: "${SMTP_RELAY_TLS_VERIFY:-False}"\n', text)
        self.assertNotIn("      SMTP_RELAY_TLS_VERIFY", text)
        self.assertEqual(re_.classify(text)["state"], re_.CURRENT)

    def test_an_operator_s_ca_mount_is_left_and_none_is_added(self):
        mount = "    volumes:\n      - ./.secrets/smtp-relay-ca.pem:/etc/dlux/smtp-relay-ca.pem:ro\n"
        own = COMPOSE.replace(PASSWORD_LINE, PASSWORD_LINE + mount)
        text = re_.migrate_compose(own)
        self.assertIn(PASSWORD_LINE + mount, text)
        self.assertEqual(re_.migrate_compose(COMPOSE).count("smtp-relay-ca.pem"), 0)

    def test_windows_line_endings_are_kept(self):
        text = re_.migrate_compose(COMPOSE.replace("\n", "\r\n"))
        self.assertEqual(text, EXPECTED.replace("\n", "\r\n"))

    def test_a_custom_stack_is_refused(self):
        with self.assertRaises(re_.RelayEnvError):
            re_.migrate_compose(COMPOSE.replace('"${SMTP_RELAY_HOST:-smtp.gmail.com}"', '"smtp.office365.com"'))


class PlanTests(unittest.TestCase):
    def test_inspect_names_the_file_and_the_development_override_is_not_edited(self):
        root = _copy(SCHEMA_4, self)
        state = re_.inspect(root, ["compose.yml", "compose.dev.yml"])
        self.assertEqual((state["state"], state["compose_file"], state["files"]), (re_.LEGACY, "compose.yml",
                                                                                   ["compose.yml"]))

    def test_a_custom_literal_value_is_left_in_place(self):
        root = _copy(SCHEMA_4, self)
        compose = root / "compose.yml"
        text = COMPOSE.replace('"${SMTP_RELAY_HOST:-smtp.gmail.com}"', '"smtp.office365.com"')
        compose.write_text(text, encoding="utf-8")
        state = re_.inspect(root, ["compose.yml"])
        self.assertEqual((state["state"], state["files"]), (re_.CUSTOM, []))
        outcome = re_.migrate(str(root), ["compose.yml"], environment={}, command_runner=FakeDocker(root))
        self.assertEqual((outcome["applied"], outcome["files"]), (False, []))
        self.assertEqual(compose.read_text(encoding="utf-8"), text)
        self.assertFalse((root / ".xclude").exists())

    def test_preview_writes_nothing_and_never_shows_the_password(self):
        root = _copy(SCHEMA_4, self)
        (root / ".secrets").mkdir()
        (root / ".secrets" / ".env").write_text(f"SMTP_RELAY_PASSWORD={SECRET}\nSMTP_RELAY_USER=me@example.com\n",
                                               encoding="utf-8")
        preview = re_.preview(str(root), ["compose.yml"])
        self.assertEqual(preview["files"], ["compose.yml"])
        self.assertIn('+  SMTP_RELAY_PASSWORD_SET: "${SMTP_RELAY_PASSWORD:+yes}"', preview["diff"])
        self.assertIn('-      SMTP_RELAY_HOST: "${SMTP_RELAY_HOST:-smtp.gmail.com}"', preview["diff"])
        self.assertIn("password stays on smtp-relay", preview["note"])
        self.assertNotIn(SECRET, json.dumps(preview))
        self.assertEqual((root / "compose.yml").read_text(encoding="utf-8"), COMPOSE)

    def test_a_password_value_in_a_diff_is_hidden(self):
        diff = (
            '       SMTP_RELAY_PASSWORD: "' + SECRET + '"\n'
            '-      - "SMTP_RELAY_PASSWORD=' + SECRET + '"\n'
            '       SMTP_RELAY_PASSWORD: "${SMTP_RELAY_PASSWORD:-}"\n'
            '+  SMTP_RELAY_PASSWORD_SET: "${SMTP_RELAY_PASSWORD:+yes}"\n'
        )
        masked = re_._mask_password(diff)
        self.assertNotIn(SECRET, masked)
        self.assertIn('       SMTP_RELAY_PASSWORD: [hidden]\n', masked)
        self.assertIn('       SMTP_RELAY_PASSWORD: "${SMTP_RELAY_PASSWORD:-}"\n', masked)
        self.assertIn('+  SMTP_RELAY_PASSWORD_SET: "${SMTP_RELAY_PASSWORD:+yes}"\n', masked)

    def test_the_operations_preview_never_shows_the_password(self):
        from composer.ops import _relay_env_repair

        root = _copy(SCHEMA_4, self)
        (root / ".secrets").mkdir()
        (root / ".secrets" / ".env").write_text(f"SMTP_RELAY_PASSWORD={SECRET}\n", encoding="utf-8")
        cwd = os.getcwd()
        os.chdir(root)
        self.addCleanup(os.chdir, cwd)
        repairs = _relay_env_repair(SimpleNamespace(_maintenance_compose_files=lambda: ["compose.yml"]))
        self.assertEqual([repair["name"] for repair in repairs], ["relay-env"])
        self.assertEqual(repairs[0]["files"], ["compose.yml"])
        self.assertIn("SMTP_RELAY_PASSWORD_SET", repairs[0]["diff"])
        self.assertNotIn(SECRET, json.dumps(repairs))
        Path("compose.yml").write_text(EXPECTED, encoding="utf-8")
        self.assertEqual(_relay_env_repair(SimpleNamespace(_maintenance_compose_files=lambda: ["compose.yml"])), [])


_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-+])([^}]*))?\}")


def _interpolate(value, environment):
    def replace(match):
        name, operator, argument = match.group(1), match.group(2), match.group(3) or ""
        current = environment.get(name)
        if operator == ":-":
            return current if current else argument
        if operator == "-":
            return current if current is not None else argument
        if operator == ":+":
            return argument if current else ""
        if operator == "+":
            return argument if current is not None else ""
        return current or ""

    return _VARIABLE.sub(replace, value) if isinstance(value, str) else value


class FakeDocker:
    """`docker compose`: config (anchors merged, environment interpolated), ps and up."""

    def __init__(self, root, running=("web", "worker", "smtp-relay", "caddy"), config=0, up=0, web_extra=None):
        self.root = root
        self.running = running
        self.config = config
        self.up = up
        self.web_extra = web_extra or {}
        self.calls = []
        self.models = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        environment = dict(kwargs.get("env") or {})
        files = [args[index + 1] for index, arg in enumerate(args) if arg == "-f"]
        code, out, err = 0, "", ""
        if "config" in args:
            services = {}
            for name in files:
                for service, spec in (yaml.safe_load(Path(name).read_text(encoding="utf-8"))["services"] or {}).items():
                    services.setdefault(service, {}).update(spec or {})
            for service, spec in services.items():
                values = dict(spec.get("environment") or {})
                if service == "web":
                    values.update(self.web_extra)
                spec["environment"] = {key: _interpolate(value, environment) for key, value in values.items()}
            self.models.append({"services": services})
            out = json.dumps({"services": services})
            code, err = self.config, ("services.web: invalid" if self.config else "")
        elif "ps" in args:
            out = "\n".join(self.running)
        elif "up" in args:
            code, err = self.up, ("cannot start web" if self.up else "")
        return subprocess.CompletedProcess(args, code, out, err)


class MigrateTests(unittest.TestCase):
    def setUp(self):
        self.root = _copy(SCHEMA_4, self)
        which = patch("composer.relay_env.shutil.which", return_value="/usr/bin/docker")
        which.start()
        self.addCleanup(which.stop)

    def _migrate(self, docker, environment=None):
        return re_.migrate(str(self.root), ["compose.yml"], environment=environment or {}, command_runner=docker)

    def test_it_validates_writes_archives_and_recreates_web_and_the_relay(self):
        docker = FakeDocker(self.root)
        outcome = self._migrate(docker, {"SMTP_RELAY_PASSWORD": SECRET, "SMTP_RELAY_USER": "me@example.com"})
        self.assertTrue(outcome["applied"])
        self.assertEqual(outcome["files"], ["compose.yml"])
        self.assertEqual(outcome["recreated"], ["web", "smtp-relay"], "a schema 4 stack has no worker; celery is left")
        self.assertEqual((self.root / "compose.yml").read_text(encoding="utf-8"), EXPECTED)
        backup = Path(outcome["backup_root"]) / "relay-env"
        self.assertEqual((backup / "compose.yml").read_text(encoding="utf-8"), COMPOSE)
        self.assertTrue(any(".composer-check-" in name for name in docker.calls[0]), "the candidate is validated")
        up = [call for call in docker.calls if "up" in call][0]
        self.assertEqual(up[-5:], ["-d", "--no-deps", "--force-recreate", "web", "smtp-relay"])
        self.assertFalse(list(self.root.glob(".compose.yml.composer-check-*")), "no staged candidate is left behind")

        self.assertNotIn(SECRET, json.dumps(outcome))
        self.assertNotIn(SECRET, _every_file_text(self.root))

        again = FakeDocker(self.root)
        outcome = self._migrate(again)
        self.assertEqual((outcome["applied"], outcome["files"], again.calls), (False, [], []))

    def test_a_schema_5_stack_recreates_its_worker_too(self):
        from composer import worker_migration

        (self.root / "compose.yml").write_text(worker_migration.migrate_compose(COMPOSE), encoding="utf-8")
        outcome = self._migrate(FakeDocker(self.root), {"SMTP_RELAY_PASSWORD": SECRET})
        self.assertEqual(outcome["recreated"], ["web", "worker", "smtp-relay"])
        worker = _services((self.root / "compose.yml").read_text(encoding="utf-8"))["worker"]["environment"]
        self.assertEqual(worker["SMTP_RELAY_PASSWORD_SET"], "${SMTP_RELAY_PASSWORD:+yes}")
        self.assertNotIn("SMTP_RELAY_PASSWORD", worker)

    def test_web_sees_the_relay_s_values_and_only_whether_a_password_is_set(self):
        docker = FakeDocker(self.root)
        self._migrate(docker, {"SMTP_RELAY_PASSWORD": SECRET, "SMTP_RELAY_HOST": "smtp.office365.com"})
        web = docker.models[0]["services"]["web"]["environment"]
        self.assertEqual(web["SMTP_RELAY_HOST"], "smtp.office365.com")
        self.assertEqual(web["SMTP_RELAY_PASSWORD_SET"], "yes")
        self.assertNotIn("SMTP_RELAY_PASSWORD", web)

    def test_stopped_services_are_not_started(self):
        outcome = self._migrate(FakeDocker(self.root, running=("caddy", "smtp-relay")))
        self.assertEqual(outcome["recreated"], ["smtp-relay"])
        outcome = re_.migrate(str(_copy(SCHEMA_4, self)), ["compose.yml"], environment={},
                              command_runner=FakeDocker(self.root, running=()))
        self.assertEqual((outcome["applied"], outcome["recreated"]), (True, []))

    def test_a_rejected_config_changes_nothing(self):
        with self.assertRaises(re_.StackCleanupError):
            self._migrate(FakeDocker(self.root, config=1))
        self.assertEqual((self.root / "compose.yml").read_text(encoding="utf-8"), COMPOSE)
        self.assertFalse((self.root / ".xclude").exists())

    def test_an_override_that_hands_web_the_password_is_refused(self):
        docker = FakeDocker(self.root, web_extra={"SMTP_RELAY_PASSWORD": "${SMTP_RELAY_PASSWORD:-}"})
        with self.assertRaises(re_.RelayEnvError) as caught:
            self._migrate(docker, {"SMTP_RELAY_PASSWORD": SECRET})
        self.assertIn("would reach web", str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual((self.root / "compose.yml").read_text(encoding="utf-8"), COMPOSE)

    def test_an_override_that_gives_web_other_values_is_refused(self):
        docker = FakeDocker(self.root, web_extra={"SMTP_RELAY_HOST": "smtp.elsewhere.example"})
        with self.assertRaises(re_.RelayEnvError) as caught:
            self._migrate(docker)
        self.assertIn("SMTP_RELAY_HOST", str(caught.exception))
        self.assertEqual((self.root / "compose.yml").read_text(encoding="utf-8"), COMPOSE)

    def test_a_failed_recreate_names_the_backup(self):
        with self.assertRaises(re_.RelayEnvError) as caught:
            self._migrate(FakeDocker(self.root, up=1))
        self.assertIn("restore from", str(caught.exception))
        self.assertIn("cannot start web", str(caught.exception))

    def test_docker_is_required(self):
        with patch("composer.relay_env.shutil.which", return_value=None):
            with self.assertRaises(re_.RelayEnvError):
                self._migrate(FakeDocker(self.root))
        self.assertEqual((self.root / "compose.yml").read_text(encoding="utf-8"), COMPOSE)


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.cwd = os.getcwd()
        self.root = _copy(SCHEMA_4, self)
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.cwd)
        Path(".secrets").mkdir()
        Path(".secrets/.env").write_text(f"SMTP_RELAY_PASSWORD={SECRET}\n", encoding="utf-8")

    def _check(self, text=None):
        if text is not None:
            Path("compose.yml").write_text(text, encoding="utf-8")
        launcher = DockerComposeLauncher()
        launcher.active_compose_files = ["compose.yml"]
        result = launcher._check_relay_env()
        self.assertNotIn(SECRET, json.dumps(result))
        return result

    def test_a_legacy_stack_is_a_fixable_warning(self):
        result = self._check()
        self.assertEqual(result["level"], WARN)
        self.assertIn("passes the SMTP relay's settings to smtp-relay only", result["message"])
        self.assertIn("SMTP_RELAY_TLS_VERIFY and SMTP_RELAY_CA_FILE in the secrets file do not reach the relay",
                      result["message"])
        self.assertIn("composer check --fix", result["fix"])

    def test_missing_certificate_lines_alone_are_a_fixable_warning(self):
        result = self._check(MAIL_ONLY)
        self.assertEqual(result["level"], WARN)
        self.assertIn("does not pass the relay's certificate settings", result["message"])
        self.assertNotIn("Email step", result["message"])

    def test_a_current_stack_is_ok(self):
        result = self._check(EXPECTED)
        self.assertEqual(result["level"], OK)
        self.assertIn("reaches smtp-relay only", result["message"])

    def test_a_custom_stack_is_reported_with_the_lines_left(self):
        result = self._check(COMPOSE.replace('"${SMTP_RELAY_HOST:-smtp.gmail.com}"', '"smtp.office365.com"'))
        self.assertEqual(result["level"], OK)
        self.assertIn("custom, not managed by Composer", result["message"])
        self.assertIn("SMTP_RELAY_HOST", result["message"])

    def test_a_shared_password_warns(self):
        result = self._check(COMPOSE.replace(FROM_LINE, FROM_LINE + '  SMTP_RELAY_PASSWORD: "${SMTP_RELAY_PASSWORD:-}"\n'))
        self.assertEqual(result["level"], WARN)
        self.assertIn("shares the SMTP relay password", result["message"])

    def test_no_relay_is_ok(self):
        result = self._check((OLD_LAYOUT / "compose.yml").read_text(encoding="utf-8"))
        self.assertEqual(result["level"], OK)
        self.assertIn("No smtp-relay", result["message"])


class CheckFixTests(unittest.TestCase):
    SERVICES = ["web", "worker", "smtp-relay", "caddy", "composer-agent", "composer-executor", "docker-socket-proxy"]

    def setUp(self):
        self.cwd = os.getcwd()
        self.root = _copy(SCHEMA_4, self)
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.cwd)

    def _fix(self, *, confirmed=True, applied=True, effect=None):
        launcher = DockerComposeLauncher()
        launcher.services = list(self.SERVICES)
        launcher.active_compose_files = ["compose.yml"]
        outcome = {"applied": applied, "files": ["compose.yml"], "recreated": ["web", "smtp-relay"],
                   "backup_root": "/x"}
        with (
            patch.object(launcher, "build_compose_env", return_value={}),
            patch("composer.checkup.confirm", return_value=confirmed) as confirm,
            patch("composer.wrappers.inspect_wrappers", return_value=[]),
            patch("composer.checkup.inspect_legacy_proxy_routes", return_value={"recognized": [], "unsupported": []}),
            patch("composer.agent_installer.enable_post_start_label", return_value={"files": []}),
            patch("composer.agent_installer.migrate_dlux_updater", return_value={"files": []}),
            patch("composer.agent_installer.enable_executor", return_value={"files": []}),
            patch("composer.agent_installer.normalize_restart_labels", return_value={"files": []}),
            patch("composer.maintenance_migration.inspect", return_value={"legacy": False}),
            patch("composer.worker_migration.pending", return_value={"compose": False, "dev": False, "stamp": False}),
            patch("composer.front_proxy.inspect", return_value={"files": []}),
            patch("composer.relay_env.migrate", return_value=outcome, side_effect=effect) as migrate,
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            fixes = launcher._maybe_fix(SimpleNamespace(file=None, dev=False, fix=True, yes=True, json=False), [])
        shown = json.dumps(confirm.call_args.args if confirm.called else []) + stdout.getvalue()
        return fixes, migrate, shown

    def test_the_fix_runs_and_reports_the_recreate(self):
        fixes, migrate, printed = self._fix()
        migrate.assert_called_once()
        self.assertEqual(migrate.call_args.args[1], ["compose.yml"])
        self.assertEqual(migrate.call_args.kwargs["environment"], {})
        entry = [fix for fix in fixes if fix["name"] == "fix:relay-env"][0]
        self.assertEqual(entry["level"], OK)
        self.assertIn("recreated web, smtp-relay", entry["message"])
        self.assertIn("Share the SMTP relay's host, port, encryption and user", printed)

    def test_the_fix_gets_the_secrets_so_a_recreate_never_runs_on_defaults(self):
        Path(".secrets").mkdir()
        Path(".secrets/.env").write_text(f"SMTP_RELAY_PASSWORD={SECRET}\n", encoding="utf-8")
        fixes, migrate, printed = self._fix()
        self.assertEqual(migrate.call_args.kwargs["environment"].get("SMTP_RELAY_PASSWORD"), SECRET)
        self.assertNotIn(SECRET, json.dumps(fixes) + printed)

    def test_declining_changes_nothing(self):
        _fixes, migrate, _printed = self._fix(confirmed=False)
        migrate.assert_not_called()

    def test_a_current_or_custom_stack_has_nothing_to_fix(self):
        Path("compose.yml").write_text(EXPECTED, encoding="utf-8")
        fixes, migrate, _printed = self._fix()
        migrate.assert_not_called()
        self.assertEqual(fixes, [])
        Path("compose.yml").write_text(
            COMPOSE.replace('"${SMTP_RELAY_HOST:-smtp.gmail.com}"', '"smtp.office365.com"'), encoding="utf-8")
        fixes, migrate, _printed = self._fix()
        migrate.assert_not_called()
        self.assertEqual(fixes, [])

    def test_a_refused_rewrite_fails_the_fix(self):
        fixes, _migrate, _printed = self._fix(effect=re_.RelayEnvError("SMTP_RELAY_PASSWORD would reach web"))
        entry = [fix for fix in fixes if fix["name"] == "fix:relay-env"][0]
        self.assertEqual(entry["level"], "fail")
        self.assertIn("would reach web", entry["message"])


class DjangoLuxScaffoldTests(unittest.TestCase):
    def test_the_scaffold_carries_the_same_lines(self):
        template = DLUX_PROJECT / "compose.yml.tmpl"
        if not template.is_file() or re_.PASSWORD_SET not in template.read_text(encoding="utf-8"):
            self.skipTest("No DjangoLux checkout alongside that shares the relay settings in its scaffold")
        text = template.read_text(encoding="utf-8")
        self.assertEqual(re_.classify(text)["state"], re_.CURRENT)
        self.assertIn(FROM_LINE + MAIL_LINES, text)
        if re_.TLS_KEYS[0] in text:
            for line in TLS_LINES.splitlines(keepends=True):
                if not line.lstrip().startswith("#"):
                    self.assertIn(line, text)


if __name__ == "__main__":
    unittest.main()
