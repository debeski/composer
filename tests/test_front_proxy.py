"""Caddy's trusted front proxies come from DLUX_FRONT_PROXY in the secrets file.

The generated Caddyfile trusts X-Forwarded-For from every private range. These
tests pin how `check` reports each state, how `check --fix` rewrites the
Caddyfile line and the caddy service's environment (and only those), and that
an invalid value is refused before Caddy is handed it.
"""

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from composer import front_proxy as fp
from composer.checkup import FAIL, OK, WARN
from composer.launcher import DockerComposeLauncher

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCHEMA_4 = FIXTURES / "schema-4"
OLD_LAYOUT = FIXTURES / "old-layout"
DLUX_PROJECT = (
    Path(__file__).resolve().parents[3] / "pkg-django-lux" / "main" / "dlux" / "scaffold" / "templates" / "project"
)

LEGACY_LINE = "\t\ttrusted_proxies static private_ranges\n"
MANAGED_LINE = "\t\ttrusted_proxies static {$DLUX_FRONT_PROXY:private_ranges}\n"
COMPOSE_ENTRY = 'DLUX_FRONT_PROXY: "${DLUX_FRONT_PROXY-private_ranges}"'

CADDYFILE = (
    "{\n"
    "\tauto_https off\n"
    "\t# trusted_proxies static private_ranges  (a comment is not a directive)\n"
    "\tservers {\n"
    + LEGACY_LINE
    + "\t\ttrusted_proxies_strict\n"
    "\t}\n"
    "}\n"
    "\n"
    "{$CADDY_SITE_ADDRESS::80} {\n"
    "\treverse_proxy web:8000\n"
    "}\n"
)

COMPOSE = """services:
  web:
    image: example/web:latest
    environment:
      DLUX_FRONT_PROXY_NOTE: "not caddy's"

  caddy:
    image: caddy:latest
    restart: always
    ports:
      - target: 80
        published: "${CADDY_PORT:-${NGINX_PORT:-80}}"
        protocol: tcp
    environment:
      CADDY_SITE_ADDRESS: "${CADDY_SITE_ADDRESS:-:80}"
      # Mirrors nginx client_max_body_size.
      CADDY_MAX_SIZE: "${CADDY_MAX_SIZE:-10MB}"
    volumes:
      - ./.proxy/Caddyfile:/etc/caddy/Caddyfile:ro

volumes:
  caddy_data:
"""


def _caddy(text):
    return yaml.safe_load(text)["services"]["caddy"]


def _copy(source: Path, case: unittest.TestCase) -> Path:
    holder = tempfile.TemporaryDirectory()
    case.addCleanup(holder.cleanup)
    root = Path(holder.name)
    shutil.copytree(source, root, dirs_exist_ok=True)
    return root


class CaddyfileTests(unittest.TestCase):
    def test_states(self):
        self.assertEqual(fp.classify_caddyfile(CADDYFILE), fp.LEGACY)
        self.assertEqual(fp.classify_caddyfile(CADDYFILE.replace(LEGACY_LINE, MANAGED_LINE)), fp.MANAGED)
        self.assertEqual(
            fp.classify_caddyfile(CADDYFILE.replace(LEGACY_LINE, "\t\ttrusted_proxies static 10.0.0.5\n")), fp.CUSTOM
        )
        self.assertEqual(fp.classify_caddyfile(CADDYFILE.replace(LEGACY_LINE, "")), fp.NONE)

    def test_a_legacy_line_beside_a_custom_one_is_custom(self):
        text = CADDYFILE.replace("\treverse_proxy web:8000\n", "\treverse_proxy web:8000 {\n\t\ttrusted_proxies 10.0.0.1\n\t}\n")
        self.assertEqual(fp.classify_caddyfile(text), fp.CUSTOM)
        with self.assertRaises(fp.FrontProxyError):
            fp.migrate_caddyfile(text)

    def test_the_rewrite_changes_only_the_directive_and_keeps_its_indentation(self):
        text = fp.migrate_caddyfile(CADDYFILE)
        self.assertEqual(text, CADDYFILE.replace(LEGACY_LINE, MANAGED_LINE))
        self.assertIn("\t# trusted_proxies static private_ranges  (a comment", text)
        self.assertIn("\t\ttrusted_proxies_strict\n", text)
        self.assertEqual(fp.migrate_caddyfile(text), text)

    def test_space_indentation_and_a_trailing_comment_are_kept(self):
        text = fp.migrate_caddyfile("{\n    servers {\n        trusted_proxies static private_ranges  # LAN\n    }\n}\n")
        self.assertIn("        trusted_proxies static {$DLUX_FRONT_PROXY:private_ranges}  # LAN\n", text)


class ComposeTests(unittest.TestCase):
    def test_a_block_mapping_gains_the_entry_and_nothing_else_changes(self):
        text = fp.add_compose_variable(COMPOSE)
        self.assertEqual(
            text,
            COMPOSE.replace(
                '      CADDY_MAX_SIZE: "${CADDY_MAX_SIZE:-10MB}"\n',
                '      CADDY_MAX_SIZE: "${CADDY_MAX_SIZE:-10MB}"\n      ' + COMPOSE_ENTRY + "\n",
            ),
        )
        self.assertEqual(_caddy(text)["environment"]["DLUX_FRONT_PROXY"], fp.COMPOSE_VALUE)
        self.assertEqual(fp.compose_value(text), fp.COMPOSE_VALUE)
        self.assertEqual(fp.add_compose_variable(text), text)

    def test_another_service_s_similar_key_does_not_count(self):
        self.assertIsNone(fp.compose_value(COMPOSE))

    def test_a_list_keeps_its_style(self):
        listed = COMPOSE.replace(
            '      CADDY_SITE_ADDRESS: "${CADDY_SITE_ADDRESS:-:80}"\n',
            '      - "CADDY_SITE_ADDRESS=${CADDY_SITE_ADDRESS:-:80}"\n',
        ).replace('      CADDY_MAX_SIZE: "${CADDY_MAX_SIZE:-10MB}"\n', '      - "CADDY_MAX_SIZE=${CADDY_MAX_SIZE:-10MB}"\n')
        text = fp.add_compose_variable(listed)
        self.assertIn('      - "DLUX_FRONT_PROXY=${DLUX_FRONT_PROXY-private_ranges}"\n', text)
        self.assertIn("DLUX_FRONT_PROXY=${DLUX_FRONT_PROXY-private_ranges}", _caddy(text)["environment"])
        self.assertEqual(fp.compose_value(text), fp.COMPOSE_VALUE)

        bare = listed.replace('- "CADDY_SITE_ADDRESS=${CADDY_SITE_ADDRESS:-:80}"', "- CADDY_SITE_ADDRESS=:80")
        self.assertIn("      - DLUX_FRONT_PROXY=${DLUX_FRONT_PROXY-private_ranges}\n", fp.add_compose_variable(bare))

    def test_a_service_without_environment_gets_one_after_ports(self):
        start = COMPOSE.index("    environment:\n      CADDY_SITE")
        end = COMPOSE.index("    volumes:\n      - ./.proxy")
        text = fp.add_compose_variable(COMPOSE[:start] + COMPOSE[end:])
        self.assertIn('        protocol: tcp\n    environment:\n      ' + COMPOSE_ENTRY + "\n    volumes:\n", text)
        self.assertEqual(_caddy(text)["environment"], {"DLUX_FRONT_PROXY": fp.COMPOSE_VALUE})

    def test_an_alias_or_flow_environment_is_left_for_a_human(self):
        for value in ("*caddy-env", "{CADDY_MAX_SIZE: 10MB}"):
            start = COMPOSE.index("    environment:\n      CADDY_SITE")
            end = COMPOSE.index("    volumes:\n      - ./.proxy")
            text = COMPOSE[:start] + f"    environment: {value}\n" + COMPOSE[end:]
            with self.assertRaises(fp.FrontProxyError):
                fp.add_compose_variable(text)

    def test_an_own_value_is_reported_as_custom(self):
        text = COMPOSE.replace(
            '      CADDY_MAX_SIZE: "${CADDY_MAX_SIZE:-10MB}"\n',
            '      CADDY_MAX_SIZE: "${CADDY_MAX_SIZE:-10MB}"\n      DLUX_FRONT_PROXY: "${DLUX_FRONT_PROXY:-private_ranges}"\n',
        )
        self.assertEqual(fp.compose_value(text), "${DLUX_FRONT_PROXY:-private_ranges}")
        self.assertEqual(fp.add_compose_variable(text), text)


class ValueTests(unittest.TestCase):
    def test_addresses_ranges_and_private_ranges_are_valid(self):
        entries, invalid = fp.parse_ranges("10.0.0.5  192.168.10.0/24 private_ranges 2001:db8::/32 10.0.0.9/24")
        self.assertEqual(len(entries), 5)
        self.assertEqual(invalid, [])

    def test_anything_caddy_cannot_parse_is_invalid(self):
        _entries, invalid = fp.parse_ranges(
            "none 10.0.0.5,10.0.0.6 300.1.1.1 10.0.0.0/255.255.255.0 fe80::1%eth0 10.0.0.0/33 #"
        )
        self.assertEqual(
            invalid, ["none", "10.0.0.5,10.0.0.6", "300.1.1.1", "10.0.0.0/255.255.255.0", "fe80::1%eth0", "10.0.0.0/33", "#"]
        )

    def _root(self, env=None):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        root = Path(holder.name)
        if env is not None:
            (root / ".secrets").mkdir()
            (root / ".secrets" / ".env").write_text(env, encoding="utf-8")
        return root

    def test_absent_empty_quoted_and_environment_values(self):
        self.assertEqual(fp.read_setting(self._root("SECRET_KEY=x\n"), {})["set"], False)
        empty = fp.read_setting(self._root("DLUX_FRONT_PROXY=\n"), {})
        self.assertEqual((empty["set"], empty["value"], empty["source"]), (True, "", ".secrets/.env"))
        quoted = fp.read_setting(self._root('DLUX_FRONT_PROXY="10.0.0.5 192.168.10.0/24"\n'), {})
        self.assertEqual(quoted["value"], "10.0.0.5 192.168.10.0/24")
        inherited = fp.read_setting(self._root(), {"DLUX_FRONT_PROXY": "10.0.0.5"})
        self.assertEqual((inherited["value"], inherited["source"], inherited["secrets"]),
                         ("10.0.0.5", "the environment", ".secrets/.env"))
        self.assertEqual(fp.read_setting(self._root("DLUX_FRONT_PROXY=10.0.0.7\n"), {"DLUX_FRONT_PROXY": "x"})["value"],
                         "10.0.0.7")

    def test_the_published_port_follows_the_generated_compose(self):
        self.assertEqual(fp.published_port(self._root(), {}), "80")
        self.assertEqual(fp.published_port(self._root("NGINX_PORT=8081\n"), {}), "8081")
        self.assertEqual(fp.published_port(self._root("NGINX_PORT=8081\nCADDY_PORT=8443\n"), {}), "8443")
        self.assertEqual(fp.published_port(self._root("CADDY_PORT=\n"), {"NGINX_PORT": "81"}), "81")


class PlanTests(unittest.TestCase):
    def test_a_schema_4_stack_is_rewritten_once(self):
        root = _copy(SCHEMA_4, self)
        state = fp.inspect(root, ["compose.yml"], {})
        self.assertEqual((state["caddyfile"], state["compose"]), (fp.LEGACY, fp.ABSENT))
        self.assertEqual(state["files"], [".proxy/Caddyfile", "compose.yml"])
        _sources, updates, notes = fp._plan(root, ["compose.yml"])
        self.assertEqual(notes, [])
        for path, text in updates.items():
            path.write_text(text, encoding="utf-8")
        state = fp.inspect(root, ["compose.yml"], {})
        self.assertEqual((state["caddyfile"], state["compose"], state["files"]), (fp.MANAGED, fp.PASSED, []))

    def test_an_old_layout_caddy_without_environment_gets_one(self):
        root = _copy(OLD_LAYOUT, self)
        _sources, updates, _notes = fp._plan(root, ["compose.yml"])
        compose = updates[(root / "compose.yml").resolve()]
        self.assertEqual(_caddy(compose)["environment"], {"DLUX_FRONT_PROXY": fp.COMPOSE_VALUE})

    def test_the_development_override_does_not_change_which_file_is_edited(self):
        root = _copy(SCHEMA_4, self)
        state = fp.inspect(root, ["compose.yml", "compose.dev.yml"], {})
        self.assertEqual(state["compose_file"], "compose.yml")
        self.assertEqual(state["files"], [".proxy/Caddyfile", "compose.yml"])

    def test_custom_and_missing_caddyfiles_are_left_alone(self):
        root = _copy(SCHEMA_4, self)
        caddyfile = root / ".proxy" / "Caddyfile"
        caddyfile.write_text(caddyfile.read_text(encoding="utf-8").replace(
            "trusted_proxies static private_ranges", "trusted_proxies static 10.0.0.1"), encoding="utf-8")
        self.assertEqual(fp.inspect(root, ["compose.yml"], {})["files"], [])
        caddyfile.rename(root / ".proxy" / "Caddyfile.moved")
        state = fp.inspect(root, ["compose.yml"], {})
        self.assertEqual((state["caddyfile"], state["files"]), (fp.MISSING, []))

    def test_an_uneditable_environment_still_gets_the_caddyfile_and_a_note(self):
        root = _copy(SCHEMA_4, self)
        compose = root / "compose.yml"
        text = compose.read_text(encoding="utf-8")
        start = text.index("    environment:\n      CADDY_SITE")
        end = text.index("    volumes:\n      - ./.proxy/Caddyfile")
        compose.write_text(text[:start] + "    environment: *caddy-env\n" + text[end:], encoding="utf-8")
        state = fp.inspect(root, ["compose.yml"], {})
        self.assertEqual(state["files"], [".proxy/Caddyfile"])
        self.assertIn("not a block mapping or list", state["unsupported"])

    def test_preview_writes_nothing_and_refuses_an_invalid_value(self):
        root = _copy(SCHEMA_4, self)
        before = (root / "compose.yml").read_text(encoding="utf-8")
        preview = fp.preview(str(root), ["compose.yml"], {})
        self.assertEqual(preview["files"], [".proxy/Caddyfile", "compose.yml"])
        self.assertIn("+      " + COMPOSE_ENTRY, preview["diff"])
        self.assertEqual((root / "compose.yml").read_text(encoding="utf-8"), before)
        (root / ".secrets").mkdir()
        (root / ".secrets" / ".env").write_text("SECRET_KEY=s3cr3t-value\nDLUX_FRONT_PROXY=10.0.0.5,10.0.0.6\n",
                                               encoding="utf-8")
        blocked = fp.preview(str(root), ["compose.yml"], {})
        self.assertEqual(blocked["files"], [])
        self.assertIn("10.0.0.5,10.0.0.6", blocked["note"])
        self.assertNotIn("s3cr3t-value", json.dumps(blocked))


class FakeDocker:
    """`docker compose`: config, run (caddy validate), ps and up."""

    def __init__(self, root, running=("caddy", "web"), passes=None, validate=0):
        self.root = root
        self.running = running
        self.passes = passes
        self.validate = validate
        self.calls = []
        self.validated = []

    def __call__(self, args, **_kwargs):
        self.calls.append(list(args))
        files = [args[index + 1] for index, arg in enumerate(args) if arg == "-f"]
        code, out = 0, ""
        if "config" in args:
            services = {}
            for name in files:
                for service, spec in (yaml.safe_load(Path(name).read_text(encoding="utf-8"))["services"] or {}).items():
                    services.setdefault(service, {}).update(spec or {})
            environment = services.get("caddy", {}).get("environment") or {}
            if self.passes is False:
                environment = {}
            services["caddy"]["environment"] = environment
            services["caddy"]["volumes"] = [
                {"type": "bind", "source": str(self.root / ".proxy" / "Caddyfile"), "target": "/etc/caddy/Caddyfile"}
            ]
            out = json.dumps({"services": services})
        elif "run" in args:
            candidate = next(arg for arg in args if arg.endswith(":/etc/caddy/Caddyfile:ro")).split(":")[0]
            self.validated.append((Path(candidate).read_text(encoding="utf-8"), list(files)))
            code = self.validate
        elif "ps" in args:
            out = "\n".join(self.running)
        return subprocess.CompletedProcess(args, code, out, "caddy: invalid range" if code else "")


class MigrateTests(unittest.TestCase):
    def setUp(self):
        self.root = _copy(SCHEMA_4, self).resolve()
        which = patch("composer.front_proxy.shutil.which", return_value="/usr/bin/docker")
        which.start()
        self.addCleanup(which.stop)

    def _migrate(self, docker):
        return fp.migrate(str(self.root), ["compose.yml"], environment={}, command_runner=docker)

    def test_it_validates_writes_archives_and_recreates_caddy(self):
        docker = FakeDocker(self.root)
        outcome = self._migrate(docker)
        self.assertTrue(outcome["applied"])
        self.assertEqual(outcome["files"], [".proxy/Caddyfile", "compose.yml"])
        self.assertEqual(outcome["recreated"], ["caddy"])
        self.assertIn(MANAGED_LINE.strip(), (self.root / ".proxy" / "Caddyfile").read_text(encoding="utf-8"))
        self.assertIn(COMPOSE_ENTRY, (self.root / "compose.yml").read_text(encoding="utf-8"))
        backup = Path(outcome["backup_root"]) / "front-proxy"
        self.assertIn(LEGACY_LINE.strip(), (backup / ".proxy" / "Caddyfile").read_text(encoding="utf-8"))
        self.assertNotIn("DLUX_FRONT_PROXY", (backup / "compose.yml").read_text(encoding="utf-8"))

        validated_text, validated_files = docker.validated[0]
        self.assertIn(MANAGED_LINE.strip(), validated_text)
        self.assertTrue(any(".composer-check-" in name for name in validated_files),
                        "caddy is validated with the new environment")
        up = [call for call in docker.calls if "up" in call]
        self.assertEqual(up[0][-4:], ["-d", "--no-deps", "--force-recreate", "caddy"])
        self.assertEqual(sorted(p.name for p in (self.root / ".proxy").iterdir()), ["Caddyfile"],
                         "no staged candidate is left behind")
        self.assertFalse(list(self.root.glob(".compose.yml.composer-check-*")))

        again = self._migrate(FakeDocker(self.root))
        self.assertEqual((again["applied"], again["files"]), (False, []))

    def test_a_stopped_caddy_is_not_started(self):
        outcome = self._migrate(FakeDocker(self.root, running=("web",)))
        self.assertEqual(outcome["recreated"], [])
        self.assertTrue(outcome["applied"])

    def test_a_rejected_config_changes_nothing(self):
        before = (self.root / ".proxy" / "Caddyfile").read_text(encoding="utf-8")
        with self.assertRaises(fp.StackCleanupError):
            self._migrate(FakeDocker(self.root, validate=1))
        self.assertEqual((self.root / ".proxy" / "Caddyfile").read_text(encoding="utf-8"), before)
        self.assertFalse((self.root / ".xclude").exists())

    def test_an_override_that_drops_the_variable_is_refused(self):
        with self.assertRaises(fp.FrontProxyError):
            self._migrate(FakeDocker(self.root, passes=False))
        self.assertNotIn("DLUX_FRONT_PROXY", (self.root / "compose.yml").read_text(encoding="utf-8"))

    def test_an_invalid_value_is_refused_before_docker_is_asked(self):
        (self.root / ".secrets").mkdir()
        (self.root / ".secrets" / ".env").write_text("DLUX_FRONT_PROXY=none\n", encoding="utf-8")
        docker = FakeDocker(self.root)
        with self.assertRaises(fp.FrontProxyError) as caught:
            self._migrate(docker)
        self.assertIn("none", str(caught.exception))
        self.assertEqual(docker.calls, [])


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.cwd = os.getcwd()
        self.root = _copy(SCHEMA_4, self)
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.cwd)

    def _check(self, env=None, environ=None):
        if env is not None:
            Path(".secrets").mkdir(exist_ok=True)
            Path(".secrets/.env").write_text("SECRET_KEY=x\n" + env, encoding="utf-8")
        launcher = DockerComposeLauncher()
        launcher.active_compose_files = ["compose.yml"]
        with patch.object(launcher, "build_compose_env", return_value=dict(environ or {})):
            return launcher._check_front_proxy()

    def _managed(self):
        for path, text in fp._plan(self.root.resolve(), ["compose.yml"])[1].items():
            path.write_text(text, encoding="utf-8")

    def test_a_legacy_stack_is_a_fixable_warning(self):
        result = self._check()
        self.assertEqual(result["level"], WARN)
        self.assertIn("every private network", result["message"])
        self.assertIn(".proxy/Caddyfile does not read DLUX_FRONT_PROXY and compose.yml does not pass it", result["message"])
        self.assertIn("composer check --fix", result["fix"])

    def test_a_value_the_stack_cannot_read_yet_warns(self):
        result = self._check("DLUX_FRONT_PROXY=10.0.0.5\n")
        self.assertEqual(result["level"], WARN)
        self.assertTrue(result["message"].startswith("DLUX_FRONT_PROXY is set in .secrets/.env, but"))

    def test_compose_alone_missing_the_variable_warns(self):
        self._managed()
        Path("compose.yml").write_text(
            Path("compose.yml").read_text(encoding="utf-8").replace("      " + COMPOSE_ENTRY + "\n", ""),
            encoding="utf-8",
        )
        result = self._check("DLUX_FRONT_PROXY=\n")
        self.assertEqual(result["level"], WARN)
        self.assertIn("compose.yml does not pass it to caddy", result["message"])
        self.assertNotIn(".proxy/Caddyfile does not read", result["message"])

    def test_managed_and_unset_warns_with_the_ways_out(self):
        self._managed()
        result = self._check("CADDY_PORT=8443\n")
        self.assertEqual(result["level"], WARN)
        self.assertIn("can choose the address DjangoLux records", result["message"])
        self.assertIn("Set DLUX_FRONT_PROXY in .secrets/.env", result["fix"])
        self.assertIn("set it empty (DLUX_FRONT_PROXY=)", result["fix"])
        self.assertIn("published port 8443", result["fix"])
        self.assertIn("'./start.sh', which recreates caddy", result["fix"])

    def test_explicit_values_are_ok(self):
        self._managed()
        cases = {
            "DLUX_FRONT_PROXY=private_ranges\n": "a deliberate choice",
            "DLUX_FRONT_PROXY=\n": "Caddy trusts no proxy",
            'DLUX_FRONT_PROXY="10.0.0.5 192.168.10.0/24"\n': "only from 10.0.0.5, 192.168.10.0/24",
            "DLUX_FRONT_PROXY=private_ranges 203.0.113.7\n": "only from every private network, 203.0.113.7",
        }
        for env, expected in cases.items():
            result = self._check(env)
            self.assertEqual(result["level"], OK, (env, result))
            self.assertIn(expected, result["message"])

    def test_an_inherited_value_is_read_when_the_file_has_none(self):
        self._managed()
        result = self._check("", environ={"DLUX_FRONT_PROXY": "10.0.0.5"})
        self.assertEqual(result["level"], OK)
        self.assertIn("(DLUX_FRONT_PROXY in the environment)", result["message"])

    def test_invalid_entries_fail_and_commas_get_a_hint(self):
        self._managed()
        result = self._check("DLUX_FRONT_PROXY=10.0.0.5,10.0.0.6 none\n")
        self.assertEqual(result["level"], FAIL)
        self.assertIn("10.0.0.5,10.0.0.6, none", result["message"])
        self.assertIn("Caddy does not start with them", result["message"])
        self.assertIn("separated by spaces, not commas", result["fix"])
        legacy = _copy(SCHEMA_4, self)
        os.chdir(legacy)
        self.assertIn("once it reads the variable", self._check("DLUX_FRONT_PROXY=none\n")["message"])

    def test_custom_none_missing_and_no_caddy_are_ok(self):
        caddyfile = Path(".proxy/Caddyfile")
        original = caddyfile.read_text(encoding="utf-8")
        caddyfile.write_text(original.replace("static private_ranges", "static 10.0.0.1"), encoding="utf-8")
        self.assertIn("custom, not managed by Composer", self._check()["message"])
        caddyfile.write_text(original.replace("\t\ttrusted_proxies static private_ranges\n", ""), encoding="utf-8")
        result = self._check()
        self.assertEqual(result["level"], OK)
        self.assertIn("sets no trusted_proxies", result["message"])
        caddyfile.rename(".proxy/Caddyfile.moved")
        self.assertEqual(self._check()["level"], OK)
        Path("compose.yml").write_text("services:\n  web:\n    image: x\n", encoding="utf-8")
        self.assertIn("No caddy service", self._check()["message"])

    def test_compose_with_its_own_value_is_custom(self):
        self._managed()
        Path("compose.yml").write_text(
            Path("compose.yml").read_text(encoding="utf-8").replace(fp.COMPOSE_VALUE, "10.9.9.9"), encoding="utf-8"
        )
        result = self._check()
        self.assertEqual(result["level"], OK)
        self.assertIn("its own DLUX_FRONT_PROXY value", result["message"])

    def test_an_uneditable_environment_names_the_manual_step(self):
        self._managed()
        text = Path("compose.yml").read_text(encoding="utf-8")
        start = text.index("    environment:\n      CADDY_SITE")
        end = text.index("    volumes:\n      - ./.proxy/Caddyfile")
        Path("compose.yml").write_text(text[:start] + "    environment: *caddy-env\n" + text[end:], encoding="utf-8")
        result = self._check()
        self.assertEqual(result["level"], WARN)
        self.assertNotIn("check --fix", result["fix"])
        self.assertIn("by hand", result["fix"])


class CheckFixTests(unittest.TestCase):
    SERVICES = ["web", "caddy", "composer-agent", "composer-executor", "docker-socket-proxy"]

    def setUp(self):
        self.cwd = os.getcwd()
        self.root = _copy(SCHEMA_4, self)
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.cwd)

    def _fix(self, *, confirmed=True):
        launcher = DockerComposeLauncher()
        launcher.services = list(self.SERVICES)
        launcher.active_compose_files = ["compose.yml"]
        outcome = {"files": [".proxy/Caddyfile", "compose.yml"], "recreated": ["caddy"], "backup_root": "/x",
                   "notes": []}
        with (
            patch.object(launcher, "build_compose_env", return_value={}),
            patch("composer.checkup.confirm", return_value=confirmed),
            patch("composer.wrappers.inspect_wrappers", return_value=[]),
            patch("composer.checkup.inspect_legacy_proxy_routes", return_value={"recognized": [], "unsupported": []}),
            patch("composer.agent_installer.enable_post_start_label", return_value={"files": []}),
            patch("composer.agent_installer.migrate_dlux_updater", return_value={"files": []}),
            patch("composer.agent_installer.enable_executor", return_value={"files": []}),
            patch("composer.agent_installer.normalize_restart_labels", return_value={"files": []}),
            patch("composer.maintenance_migration.inspect", return_value={"legacy": False}),
            patch("composer.worker_migration.pending", return_value={"compose": False, "dev": False, "stamp": False}),
            patch("composer.front_proxy.migrate", return_value=outcome) as migrate,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            fixes = launcher._maybe_fix(SimpleNamespace(file=None, dev=False, fix=True, yes=True, json=False), [])
        return fixes, migrate

    def test_the_fix_runs_and_reports_the_recreate(self):
        fixes, migrate = self._fix()
        migrate.assert_called_once()
        self.assertEqual(migrate.call_args.kwargs["environment"], {})
        entry = [fix for fix in fixes if fix["name"] == "fix:front-proxy"][0]
        self.assertEqual(entry["level"], OK)
        self.assertIn("recreated caddy", entry["message"])

    def test_an_invalid_value_blocks_the_fix(self):
        Path(".secrets").mkdir()
        Path(".secrets/.env").write_text("DLUX_FRONT_PROXY=none\n", encoding="utf-8")
        fixes, migrate = self._fix()
        migrate.assert_not_called()
        blocked = [fix for fix in fixes if fix["name"] == "front-proxy"][0]
        self.assertEqual(blocked["level"], WARN)
        self.assertIn("none", blocked["message"])

    def test_declining_changes_nothing(self):
        _fixes, migrate = self._fix(confirmed=False)
        migrate.assert_not_called()

    def test_a_managed_stack_has_nothing_to_fix(self):
        for path, text in fp._plan(self.root.resolve(), ["compose.yml"])[1].items():
            path.write_text(text, encoding="utf-8")
        fixes, migrate = self._fix()
        migrate.assert_not_called()
        self.assertEqual(fixes, [])


class DjangoLuxScaffoldTests(unittest.TestCase):
    def test_the_scaffold_carries_the_same_lines(self):
        caddyfile = DLUX_PROJECT / ".proxy" / "Caddyfile.tmpl"
        if not caddyfile.is_file() or fp.CADDY_RANGES not in caddyfile.read_text(encoding="utf-8"):
            self.skipTest("No DjangoLux checkout alongside with DLUX_FRONT_PROXY in its scaffold")
        self.assertEqual(fp.classify_caddyfile(caddyfile.read_text(encoding="utf-8")), fp.MANAGED)
        self.assertIn(COMPOSE_ENTRY, (DLUX_PROJECT / "compose.yml.tmpl").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
