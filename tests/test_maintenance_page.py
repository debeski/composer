import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from composer import maintenance_page as mp
from composer.checkup import OK, WARN
from composer.launcher import DockerComposeLauncher

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class MaintenancePageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".proxy").mkdir()
        self.page = self.root / ".proxy" / "maintenance.html"

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_bundled_page_has_no_stuck_logic(self):
        text = mp.render(2)
        self.assertNotIn("sawProgress", text)
        self.assertIn("window.location.replace(target)", text)
        self.assertIn("<!-- dlux stack schema 2 -->", text)
        self.assertNotIn("dlux stack schema", mp.render(None))

    def test_a_stock_page_from_an_older_djangolux_is_stale(self):
        self.page.write_bytes((FIXTURES / "maintenance-stock-1.8.html").read_bytes())
        self.assertEqual(mp.inspect(self.root)["state"], mp.STALE_STOCK_PAGE)

    def test_a_customised_page_with_the_old_logic_is_reported_not_replaced(self):
        self.page.write_text("<title>Updating…</title><script>var sawProgress; var readyCount;</script>")
        self.assertEqual(mp.inspect(self.root)["state"], mp.CUSTOM_STUCK)

    def test_current_and_other_custom_pages(self):
        self.page.write_text(mp.render(2))
        self.assertEqual(mp.inspect(self.root)["state"], mp.CURRENT)
        self.page.write_text(mp.render(None))
        self.assertEqual(mp.inspect(self.root)["state"], mp.CURRENT)
        self.page.write_text("<p>our own page</p>")
        self.assertEqual(mp.inspect(self.root)["state"], mp.CUSTOM)
        self.page.unlink()
        self.assertEqual(mp.inspect(self.root)["state"], mp.MISSING)

    def test_install_archives_and_rewrites_in_place(self):
        stock = (FIXTURES / "maintenance-stock-1.8.html").read_bytes()
        self.page.write_bytes(stock)
        inode = self.page.stat().st_ino
        archive = self.root / ".xclude" / "composer-check" / "x"
        mp.install(self.root, 2, archive)
        self.assertEqual(self.page.stat().st_ino, inode, "a file bind mount keeps its inode")
        self.assertEqual(self.page.read_text(encoding="utf-8"), mp.render(2))
        self.assertEqual((archive / ".proxy" / "maintenance.html").read_bytes(), stock)
        self.assertEqual(mp.inspect(self.root)["state"], mp.CURRENT)

    def test_check_findings(self):
        cwd = os.getcwd()
        os.chdir(self.root)
        try:
            launcher = DockerComposeLauncher()
            self.page.write_bytes((FIXTURES / "maintenance-stock-1.8.html").read_bytes())
            result = launcher._check_maintenance_page()
            self.assertEqual(result["level"], WARN)
            self.assertIn("check --fix", result["fix"])
            self.page.write_text(mp.render(None))
            self.assertEqual(launcher._check_maintenance_page()["level"], OK)
        finally:
            os.chdir(cwd)


class BundledPageMirrorTests(unittest.TestCase):
    def test_bundled_template_matches_djangolux_when_both_checkouts_are_present(self):
        dlux = Path(__file__).resolve().parents[3] / "pkg-django-lux" / "main" / "dlux" / "scaffold" / "templates" / "project" / ".proxy" / "maintenance.html.tmpl"
        if not dlux.is_file():
            self.skipTest("DjangoLux checkout not alongside")
        self.assertEqual(mp.TEMPLATE.read_text(encoding="utf-8"), dlux.read_text(encoding="utf-8"))
