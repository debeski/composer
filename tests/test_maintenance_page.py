import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from composer import maintenance_migration as mm
from composer import maintenance_page as mp
from composer.checkup import OK, WARN
from composer.launcher import DockerComposeLauncher

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LEGACY = FIXTURES / "legacy-proxy"
DLUX_PROJECT = (
    Path(__file__).resolve().parents[3] / "pkg-django-lux" / "main" / "dlux" / "scaffold" / "templates" / "project"
)


class LegacyPageClassificationTests(unittest.TestCase):
    """`inspect` decides whether a migrated stack keeps its page as an override."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".proxy").mkdir()
        self.page = self.root / ".proxy" / "maintenance.html"

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_stock_page_from_an_older_djangolux_is_stale(self):
        self.page.write_bytes((FIXTURES / "maintenance-stock-1.8.html").read_bytes())
        self.assertEqual(mp.inspect(self.root)["state"], mp.STALE_STOCK_PAGE)

    def test_a_customised_page_with_the_old_logic_is_custom_stuck(self):
        self.page.write_text("<title>Updating…</title><script>var sawProgress; var readyCount;</script>")
        self.assertEqual(mp.inspect(self.root)["state"], mp.CUSTOM_STUCK)

    def test_current_and_other_custom_pages(self):
        self.page.write_text(mp.render(2))
        self.assertEqual(mp.inspect(self.root)["state"], mp.CURRENT)
        self.page.write_text("<p>our own page</p>")
        self.assertEqual(mp.inspect(self.root)["state"], mp.CUSTOM)
        self.page.unlink()
        self.assertEqual(mp.inspect(self.root)["state"], mp.MISSING)


class PublishTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_bundle_has_no_inline_script_or_style(self):
        html = (mp.BUNDLE / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("<style", html)
        self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)")
        self.assertIn('href="/_dlux/maintenance/maintenance.css"', html)
        self.assertIn('src="/_dlux/maintenance/maintenance.js"', html)
        script = (mp.BUNDLE / "maintenance.js").read_text(encoding="utf-8")
        self.assertIn("window.location.replace(target)", script)
        self.assertNotIn("sawProgress", script)

    def test_publish_writes_the_bundle_once(self):
        self.assertTrue(mp.publish(self.runtime))
        for name in mp.BUNDLE_FILES:
            self.assertEqual((mp.published_dir(self.runtime) / name).read_bytes(), (mp.BUNDLE / name).read_bytes())
        self.assertTrue(mp.is_published(self.runtime))
        self.assertFalse(mp.publish(self.runtime), "an unchanged bundle is not rewritten")

    def test_an_override_wins_and_survives_the_agent(self):
        override = self.runtime / "project-override"
        override.mkdir()
        (override / "index.html").write_text("<p>ours</p>", encoding="utf-8")
        self.assertEqual(mp.publish_override(override, self.runtime), 1)
        self.assertFalse(mp.publish(self.runtime))
        self.assertEqual((mp.published_dir(self.runtime) / "index.html").read_text(encoding="utf-8"), "<p>ours</p>")


class MigrationTransformTests(unittest.TestCase):
    def test_legacy_files_are_detected_and_migrated_once(self):
        caddy, nginx, compose = (
            (LEGACY / name).read_text(encoding="utf-8") for name in ("Caddyfile", "default.conf.template", "compose.yml")
        )
        self.assertTrue(mm.is_legacy_caddyfile(caddy))
        self.assertTrue(mm.is_legacy_nginx(nginx))
        self.assertTrue(mm.is_legacy_compose(compose))

        caddy2, nginx2, compose2 = mm.migrate_caddyfile(caddy), mm.migrate_nginx(nginx), mm.migrate_compose(compose)
        self.assertFalse(mm.is_legacy_caddyfile(caddy2))
        self.assertFalse(mm.is_legacy_nginx(nginx2))
        self.assertFalse(mm.is_legacy_compose(compose2))
        self.assertIn(mm.caddy_asset_block("\t\t"), caddy2)
        self.assertIn(f"root * {mm.RUNTIME_PAGE_DIR}", caddy2)
        self.assertIn("rewrite * /index.html", caddy2)
        self.assertIn(f"alias {mm.RUNTIME_PAGE_DIR}/index.html;", nginx2)
        self.assertIn("location ^~ /_dlux/maintenance/", nginx2)
        self.assertNotIn("maintenance.html:", compose2)
        self.assertEqual(mm.migrate_nginx(nginx2), nginx2, "nginx migration is idempotent")

    def test_a_caddyfile_without_the_update_log_route_is_left_for_a_human(self):
        with self.assertRaises(mm.StackCleanupError):
            mm.migrate_caddyfile("root * /usr/share/caddy/maintenance\n")


class FakeDocker:
    def __init__(self, caddyfile, running=("caddy", "composer-agent")):
        self.calls = []
        self.caddyfile = caddyfile
        self.running = running

    def __call__(self, args, **_kwargs):
        self.calls.append(args)
        out = ""
        if "config" in args:
            out = (
                '{"services": {"caddy": {"volumes": [{"type": "bind", "source": "%s",'
                ' "target": "/etc/caddy/Caddyfile"}]}, "composer-agent": {}}}' % self.caddyfile
            )
        elif "ps" in args:
            out = "\n".join(self.running)
        return subprocess.CompletedProcess(args, 0, out, "")


class MigrateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / ".proxy").mkdir()
        for name, target in (("Caddyfile", ".proxy/Caddyfile"), ("compose.yml", "compose.yml")):
            (self.root / target).write_text((LEGACY / name).read_text(encoding="utf-8"), encoding="utf-8")
        self.docker = FakeDocker(str(self.root / ".proxy" / "Caddyfile"))

    def tearDown(self):
        self._tmp.cleanup()

    def _migrate(self):
        with patch("composer.maintenance_migration.shutil.which", return_value="/usr/bin/docker"):
            return mm.migrate(str(self.root), ["compose.yml"], command_runner=self.docker)

    def _commands(self):
        return [" ".join(call) for call in self.docker.calls]

    def test_a_stock_page_is_archived_and_the_bundle_published(self):
        (self.root / ".proxy" / "maintenance.html").write_bytes((FIXTURES / "maintenance-stock-1.8.html").read_bytes())
        outcome = self._migrate()
        self.assertTrue(outcome["applied"])
        self.assertEqual(sorted(outcome["files"]), [".proxy/Caddyfile", "compose.yml"])
        self.assertEqual(outcome["override"], "")
        self.assertFalse((self.root / ".proxy" / "maintenance.html").exists())
        archive = Path(outcome["backup_root"]) / "maintenance-migration"
        self.assertTrue((archive / ".proxy" / "maintenance.html").is_file())
        self.assertTrue(mm.is_legacy_caddyfile((archive / ".proxy" / "Caddyfile").read_text(encoding="utf-8")))
        self.assertFalse(mm.inspect(self.root, ["compose.yml"])["legacy"])
        commands = self._commands()
        self.assertTrue(any("caddy validate" in c for c in commands))
        self.assertTrue(any("exec -T composer-agent python -m composer maintenance publish" in c for c in commands))
        self.assertTrue(any("up -d --no-deps --force-recreate caddy" in c for c in commands))
        self.assertEqual(outcome["recreated"], ["caddy"])

    def test_a_customised_page_becomes_the_override(self):
        (self.root / ".proxy" / "maintenance.html").write_text("<p>our own page</p>", encoding="utf-8")
        outcome = self._migrate()
        self.assertEqual(outcome["override"], ".proxy/maintenance/index.html")
        self.assertEqual(
            (self.root / ".proxy" / "maintenance" / "index.html").read_text(encoding="utf-8"), "<p>our own page</p>"
        )
        commands = self._commands()
        self.assertTrue(any(" cp " in f" {c} " for c in commands))
        self.assertTrue(any("--override /tmp/dlux-maintenance-override" in c for c in commands))

    def test_a_schema_2_stack_is_restamped_3(self):
        from composer.stack_schema import read_stamp

        for target in ("compose.yml", ".proxy/Caddyfile"):
            path = self.root / target
            path.write_text(path.read_text(encoding="utf-8").replace("{{ stack_schema }}", "2"), encoding="utf-8")
        (self.root / "Dockerfile").write_text('LABEL org.dlux.stack-schema="2"\n', encoding="utf-8")
        (self.root / "gunicorn.py").write_text("# unstamped\n", encoding="utf-8")
        outcome = self._migrate()
        for target in ("compose.yml", ".proxy/Caddyfile", "Dockerfile"):
            self.assertEqual(read_stamp((self.root / target).read_text(encoding="utf-8")), 3, target)
        self.assertIn("Dockerfile", outcome["files"])
        self.assertNotIn("gunicorn.py", outcome["files"])

    def test_a_migrated_stack_is_left_alone(self):
        self._migrate()
        self.docker.calls.clear()
        self.assertFalse(self._migrate()["applied"])
        self.assertEqual(self.docker.calls, [])


class PreviewTests(MigrateTests):
    """The repair preview DjangoLux shows before an operator applies `check --fix`."""

    def _snapshot(self):
        return {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def test_the_preview_shows_exactly_what_the_migration_writes_and_writes_nothing(self):
        (self.root / ".proxy" / "maintenance.html").write_bytes((FIXTURES / "maintenance-stock-1.8.html").read_bytes())
        before = self._snapshot()
        preview = mm.preview(str(self.root), ["compose.yml"])
        self.assertEqual(self._snapshot(), before, "a preview writes nothing")
        self.assertEqual(self.docker.calls, [], "and runs nothing")
        self.assertEqual(preview["files"], [".proxy/Caddyfile", "compose.yml"])
        self.assertIn("+++ b/.proxy/Caddyfile", preview["diff"])
        added = [line for line in preview["diff"].splitlines() if line.startswith("+")]
        removed = [line for line in preview["diff"].splitlines() if line.startswith("-")]
        self.assertTrue(any("handle /_dlux/maintenance/* {" in line for line in added))
        self.assertTrue(any(f"root * {mm.RUNTIME_PAGE_DIR}" in line for line in added))
        self.assertTrue(any("./.proxy/maintenance.html:/usr/share/caddy" in line for line in removed))
        self.assertIn("moves to the backup", preview["note"])
        self.assertNotIn("override", preview["note"], "a stock page is not kept")

        outcome = self._migrate()
        self.assertEqual(sorted(outcome["files"]), preview["files"], "the preview and the migration share one plan")

    def test_a_customised_page_is_announced_as_the_override(self):
        (self.root / ".proxy" / "maintenance.html").write_text("<p>our own page</p>", encoding="utf-8")
        self.assertIn(".proxy/maintenance/index.html", mm.preview(str(self.root), ["compose.yml"])["note"])

    def test_a_migrated_stack_previews_nothing(self):
        self._migrate()
        self.assertEqual(mm.preview(str(self.root), ["compose.yml"]), {"files": [], "diff": "", "note": ""})


class CheckFindingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cwd = os.getcwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self.cwd)
        self._tmp.cleanup()

    def test_a_legacy_stack_is_told_to_migrate(self):
        (self.root / "compose.yml").write_text((LEGACY / "compose.yml").read_text(encoding="utf-8"), encoding="utf-8")
        result = DockerComposeLauncher()._check_maintenance_page()
        self.assertEqual(result["level"], WARN)
        self.assertIn("check --fix", result["fix"])

    def test_declining_check_fix_migrates_nothing(self):
        from types import SimpleNamespace

        (self.root / "compose.yml").write_text((LEGACY / "compose.yml").read_text(encoding="utf-8"), encoding="utf-8")
        launcher = DockerComposeLauncher()
        launcher.services = ["web", "caddy", "composer-agent"]
        with patch("composer.checkup.confirm", return_value=False), \
                patch("composer.maintenance_migration.migrate") as migrate:
            launcher._maybe_fix(SimpleNamespace(fix=True, yes=False, file="", dry_run=False, json=False), [])
        migrate.assert_not_called()

    def _published(self, files):
        runtime = self.root / "runtime"
        page = runtime / "proxy" / "maintenance"
        page.mkdir(parents=True)
        for name in files:
            (page / name).write_text("x", encoding="utf-8")
        (self.root / "compose.yml").write_text("services:\n  web: {}\n  composer-agent: {}\n", encoding="utf-8")
        launcher = DockerComposeLauncher()
        launcher.services = ["web", "composer-agent"]
        return launcher, patch.object(mm, "RUNTIME_PAGE_DIR", str(page))

    def test_inside_the_agent_the_page_is_read_from_the_volume_not_through_exec(self):
        # The agent's socket proxy refuses `exec`; asking it made every panel
        # check say a published page was missing (found accepting 1.13.0b5).
        launcher, runtime = self._published(["index.html"])
        with runtime, patch.object(launcher, "run_docker_compose", side_effect=AssertionError("no exec")):
            result = launcher._check_maintenance_page()
        self.assertEqual(result["level"], OK, result["message"])

    def test_inside_the_agent_a_missing_page_is_still_reported(self):
        launcher, runtime = self._published([])
        with runtime, patch.object(launcher, "run_docker_compose", side_effect=AssertionError("no exec")):
            self.assertEqual(launcher._check_maintenance_page()["level"], WARN)

    def test_inside_the_agent_the_override_marker_is_read_from_the_volume(self):
        launcher, runtime = self._published(["index.html", mp.CUSTOM_MARKER])
        (self.root / mp.OVERRIDE).mkdir(parents=True)
        with runtime, patch.object(launcher, "run_docker_compose", side_effect=AssertionError("no exec")):
            result = launcher._check_maintenance_page()
        self.assertEqual(result["level"], OK)
        self.assertIn("project page", result["message"])

    def test_on_the_host_composer_agent_is_asked(self):
        launcher = DockerComposeLauncher()
        with patch.object(mm, "RUNTIME_PAGE_DIR", str(self.root / "absent" / "proxy" / "maintenance")), \
                patch.object(launcher, "run_docker_compose", return_value=(True, "", "")) as run:
            self.assertTrue(launcher._maintenance_page_published())
        self.assertIn("exec", run.call_args[0][0])

    def test_a_stack_without_an_agent_has_nothing_to_publish(self):
        (self.root / "compose.yml").write_text("services:\n  web: {}\n", encoding="utf-8")
        self.assertEqual(DockerComposeLauncher()._check_maintenance_page()["level"], OK)


class DjangoLuxScaffoldTests(unittest.TestCase):
    def test_the_scaffold_caddyfile_carries_the_same_asset_route(self):
        caddyfile = DLUX_PROJECT / ".proxy" / "Caddyfile.tmpl"
        if not caddyfile.is_file() or mm.is_legacy_caddyfile(caddyfile.read_text(encoding="utf-8")):
            self.skipTest("No schema-3 DjangoLux checkout alongside")
        text = caddyfile.read_text(encoding="utf-8")
        self.assertIn(mm.caddy_asset_block("\t\t"), text)
        self.assertIn(f"root * {mm.RUNTIME_PAGE_DIR}", text)
