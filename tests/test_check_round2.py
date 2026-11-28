"""`check` and `check --fix` findings of break-it round 2 (R2-0xx), one class each.

Each test fails on the code before its fix: the finding's repro, cut to the part
`check` reads or writes.
"""

import json
import os
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from composer import app_permissions, guidance, stack_schema
from composer import worker_migration as wm
from composer.checkup import FAIL, OK, WARN
from composer.launcher import DockerComposeLauncher

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCHEMA_4 = FIXTURES / "schema-4"


@contextmanager
def _in(directory):
    previous = os.getcwd()
    os.chdir(directory)
    try:
        yield
    finally:
        os.chdir(previous)


class _Project(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name).resolve()
        self.launcher = DockerComposeLauncher()
        self.launcher.active_compose_files = ["compose.yml"]

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
        return path

    def schema_5_project(self, env="5"):
        shutil.copytree(SCHEMA_4, self.root, dirs_exist_ok=True)
        compose = wm.migrate_compose((self.root / "compose.yml").read_text(encoding="utf-8"))
        compose = compose.replace('DLUX_STACK_SCHEMA: "4"', f'DLUX_STACK_SCHEMA: "{env}"')
        (self.root / "compose.yml").write_text(compose, encoding="utf-8")
        for name in ("Dockerfile", "entrypoint.sh", "compose.dev.yml", ".proxy/Caddyfile"):
            path = self.root / name
            path.write_text(stack_schema.restamp(path.read_text(encoding="utf-8"), 5), encoding="utf-8")
        return self.root / "compose.yml"


class ComposeConfigErrorTests(_Project):
    """R2-066: the FAIL said only "docker compose config failed."."""

    def test_composes_own_words_are_quoted(self):
        error = 'validating compose.yml: line 72: mapping key "restart" already defined at line 70\n'
        with patch.object(self.launcher, "discover_services", return_value=False), \
                patch.object(self.launcher, "run_docker_compose", return_value=(False, "", error)):
            result = self.launcher._check_compose_parses()
        self.assertEqual(result["level"], FAIL)
        self.assertIn('line 72: mapping key "restart" already defined at line 70', result["message"])


class SecretsModeTests(_Project):
    """R2-109 (Composer half): a secrets file others can read was reported as fine."""

    def _check(self, mode):
        path = self.write(".secrets/.env", "DJANGO_SECRET_KEY=x\n")
        path.chmod(mode)
        self.launcher.services = ["web"]
        with _in(self.root), patch.object(self.launcher, "plaintext_env_candidates", return_value=[Path(".secrets/.env")]):
            return self.launcher._check_secrets()

    def test_a_group_readable_file_warns_with_the_chmod(self):
        result = self._check(0o644)
        self.assertEqual(result["level"], WARN)
        self.assertIn("mode 0644", result["message"])
        self.assertIn("chmod 600 .secrets/.env", result["fix"])
        self.assertEqual(dict(guidance.summary([result], [], [])["still"])["secrets"], "chmod 600 .secrets/.env")

    def test_0600_passes(self):
        self.assertEqual(self._check(0o600)["level"], OK)


class ShadowedSecretsTests(_Project):
    """R2-029: a one-key project-root .env replaced .secrets/.env, and `check` said it was fine."""

    def test_the_shadowing_file_is_named(self):
        self.write(".env", "TIME_ZONE=UTC\n")
        secrets = self.write(".secrets/.env", "DJANGO_SECRET_KEY=x\nPOSTGRES_PASSWORD=y\n")
        secrets.chmod(0o600)
        self.launcher.services = ["web"]
        with _in(self.root):
            result = self.launcher._check_secrets()
        self.assertEqual(result["level"], WARN)
        self.assertIn(".env shadows .secrets/.env", result["message"])
        self.assertNotIn("UTC", json.dumps(result))


class ProjectNameTests(_Project):
    """R2-083: a compose.dev.yml with another `name:` started a second stack under -d."""

    def test_differing_names_fail_and_agreeing_ones_say_nothing(self):
        self.write("compose.yml", "name: shop\nservices: {}\n")
        self.write("compose.dev.yml", "name: bk2nr\nservices: {}\n")
        self.launcher.active_compose_files = ["compose.yml", "compose.dev.yml"]
        with _in(self.root):
            result = self.launcher._check_project_name()
            self.assertEqual(result["level"], FAIL)
            self.assertIn("compose.yml names it shop; compose.dev.yml names it bk2nr", result["message"])
            self.write("compose.dev.yml", "services: {}\n")
            self.assertIsNone(self.launcher._check_project_name())


class RunningPairTests(unittest.TestCase):
    """R2-023: `check` called the block's image the one "the resident pair runs"."""

    def test_the_block_and_the_running_images_are_told_apart(self):
        launcher = DockerComposeLauncher()
        with patch.object(launcher, "_resident_running_images",
                          return_value={"composer-agent": "debeski/composer@sha256:b2", "composer-executor": "debeski/composer@sha256:b2"}), \
                patch.object(launcher, "_resident_agent_version", return_value="1.11.0b2"):
            note = launcher._running_pair_note()
        self.assertIn("composer-agent runs debeski/composer@sha256:b2", note)
        self.assertIn("(Composer 1.11.0b2)", note)


class LastErrorAddressTests(unittest.TestCase):
    """R2-086 (e): the services finding quoted a container's internal address."""

    def test_addresses_are_replaced(self):
        launcher = DockerComposeLauncher()
        log = "web-1  | Error: connection to db (172.29.0.3) 5432 refused; upstream 10.0.0.5:8000\n"
        with patch.object(launcher, "run_docker_compose", return_value=(True, log, "")):
            line = launcher._last_error_line("web")
        self.assertNotIn("172.29.0.3", line)
        self.assertNotIn("10.0.0.5", line)
        self.assertIn("db (<address>) 5432", line)
        self.assertIn("upstream <address>", line)


class BannerUrlTests(_Project):
    """R2-126 (4): the start banner printed ${BASE_URL:-http://localhost} as written."""

    def _url(self, compose, secrets=None, env=None):
        self.write("compose.yml", compose)
        if secrets is not None:
            self.write(".secrets/.env", secrets)
        self.launcher.app_url = "http://localhost"
        with _in(self.root), patch.dict(os.environ, env or {}, clear=False):
            for key in ("BASE_URL", "SITE_HOST"):
                if key not in (env or {}):
                    os.environ.pop(key, None)
            self.launcher.extract_config()
        return self.launcher.app_url

    def test_the_value_compose_would_use(self):
        compose = 'x-environment: &de\n  BASE_URL: "${BASE_URL:-http://localhost}"\n'
        self.assertEqual(self._url(compose), "http://localhost")
        self.assertEqual(self._url(compose, secrets="BASE_URL=https://shop.example\n"), "https://shop.example")
        self.assertEqual(self._url(compose, secrets="BASE_URL=https://shop.example\n",
                                   env={"BASE_URL": "https://env.example"}), "https://env.example")

    def test_a_default_inside_a_longer_value_and_an_unset_reference(self):
        self.assertEqual(self._url('  BASE_URL: "https://${SITE_HOST:-a.example}/app"\n'), "https://a.example/app")
        self.assertNotIn("${", self._url('  BASE_URL: "${BASE_URL}"\n'))


class StrictStampTests(_Project):
    """R2-068: "99" and "0" only warned, "5x" read as 5, "-1" as no stamp."""

    def _check(self):
        with _in(self.root), patch.object(self.launcher, "plaintext_env_candidates", return_value=[]):
            return self.launcher._check_stack_schema()

    def test_each_odd_value_fails_naming_it(self):
        for value in ("99", "0", "5x", "-1", ""):
            with self.subTest(value=value):
                self.schema_5_project(env=value)
                result = self._check()
                self.assertEqual(result["level"], FAIL, result)
                self.assertIn(f'DLUX_STACK_SCHEMA to "{value}"', result["message"])
                self.assertIn('DLUX_STACK_SCHEMA: "5", the schema its files follow', result["fix"])
                with _in(self.root):
                    self.assertIsNone(stack_schema.deployment_schema("."), "an odd value passes no floor")

    def test_a_plain_schema_still_passes(self):
        self.schema_5_project()
        self.assertEqual(self._check()["level"], OK)
        self.assertEqual(stack_schema.deployment_schema(str(self.root)), 5)

    def test_the_values_as_written(self):
        text = 'x: &de\n  DLUX_STACK_SCHEMA: "5x"\n  B: 1\nother:\n  DLUX_STACK_SCHEMA: 4 # note\n'
        self.assertEqual(stack_schema.env_stamp_values(text), ["5x", "4"])
        self.assertEqual(stack_schema.invalid_env_stamps(text, 5), ["5x"])


class LineEndingEdgeTests(_Project):
    """R2-069: a mixed file came back all CRLF and a CR-only file came back LF."""

    def test_a_cr_only_file_keeps_cr(self):
        path = self.write("compose.yml", b"a: 1\rb: 2\r")
        self.assertEqual(stack_schema.line_ending(path), "\r")
        stack_schema.write_updates(self.root, {path: "a: 1\nb: 3\n"}, self.root / ".xclude" / "backup")
        self.assertEqual(path.read_bytes(), b"a: 1\rb: 3\r")

    def test_a_mixed_file_is_reported_and_left_alone(self):
        self.write("compose.yml", b"a: 1\r\nb: 2\nc: 3\r\n")
        self.write(".proxy/Caddyfile", b":80 {\n}\n")
        self.assertTrue(stack_schema.mixed_line_endings(self.root / "compose.yml"))
        self.assertFalse(stack_schema.mixed_line_endings(self.root / ".proxy" / "Caddyfile"))
        with _in(self.root):
            result = self.launcher._check_line_endings()
        self.assertEqual(result["level"], WARN)
        self.assertIn("compose.yml mixes", result["message"])
        self.assertIn("dos2unix compose.yml", result["fix"])

    def test_a_clean_stack_has_no_line_endings_finding(self):
        self.write("compose.yml", b"a: 1\n")
        with _in(self.root):
            self.assertIsNone(self.launcher._check_line_endings())


class ImageFloorTests(_Project):
    """R2-020: 1.17 files on the 1.15.1 image passed `check` with exit 0."""

    def _check(self, version):
        self.schema_5_project()
        with _in(self.root), patch.object(self.launcher, "_running_dlux_version", return_value=version):
            return self.launcher._check_image_floor()

    def test_files_newer_than_the_image_fail(self):
        result = self._check((1, 15, 1))
        self.assertEqual(result["level"], FAIL)
        self.assertIn("need DjangoLux 1.16.0 or later", result["message"])
        self.assertIn("private /media/ route", result["message"])
        self.assertIn("worker service", result["message"])
        self.assertIn("runs DjangoLux 1.15.1", result["message"])

    def test_only_the_unmet_floor_is_named(self):
        result = self._check((1, 15, 5))
        self.assertEqual(result["level"], FAIL)
        self.assertNotIn("private /media/ route", result["message"])

    def test_a_new_enough_image_passes_and_an_unknown_one_is_not_judged(self):
        self.assertEqual(self._check((1, 17, 0))["level"], OK)
        self.assertIsNone(self._check(None))

    def test_task_worker_makes_no_claim_on_an_image_without_dlux_worker(self):
        self.launcher.services = ["web", "worker"]
        with patch.object(self.launcher, "_running_dlux_version", return_value=(1, 15, 1)):
            self.assertIsNone(self.launcher._check_task_worker())


class ProxyConfigTests(_Project):
    """R2-067: a missing or truncated .proxy/Caddyfile passed `check`."""

    def _model(self):
        return {"name": "shop", "services": {
            "web": {"image": "shop/web"},
            "caddy": {"image": "caddy:2", "volumes": [
                {"type": "bind", "source": str(self.root / ".proxy" / "Caddyfile"), "target": "/etc/caddy/Caddyfile"},
                {"type": "bind", "source": str(self.root / "media"), "target": "/srv/media"},
            ]},
        }}

    def _check(self, answer=(True, "Valid configuration", "")):
        calls = []

        def run(argv, timeout=None, env=None):
            calls.append(argv)
            return answer

        with _in(self.root), patch.object(self.launcher, "compose_config_json", return_value=self._model()), \
                patch.object(self.launcher, "resolve_secrets", return_value=(True, "")), \
                patch.object(self.launcher, "run_command", side_effect=run):
            return self.launcher._check_proxy_config(), calls

    def test_a_missing_proxy_file_fails_and_a_data_folder_does_not(self):
        result, calls = self._check()
        self.assertEqual(result["level"], FAIL)
        self.assertIn("caddy mounts ./.proxy/Caddyfile at /etc/caddy/Caddyfile", result["message"])
        self.assertNotIn("media", result["message"])
        self.assertEqual(calls, [])

    def test_a_file_caddy_rejects_fails_with_caddys_words(self):
        self.write(".proxy/Caddyfile", ":80 {\n  reverse_proxy web:8000\n")
        result, calls = self._check((False, "", "Error: adapting config using caddyfile: unexpected EOF"))
        self.assertEqual(result["level"], FAIL)
        self.assertIn("caddy rejects ./.proxy/Caddyfile: Error: adapting config using caddyfile: unexpected EOF",
                      result["message"])
        argv = calls[0]
        self.assertEqual(argv[:2], ["docker", "run"])
        self.assertEqual(argv[argv.index("--pull") + 1], "never")
        self.assertEqual(argv[argv.index("--network") + 1], "none")

    def test_a_valid_file_passes_and_a_missing_image_is_not_judged(self):
        self.write(".proxy/Caddyfile", ":80 {\n}\n")
        self.assertIn("passed the proxy's own validation", self._check()[0]["message"])
        result, _calls = self._check((False, "", "docker: Error response from daemon: No such image: caddy:2"))
        self.assertEqual(result["level"], OK)
        self.assertIn("not validated", result["message"])


class ScaffoldDriftTests(_Project):
    """R2-019: a stack walked to 1.17 lacked the non-root preparation and `check` said nothing."""

    def _check(self):
        model = {"services": {"web": {"image": "shop/web", "volumes": [
            {"type": "volume", "source": "static", "target": "/app/staticfiles"},
            {"type": "bind", "source": "./media", "target": "/app/media"},
            {"type": "volume", "source": "exports", "target": "/app/exports"},
            {"type": "bind", "source": "./imports", "target": "/app/imports", "read_only": True},
        ]}}}
        with _in(self.root), patch.object(self.launcher, "compose_config_json", return_value=model):
            return self.launcher._check_scaffold_drift()

    def test_a_walked_stack_names_each_gap(self):
        self.write("Dockerfile", "FROM python:3.14\nUSER 10001:0\n")
        self.write("entrypoint.sh", "#!/bin/sh\nexec \"$@\"\n")
        self.write("compose.yml", "x-environment: &de\n  A: 1\nservices:\n  db:\n    image: postgres:17\n"
                                  "    environment:\n      <<: *de\n      POSTGRES_DB: app\n  web:\n    image: x\n")
        result = self._check()
        self.assertEqual(result["level"], WARN)
        for gap in ("does not prepare the app's mount points", "umask 0002", "dlux.mount_access gate",
                    "db in compose.yml merges the shared x-environment"):
            self.assertIn(gap, result["message"])

    def test_an_operator_mount_point_the_prep_misses_is_named(self):
        self.write("Dockerfile", "RUN mkdir -p /app/media /app/staticfiles \\\n    && chmod g+rwX /app/media /app/staticfiles\n")
        self.write("entrypoint.sh", "umask 0002\npython -m dlux.mount_access gate\n")
        self.write("compose.yml", "services:\n  db:\n    image: postgres:17\n")
        result = self._check()
        self.assertEqual(result["level"], WARN)
        self.assertIn("does not prepare /app/exports", result["message"])
        self.assertNotIn("/app/media", result["message"])
        self.assertNotIn("/app/imports", result["message"])

    def test_a_manage_py_without_the_inline_release_is_named(self):
        # R2-051, consumer side: 4 of 5 consumers' manage.py import the image's DjangoLux.
        self.write("Dockerfile", "RUN chmod g+rwX /app/media /app/staticfiles /app/exports\n")
        self.write("entrypoint.sh", "umask 0002\npython -m dlux.mount_access gate\n")
        self.write("compose.yml", "services:\n  db:\n    image: postgres:17\n")
        self.write("manage.py", "from django.core.management import execute_from_command_line\n")
        self.assertIn("manage.py does not resolve the active inline release", self._check()["message"])
        self.write("manage.py", "from dlux.updater.supervisor import resolve_release\n"
                                "from django.core.management import execute_from_command_line\n")
        self.assertEqual(self._check()["level"], OK)

    def test_a_deploy_folder_without_the_source_is_not_judged(self):
        self.write("compose.yml", "services: {}\n")
        self.assertIsNone(self._check())


class CircularAdviceTests(unittest.TestCase):
    """R2-021: "Still needs you" sent the operator back to `check --fix` for what it cannot repair."""

    PROXY = {"level": FAIL, "name": "docker-proxy", "message": "accepts writes",
             "fix": "Run 'composer check --fix'. A proxy outside the block must be edited by hand.",
             "manual": "remove docker-socket-proxy by hand"}

    def test_after_a_run_the_step_by_hand_is_named(self):
        report = guidance.summary([self.PROXY], [], [])
        self.assertEqual(report["still"], [("docker-proxy", "remove docker-socket-proxy by hand")])

    def test_a_finding_without_its_own_step_points_at_the_doc(self):
        entry = {"level": WARN, "name": "redis-persistence", "message": "aof",
                 "fix": "Run 'composer check --fix'."}
        step = dict(guidance.summary([entry], [], [])["still"])["redis-persistence"]
        self.assertIn("docs/check.md", step)
        self.assertNotIn("./start.sh check --fix", step)

    def test_a_declined_run_still_points_at_check_fix(self):
        report = guidance.summary([self.PROXY], [{"level": WARN, "name": "fix", "message": "Changes declined."}], [],
                                  declined=True)
        self.assertEqual(dict(report["still"])["docker-proxy"], "./start.sh check --fix")

    def test_a_failed_repair_lends_its_step_to_the_finding(self):
        result = {"level": FAIL, "name": "app-permissions", "message": "cannot use 1 entry",
                  "fix": "./start.sh check --fix adds the group bits"}
        fix = {"level": FAIL, "name": "fix:app-permissions", "message": "Could not give …",
               "manual": "on the host: sudo chmod g+rwX /srv/a/media"}
        still = dict(guidance.summary([result], [fix], [])["still"])
        self.assertEqual(still["app-permissions"], "on the host: sudo chmod g+rwX /srv/a/media")

    def test_the_redis_and_proxy_findings_carry_their_step(self):
        launcher = DockerComposeLauncher()
        launcher.services = ["docker-socket-proxy", "composer-executor"]
        model = {"services": {"docker-socket-proxy": {"environment": {"POST": "1"}}}}
        with patch.object(launcher, "run_docker_compose", return_value=(True, json.dumps(model), "")):
            proxy = launcher._check_docker_proxy()
        self.assertIn("by hand", proxy["manual"])
        launcher.services = ["redis"]
        with patch.object(launcher, "_redis_definition", return_value={"command": "redis-server --appendonly yes"}):
            redis = launcher._check_redis_persistence()
        self.assertIn("--appendonly no --save 60 1", redis["manual"])


class _FixRun(_Project):
    """``_maybe_fix`` on a given plan: the confirmation and the stack around it are stubbed."""

    KEYS = (
        "legacy", "needs_hardening", "needs_agent_block_migration", "needs_updater_migration", "needs_install",
        "needs_init_containers", "needs_dev_override_migration", "needs_post_start_migration",
        "needs_restart_labels", "retire_celery", "maintenance_legacy", "maintenance_unpublished", "redis_corrupt",
    )

    def plan(self, **wanted):
        plan = {key: False for key in self.KEYS}
        plan.update(fixes=[], refused=False, pending=True, init_host="worker", dev_override_file="",
                    base_compose_file="", needs_worker="", stale_wrappers=[], obsolete=[], proxy_routes=[],
                    media_exposed=[], front_proxy_files=[], relay_env_files=[], update_policy_files=[],
                    grant_access={}, stamp_repair={"files": [], "notes": [], "schema": None}, redis_files=[],
                    secret_writes={})
        plan.update(wanted)
        return plan

    def run_fix(self, plan, containers=()):
        args = SimpleNamespace(yes=True, file="", dev=False, no_recreate=False)
        with _in(self.root), patch.object(self.launcher, "_fix_plan", return_value=plan), \
                patch("composer.checkup.confirm", return_value=True), \
                patch.object(self.launcher, "_container_states", return_value=list(containers)):
            self.launcher._capture_lifecycle = False
            return self.launcher._maybe_fix(args, [])


class MaintenanceOnAStoppedStackTests(_FixRun):
    """R2-064: every `--fix` on a stopped stack exited 1 with "it needs Composer 1.7+"."""

    def test_an_unpublished_page_waits_for_the_agent(self):
        fixes = self.run_fix(self.plan(maintenance_unpublished=True))
        entry = next(fix for fix in fixes if fix["name"] == "fix:maintenance-page")
        self.assertEqual(entry["level"], WARN)
        self.assertIn("composer-agent is not running", entry["message"])
        self.assertNotIn("1.7", entry["message"])

    def test_the_check_says_the_agent_publishes_it_on_start(self):
        self.write("compose.yml", "services:\n  composer-agent:\n    image: x\n")
        self.launcher.services = ["composer-agent"]
        with _in(self.root), patch.object(self.launcher, "_container_states", return_value=[]):
            result = self.launcher._check_maintenance_page()
        self.assertEqual(result["level"], OK)
        self.assertIn("publishes the maintenance page when it starts", result["message"])


@unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root writes read-only files")
class ReadOnlyComposeTests(_FixRun):
    """R2-062: the resident-block repair rewrote a 0444 compose.yml by rename (mode lost)."""

    def test_the_rename_repairs_are_held_and_the_file_keeps_its_mode(self):
        compose = self.write("compose.yml", "services:\n  web:\n    image: x\n")
        compose.chmod(0o444)
        self.addCleanup(compose.chmod, 0o644)
        with patch("composer.agent_installer.enable_executor", side_effect=AssertionError("rewrote compose.yml")):
            fixes = self.run_fix(self.plan(needs_agent_block_migration=True))
        entry = next(fix for fix in fixes if fix["name"] == "fix:read-only-files")
        self.assertEqual(entry["level"], FAIL)
        self.assertIn("compose.yml", entry["message"])
        self.assertIn("Held: resident-block", entry["message"])
        self.assertEqual(compose.stat().st_mode & 0o777, 0o444)

    def test_obsolete_service_removal_refuses_a_read_only_file(self):
        from composer.stack_cleanup import StackCleanupError, remove_obsolete_services

        compose = self.write("compose.yml", "services:\n  web:\n    image: x\n  pgadmin:\n    image: y\n")
        compose.chmod(0o444)
        self.addCleanup(compose.chmod, 0o644)
        with self.assertRaises(StackCleanupError) as raised:
            remove_obsolete_services(str(self.root), ["compose.yml"], command_runner=self.fail)
        self.assertIn("read-only", str(raised.exception))


class CeleryRemovalRestampTests(_Project):
    """R2-061: the 4->5 step left compose.dev.yml at schema 4 until the next `--fix`."""

    def test_the_dev_file_the_removal_moved_is_restamped_in_the_same_run(self):
        self.schema_5_project()
        dev = self.root / "compose.dev.yml"
        # What retire_celery leaves: the override moved to worker, the stamp still 4.
        dev.write_text("# dlux stack schema 4\nservices:\n  web:\n    platform: linux/amd64\n"
                       "  worker:\n    platform: linux/amd64\n", encoding="utf-8")

        def runner(args, **_kwargs):
            model = {"services": {"web": {}, "worker": {"command": ["python", "manage.py", "dlux_worker"]}}}
            return SimpleNamespace(returncode=0, stdout=json.dumps(model), stderr="")

        with _in(self.root), patch("composer.worker_migration.shutil.which", return_value="/usr/bin/docker"), \
                patch("composer.worker_migration.subprocess.run", side_effect=runner):
            entries = self.launcher._finish_schema_after_celery()
        self.assertEqual([entry["level"] for entry in entries], [OK])
        self.assertIn("compose.dev.yml", entries[0]["message"])
        self.assertEqual(stack_schema.read_stamps(dev.read_text(encoding="utf-8")), [5])


class RedisStillDownTests(unittest.TestCase):
    """R2-063: a refused move was reported as "recreated on snapshots, which ignore them"."""

    def test_it_says_redis_stays_down_and_how_to_move_the_files(self):
        entry = DockerComposeLauncher._redis_still_down(
            "fix:redis-aof-corrupt", "Composer does not move an append-only file named 'aof dir'; move it by hand.",
            "shop_redis_data", "redis:7", "/data", ["redis-server", "--appendonly", "yes", "--appenddirname", "aof dir"],
            "20261127T000000Z",
        )
        self.assertEqual(entry["level"], FAIL)
        self.assertIn("redis stays down until the files are moved aside", entry["message"])
        self.assertNotIn("ignore them", entry["message"])
        self.assertIn('mv "/data/aof dir" /data/aof-archive/20261127T000000Z/', entry["message"])
        self.assertIn("by hand", entry["manual"])


class AppPermissionsCheckTests(_Project):
    """R2-070 (unknown image), R2-030 (grant refusal) and R2-032 (restart) through `check`."""

    MODEL = {"name": "shop", "services": {"web": {"image": "shop/web:1", "volumes": [
        {"type": "bind", "source": "/srv/shop/media", "target": "/app/media"}]}}}

    def test_an_image_not_on_the_host_is_not_called_root(self):
        with patch.object(self.launcher, "compose_config_json", return_value=self.MODEL), \
                patch.object(app_permissions, "image_identity", return_value=("unknown", None, None)), \
                patch.object(app_permissions, "run", return_value={"targets": [], "missing": []}):
            result = self.launcher._check_app_permissions()
        self.assertNotIn("runs as root", result["message"])
        self.assertIn("cannot read which user shop/web:1 runs as", result["message"])

    def _scan_wrong(self, containers):
        summary = {"missing": [], "targets": [{"label": "volume static", "wrong": 1, "sample": [
            {"path": ".", "kind": "dir", "owner": "0:0", "mode": "0755", "needs": "rwx"}]}]}
        with patch.object(self.launcher, "compose_config_json", return_value=self.MODEL), \
                patch.object(app_permissions, "image_identity", return_value=("user", 10001, 0)), \
                patch.object(app_permissions, "missing_app_containers", return_value=containers), \
                patch.object(app_permissions, "run", return_value=summary):
            return self.launcher._check_app_permissions(deep=True)

    def test_a_stack_whose_app_never_started_is_granted_at_the_first_start(self):
        # b3 re-run: a proxy repair's recreate created static and dlux_runtime (0755, empty), and the
        # recheck failed `check --fix` with "by hand", though './start.sh' grants them before web starts.
        result = self._scan_wrong(["web"])
        self.assertEqual(result["level"], OK)
        self.assertIn("before the first start", result["message"])
        self.assertIsNone(dict(guidance.summary([result], [], [])["still"]).get("app-permissions"))

    def test_a_started_app_that_cannot_use_its_data_still_fails(self):
        self.assertEqual(self._scan_wrong([])["level"], FAIL)

    def test_a_refused_grant_names_the_entry_and_the_host_step(self):
        summary = {"manifest": "", "targets": [{"label": "./media", "host": "/srv/shop/media", "errors": 1,
                   "changed": 0, "failed": [{"path": ".", "kind": "dir", "owner": "0:0", "mode": "0555",
                                             "needs": "rwx", "error": "Operation not permitted"}]}]}
        with patch.object(self.launcher, "compose_config_json", return_value=self.MODEL), \
                patch.object(app_permissions, "missing_app_containers", return_value=[]), \
                patch.object(app_permissions, "run", return_value=summary):
            entry = self.launcher._fix_app_permissions({"uid": 10001, "gid": 0})
        self.assertEqual(entry["level"], FAIL)
        self.assertIn("./media (dir 0:0 0555, needs rwx: Operation not permitted)", entry["message"])
        self.assertEqual(entry["manual"], "on the host: sudo chmod g+rwX /srv/shop/media")

    def test_a_grant_restarts_what_stopped_on_the_refusal_and_keeps_empty_volumes(self):
        summary = {"manifest": "/m", "targets": [{"label": "volume static", "changed": 1, "kept": 1}]}
        with patch.object(self.launcher, "compose_config_json", return_value=self.MODEL), \
                patch.object(app_permissions, "missing_app_containers", return_value=[]), \
                patch.object(app_permissions, "run", return_value=summary) as run, \
                patch.object(app_permissions, "restart_refused", return_value=["web"]) as restart:
            entry = self.launcher._fix_app_permissions({"uid": 10001, "gid": 0})
        self.assertEqual(entry["level"], OK)
        self.assertTrue(run.call_args.kwargs["keep_empty"])
        restart.assert_called_once()
        self.assertTrue(restart.call_args.kwargs["exited"])
        self.assertIn("Restarted web", entry["message"])
        self.assertIn(app_permissions.KEEP_NAME, entry["message"])
        self.assertIn("web", self.launcher._fix_recreated)

    def test_no_keep_file_before_the_containers_exist(self):
        summary = {"manifest": "", "targets": []}
        with patch.object(self.launcher, "compose_config_json", return_value=self.MODEL), \
                patch.object(app_permissions, "missing_app_containers", return_value=["web"]), \
                patch.object(app_permissions, "run", return_value=summary) as run:
            self.launcher._fix_app_permissions({"uid": 10001, "gid": 0})
        self.assertFalse(run.call_args.kwargs["keep_empty"])


class UpdatePolicyWordingTests(unittest.TestCase):
    """R2-070: fix:update-policy named a worker the celery stack does not have."""

    def test_only_the_readers_the_stack_has(self):
        from composer.checkup import policy_readers

        self.assertEqual(policy_readers(["web", "celery", "composer-agent", "composer-executor"]),
                         "web, composer-agent and composer-executor")
        self.assertEqual(policy_readers(["web", "worker", "composer-agent", "composer-executor"]),
                         "web, worker, composer-agent and composer-executor")


if __name__ == "__main__":
    unittest.main()


class _Tty:
    def isatty(self):
        return True


class _Pipe:
    def isatty(self):
        return False


def _answers(*values):
    queue = list(values)
    return lambda _prompt: queue.pop(0)


class AdminPassTests(_Project):
    """R2-039 (Composer side): a stack whose ADMIN_PASS is empty starts with no admin, and nothing says so."""

    COMPOSE = ('x-environment: &de\n  POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-admin_pass}\n'
               'services:\n  worker:\n    environment:\n      ADMIN_PASS: ${ADMIN_PASS:-}\n')
    SECRET = "s3cret-Passw0rd"

    def setUp(self):
        super().setUp()
        self.write("compose.yml", self.COMPOSE)
        self.secrets = self.write(".secrets/.env", "DJANGO_SECRET_KEY=k\nPOSTGRES_PASSWORD=admin_pass\nADMIN_PASS=\nTIME_ZONE=UTC\n")
        self.secrets.chmod(0o644)
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for key in ("ADMIN_PASS", "POSTGRES_PASSWORD", "COMPOSER_ASSUME_YES"):
            os.environ.pop(key, None)

    def test_both_warnings(self):
        with _in(self.root):
            admin = self.launcher._check_admin_pass()
            database = self.launcher._check_postgres_password()
        self.assertEqual(admin["level"], WARN)
        self.assertIn("ADMIN_PASS is empty", admin["message"])
        self.assertIn("never changed", admin["message"])
        self.assertIn("./start.sh run -m web createsuperuser", admin["fix"])
        self.assertEqual(database["level"], WARN)
        self.assertIn("published default admin_pass", database["message"])
        self.assertIn("inside PostgreSQL first", database["fix"])

    def test_a_missing_database_password_takes_the_compose_default(self):
        self.secrets.write_text("DJANGO_SECRET_KEY=k\nADMIN_PASS=Long-enough-1\n", encoding="utf-8")
        with _in(self.root):
            database = self.launcher._check_postgres_password()
            admin = self.launcher._check_admin_pass()
        self.assertIn("not set, so the Compose default admin_pass applies", database["message"])
        self.assertEqual(admin["level"], OK)
        self.assertNotIn("Long-enough-1", json.dumps([database, admin]))

    def test_the_question_asks_twice_hidden_and_skips_on_enter(self):
        out = __import__("io").StringIO()
        self.assertEqual(guidance.ask_admin_pass(reader=_answers(self.SECRET, self.SECRET), stream=out), self.SECRET)
        self.assertEqual(guidance.ask_admin_pass(reader=_answers(self.SECRET, "other-pass", self.SECRET, self.SECRET),
                                                 stream=out), self.SECRET)
        self.assertIsNone(guidance.ask_admin_pass(reader=_answers(""), stream=out))
        self.assertIsNone(guidance.ask_admin_pass(reader=_answers("short", "admin", 'with"quote'), stream=out))
        self.assertNotIn(self.SECRET, out.getvalue())
        self.assertIn("never changed", out.getvalue())

    def test_check_fix_asks_at_a_terminal_and_writes_0600_without_showing_it(self):
        results = [{"level": WARN, "name": "admin-pass", "message": ""}]
        self.launcher.services = ["worker"]
        out = __import__("io").StringIO()
        with _in(self.root):
            self.launcher._ask_operator_questions(results, reader=_answers("s"), stream=out,
                                                  secret_reader=_answers(self.SECRET, self.SECRET))
            self.assertEqual(self.launcher._operator_choices.get("ADMIN_PASS"), self.SECRET)
            entry = self.launcher._write_operator_choices({"ADMIN_PASS": self.SECRET})
        self.assertEqual(entry["level"], OK)
        self.assertNotIn(self.SECRET, json.dumps(entry))
        self.assertNotIn(self.SECRET, out.getvalue())
        self.assertEqual(self.secrets.stat().st_mode & 0o777, 0o600)
        text = self.secrets.read_text(encoding="utf-8")
        self.assertIn(f"ADMIN_PASS={self.SECRET}\n", text)
        self.assertIn("POSTGRES_PASSWORD=admin_pass\nADMIN_PASS=", text)
        self.assertTrue(text.endswith("TIME_ZONE=UTC\n"))
        self.assertEqual(list((self.root / ".secrets").iterdir()), [self.secrets])
        with _in(self.root):
            self.assertEqual(self.launcher._check_admin_pass()["level"], OK)

    def test_without_a_terminal_nothing_is_asked(self):
        args = SimpleNamespace(fix=True, yes=False, json=False)
        self.assertFalse(guidance.interactive(args, stdin=_Pipe()))
        with _in(self.root):
            self.launcher.ask_first_admin(stdin=_Pipe(), reader=lambda _prompt: self.fail("asked"))
        self.assertIn("ADMIN_PASS=\n", self.secrets.read_text(encoding="utf-8"))

    def test_the_first_start_asks_and_passes_it_to_this_deploy(self):
        with _in(self.root):
            self.launcher.ask_first_admin(stdin=_Tty(), reader=_answers(self.SECRET, self.SECRET))
        self.assertEqual(os.environ.get("ADMIN_PASS"), self.SECRET)
        self.assertEqual(self.secrets.stat().st_mode & 0o777, 0o600)

    def test_a_stack_deployed_before_is_not_asked(self):
        self.write(".xclude/composer-deploy/deployed.json", json.dumps({"image": "shop/web:1"}))
        with _in(self.root):
            self.launcher.ask_first_admin(stdin=_Tty(), reader=lambda _prompt: self.fail("asked"))

    def test_the_summary_line_quotes_the_migrator(self):
        line = ("worker-pre_start-1  | No superuser created: ADMIN_PASS is empty. Set ADMIN_PASS in .secrets/.env and "
                "restart the stack, or run 'python manage.py createsuperuser'.\n")
        with _in(self.root), patch.object(self.launcher, "migration_applier_services", return_value=["worker"]), \
                patch.object(self.launcher, "run_docker_compose", return_value=(True, line, "")):
            notice = self.launcher.no_admin_notice()
        self.assertTrue(notice[0].startswith("No admin account was created. No superuser created: ADMIN_PASS is empty."))
        self.assertIn("./start.sh run -m web createsuperuser", notice[1])
        exists = "worker-1 | Superuser admin already exists.\n"
        with _in(self.root), patch.object(self.launcher, "migration_applier_services", return_value=["worker"]), \
                patch.object(self.launcher, "run_docker_compose", return_value=(True, exists, "")):
            self.assertEqual(self.launcher.no_admin_notice(), [])


class AdminPassPlanTests(_FixRun):
    """The confirmation and the fixes never show the typed password."""

    def test_the_plan_lists_the_line_without_the_value(self):
        self.write("compose.yml", "services:\n  worker:\n    environment:\n      ADMIN_PASS: ${ADMIN_PASS:-}\n")
        self.write(".secrets/.env", "ADMIN_PASS=\n")
        listed = []
        plan = self.plan(secret_writes={"ADMIN_PASS": "s3cret-Passw0rd"})
        args = SimpleNamespace(yes=True, file="", dev=False, no_recreate=False)
        with _in(self.root), patch.object(self.launcher, "_fix_plan", return_value=plan), \
                patch("composer.checkup.confirm", side_effect=lambda title, lines, **_k: listed.extend(lines) or True), \
                patch.object(self.launcher, "_container_states", return_value=[]):
            self.launcher._capture_lifecycle = False
            fixes = self.launcher._maybe_fix(args, [])
        self.assertTrue(any("ADMIN_PASS=(the password you typed, not shown)" in line for line in listed), listed)
        self.assertNotIn("s3cret-Passw0rd", json.dumps([listed, fixes]))
