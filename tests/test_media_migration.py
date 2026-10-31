import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from composer import media_migration as media
from composer.checkup import FAIL, OK, WARN
from composer.launcher import DockerComposeLauncher

DLUX_PROJECT = (
    Path(__file__).resolve().parents[3] / "pkg-django-lux" / "main" / "dlux" / "scaffold" / "templates" / "project"
)

LEGACY_CADDY = """# dlux stack schema 3
{$CADDY_SITE_ADDRESS::80} {
\troute {
\t\t# Never serve dlux DB backups over media.
\t\t# (nginx: location ^~ /media/dlux_backups/ { return 404; })
\t\trespond /media/dlux_backups/* 404

\t\t# Media files. (nginx: location /media/ { alias /app/media/; })
\t\thandle_path /media/* {
\t\t\troot * /app/media
\t\t\tfile_server
\t\t}

\t\treverse_proxy web:8000
\t}
}
"""

LEGACY_NGINX = """# dlux stack schema 3
map $http_x_forwarded_proto $forwarded_proto {
    default $http_x_forwarded_proto;
    ""      $scheme;
}

server {
    set $web_upstream "web:8000";

    location ^~ /media/dlux_backups/ {
        return 404;
    }

    location /media/ {
        alias /app/media/;
    }

    location / {
        proxy_pass http://$web_upstream;
    }
}
"""


class ClassifyTests(unittest.TestCase):
    def test_the_generated_block_alone_is_legacy(self):
        self.assertEqual(media.classify(LEGACY_CADDY, "caddy"), media.LEGACY)
        self.assertEqual(media.classify(LEGACY_NGINX, "nginx"), media.LEGACY)

    def test_a_file_with_its_own_media_rules_is_custom(self):
        custom = LEGACY_CADDY.replace("\t\trespond /media/dlux_backups/* 404\n",
                                      "\t\trespond /media/dlux_backups/* 404\n\t\trespond /media/receipts/* 404\n")
        self.assertEqual(media.classify(custom, "caddy"), media.CUSTOM)

    def test_a_migrated_file_is_private_and_no_route_is_empty(self):
        self.assertEqual(media.classify(media.migrate_caddyfile(LEGACY_CADDY), "caddy"), media.PRIVATE)
        self.assertEqual(media.classify(media.migrate_nginx(LEGACY_NGINX), "nginx"), media.PRIVATE)
        self.assertEqual(media.classify("{\n}\n", "caddy"), "")


class TransformTests(unittest.TestCase):
    def test_caddy_asks_the_app_and_keeps_only_public_folders_open(self):
        text = media.migrate_caddyfile(LEGACY_CADDY)
        self.assertNotIn("handle_path /media/*", text)
        self.assertIn("forward_auth web:8000 {\n\t\t\t\t\turi /sys/files/media-access/", text)
        self.assertIn("@public_media path /media/public/* /media/dlux/branding/*", text)
        self.assertIn("respond /media/dlux_backups/* 404", text)
        self.assertLess(text.index("@public_media"), text.index("handle /media/*"))

    def test_caddy_uses_the_stack_s_own_upstream(self):
        text = media.migrate_caddyfile(LEGACY_CADDY.replace("reverse_proxy web:8000", "reverse_proxy app:9000"))
        self.assertIn("forward_auth app:9000 {", text)

    def test_nginx_uses_auth_request_and_sets_the_forwarded_uri_itself(self):
        text = media.migrate_nginx(LEGACY_NGINX)
        self.assertNotIn("alias /app/media/", text)
        self.assertIn("auth_request /_dlux_media_access;", text)
        self.assertIn("proxy_pass http://$web_upstream/sys/files/media-access/;", text)
        self.assertIn("proxy_set_header X-Forwarded-Uri $request_uri;", text)

    def test_an_nginx_template_without_forwarded_proto_is_left_for_a_human(self):
        with self.assertRaises(media.StackCleanupError):
            media.migrate_nginx("server {" + LEGACY_NGINX.split("server {")[1])


class PlanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".proxy").mkdir()
        (self.root / ".proxy" / "Caddyfile").write_text(LEGACY_CADDY, encoding="utf-8")
        (self.root / "compose.yml").write_text('x-environment: &env\n  DLUX_STACK_SCHEMA: "3"\n', encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_inspect_reports_the_exposed_file(self):
        state = media.inspect(self.root)
        self.assertTrue(state["exposed"])
        self.assertEqual(state["files"], {".proxy/Caddyfile": media.LEGACY})

    def test_the_plan_rewrites_the_proxy_and_restamps_three_to_four(self):
        _sources, updates = media._plan(self.root.resolve(), ["compose.yml"])
        by_name = {path.name: text for path, text in updates.items()}
        self.assertIn("# dlux stack schema 4", by_name["Caddyfile"])
        self.assertIn('DLUX_STACK_SCHEMA: "4"', by_name["compose.yml"])

    def test_preview_writes_nothing(self):
        before = (self.root / ".proxy" / "Caddyfile").read_text(encoding="utf-8")
        result = media.preview(str(self.root), ["compose.yml"])
        self.assertIn(".proxy/Caddyfile", result["files"])
        self.assertIn("+\t\t\t\tforward_auth web:8000 {", result["diff"])
        self.assertEqual((self.root / ".proxy" / "Caddyfile").read_text(encoding="utf-8"), before)


class CheckTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cwd = os.getcwd()
        os.chdir(self.root)
        (self.root / ".proxy").mkdir()
        (self.root / "compose.yml").write_text("services:\n  web: {}\n", encoding="utf-8")

    def tearDown(self):
        os.chdir(self.cwd)
        self._tmp.cleanup()

    def write(self, text):
        (self.root / ".proxy" / "Caddyfile").write_text(text, encoding="utf-8")

    def test_a_public_media_proxy_is_a_security_failure(self):
        self.write(LEGACY_CADDY)
        result = DockerComposeLauncher()._check_media_privacy()
        self.assertEqual(result["level"], FAIL)
        self.assertIn("SECURITY", result["message"])
        self.assertIn("check --fix", result["fix"])

    def test_custom_rules_warn_and_private_is_ok(self):
        self.write(LEGACY_CADDY.replace("\t\trespond /media/dlux_backups/* 404\n",
                                        "\t\trespond /media/dlux_backups/* 404\n\t\trespond /media/receipts/* 404\n"))
        self.assertEqual(DockerComposeLauncher()._check_media_privacy()["level"], WARN)
        self.write(media.migrate_caddyfile(LEGACY_CADDY))
        self.assertEqual(DockerComposeLauncher()._check_media_privacy()["level"], OK)

    def _fix(self, launcher):
        args = SimpleNamespace(fix=True, yes=True, file="", dry_run=False, json=False)
        return launcher._maybe_fix(args, [])

    def test_fix_refuses_while_the_running_djangolux_lacks_the_check(self):
        self.write(LEGACY_CADDY)
        launcher = DockerComposeLauncher()
        launcher.services = ["web", "caddy"]
        with patch.object(launcher, "_running_dlux_version", return_value=(1, 15, 2)), \
                patch("composer.media_migration.migrate") as migrate:
            fixes = self._fix(launcher)
        migrate.assert_not_called()
        stored = [entry for entry in fixes if entry["name"] == "fix:stored-files"]
        self.assertEqual(stored[0]["level"], FAIL)
        self.assertIn("1.15.2", stored[0]["message"])

    def test_fix_migrates_once_djangolux_answers_the_check(self):
        self.write(LEGACY_CADDY)
        launcher = DockerComposeLauncher()
        launcher.services = ["web", "caddy"]
        outcome = {"files": [".proxy/Caddyfile"], "recreated": ["caddy"], "backup_root": "/x"}
        with patch.object(launcher, "_running_dlux_version", return_value=(1, 15, 3)), \
                patch("composer.media_migration.migrate", return_value=outcome) as migrate:
            fixes = self._fix(launcher)
        migrate.assert_called_once()
        stored = [entry for entry in fixes if entry["name"] == "fix:stored-files"]
        self.assertEqual(stored[0]["level"], OK)

    def test_a_successful_fix_replaces_the_stale_failure(self):
        from contextlib import ExitStack

        self.write(LEGACY_CADDY)
        launcher = DockerComposeLauncher()
        launcher.services = ["web", "caddy"]

        def fix(args, results):
            self.write(media.migrate_caddyfile(LEGACY_CADDY))
            return [{"level": OK, "name": "fix:stored-files", "message": "Made stored files private."}]

        ok = {"level": OK, "name": "other", "message": ""}
        with ExitStack() as stack:
            for name in ("_check_compose_files", "_check_compose_parses", "_check_secrets", "_check_composer_channel",
                         "_check_required_vars", "_check_topology", "_check_removed_services",
                         "_check_dlux_updater_executor", "_check_proxy_routes", "_check_stack_schema",
                         "_check_maintenance_page", "_check_versions"):
                stack.enter_context(patch.object(launcher, name, return_value=ok))
            for name in ("_check_docker", "_check_wrappers"):
                stack.enter_context(patch.object(launcher, name, return_value=[]))
            for name in ("_check_resident_commands", "_check_docker_proxy", "resolve_active_compose_files",
                         "_requested_channel"):
                stack.enter_context(patch.object(launcher, name, return_value=None))
            stack.enter_context(patch.object(launcher, "_maybe_fix", side_effect=fix))
            results, fixed = launcher.collect_checkup(SimpleNamespace(file="", dev=False, fix=True, deep=False))
        stored = [entry for entry in results if entry["name"] == "stored-files"]
        self.assertEqual(stored[0]["level"], OK)
        self.assertFalse(any(entry["level"] == FAIL for entry in results + fixed))

    def test_the_running_version_prefers_the_active_inline_release(self):
        launcher = DockerComposeLauncher()
        launcher.services = ["web"]
        with patch.object(launcher, "run_docker_compose",
                          return_value=(True, '{"source": "volume", "version": "1.15.3"}', "")), \
                patch.object(launcher, "_dlux_runtime_version", side_effect=AssertionError("image not read")):
            self.assertEqual(launcher._running_dlux_version(), (1, 15, 3))
        with patch.object(launcher, "run_docker_compose", return_value=(True, '{"source": "image"}', "")), \
                patch.object(launcher, "_dlux_runtime_version", return_value=(1, 15, 2)):
            self.assertEqual(launcher._running_dlux_version(), (1, 15, 2))


class DjangoLuxScaffoldTests(unittest.TestCase):
    def test_the_scaffold_proxies_carry_the_same_media_blocks(self):
        caddyfile = DLUX_PROJECT / ".proxy" / "Caddyfile.tmpl"
        nginx = DLUX_PROJECT / ".proxy" / "default.conf.template.tmpl"
        if not caddyfile.is_file() or media.ACCESS_URI not in caddyfile.read_text(encoding="utf-8"):
            self.skipTest("No stack-schema-4 DjangoLux checkout alongside")
        self.assertIn(media.caddy_media_blocks("\t\t"), caddyfile.read_text(encoding="utf-8"))
        self.assertIn(media.nginx_media_blocks("    "), nginx.read_text(encoding="utf-8"))
