"""`composer check` reports a proxy without DjangoLux's Fetch Metadata policy.

DjangoLux's scaffold names the policy `cross_site_request` in both proxy files.
These tests pin what counts as present (defined and refused with 403), that the
finding is a WARN naming the files and the fix, and that the DjangoLux scaffold
alongside passes and fails once its block is taken out.
"""

import io
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from composer import fetch_metadata as fm
from composer.checkup import OK, WARN
from composer.launcher import DockerComposeLauncher

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCHEMA_4 = FIXTURES / "schema-4"
DLUX_CHECKOUT = Path(
    os.environ.get("DLUX_CHECKOUT") or Path(__file__).resolve().parents[3] / "pkg-django-lux" / "main"
)
DLUX_PROXY = DLUX_CHECKOUT / "dlux" / "scaffold" / "templates" / "project" / ".proxy"

CADDY_POLICY = (
    "\t\t@cross_site_request expression <<CEL\n"
    '\t\t\t{header.Sec-Fetch-Site} == "cross-site"\n'
    '\t\t\t&& !(method("GET", "HEAD")\n'
    '\t\t\t\t&& {header.Sec-Fetch-Mode} == "navigate"\n'
    '\t\t\t\t&& !({header.Sec-Fetch-Dest} in ["object", "embed"]))\n'
    "\t\t\tCEL\n"
    "\t\trespond @cross_site_request 403\n"
)
NGINX_MAP = (
    'map "$http_sec_fetch_site:$request_method:$http_sec_fetch_mode:$http_sec_fetch_dest" $cross_site_request {\n'
    "    default 0;\n"
    '    "~^cross-site:(GET|HEAD):navigate:(object|embed)$" 1;\n'
    '    "~^cross-site:(GET|HEAD):navigate:" 0;\n'
    '    "~^cross-site:" 1;\n'
    "}\n"
)
NGINX_REFUSAL = "        if ($cross_site_request) { return 403; }\n"


def _copy(source: Path, case: unittest.TestCase) -> Path:
    holder = tempfile.TemporaryDirectory()
    case.addCleanup(holder.cleanup)
    root = Path(holder.name)
    shutil.copytree(source, root, dirs_exist_ok=True)
    return root


def _caddyfile(policy: str = CADDY_POLICY) -> str:
    return (
        ":80 {\n\troute {\n"
        "\t\t# @cross_site_request (a comment is not a matcher)\n"
        "\t\thandle_path /static/* {\n\t\t\tfile_server\n\t\t}\n"
        + policy
        + "\t\treverse_proxy web:8000\n\t}\n}\n"
    )


def _nginx(refusal: str = NGINX_REFUSAL, policy_map: str = NGINX_MAP) -> str:
    return (
        policy_map
        + "server {\n    listen 80;\n    location / {\n"
        + refusal
        + "        proxy_pass http://$web_upstream;\n    }\n}\n"
    )


class ClassifyTests(unittest.TestCase):
    def test_caddy(self):
        self.assertEqual(fm.classify(_caddyfile(), "caddy"), fm.PRESENT)
        self.assertEqual(fm.classify(_caddyfile(""), "caddy"), fm.MISSING)

    def test_a_caddy_matcher_nobody_refuses_is_missing(self):
        unused = CADDY_POLICY.replace("\t\trespond @cross_site_request 403\n", "")
        self.assertEqual(fm.classify(_caddyfile(unused), "caddy"), fm.MISSING)
        commented = CADDY_POLICY.replace("\t\trespond @cross", "\t\t# respond @cross")
        self.assertEqual(fm.classify(_caddyfile(commented), "caddy"), fm.MISSING)

    def test_caddy_refusal_may_carry_a_body_or_use_error(self):
        for refusal in ('respond @cross_site_request "Cross-site request refused" 403',
                        "error @cross_site_request 403"):
            with self.subTest(refusal):
                policy = CADDY_POLICY.replace("respond @cross_site_request 403", refusal)
                self.assertEqual(fm.classify(_caddyfile(policy), "caddy"), fm.PRESENT)

    def test_a_caddy_refusal_with_another_status_is_missing(self):
        policy = CADDY_POLICY.replace("respond @cross_site_request 403", "respond @cross_site_request 4030")
        self.assertEqual(fm.classify(_caddyfile(policy), "caddy"), fm.MISSING)

    def test_the_matcher_may_be_written_another_way(self):
        policy = (
            "\t\t@cross_site_request {\n\t\t\theader Sec-Fetch-Site cross-site\n\t\t\tnot method GET\n\t\t}\n"
            "\t\trespond @cross_site_request 403\n"
        )
        self.assertEqual(fm.classify(_caddyfile(policy), "caddy"), fm.PRESENT)

    def test_nginx(self):
        self.assertEqual(fm.classify(_nginx(), "nginx"), fm.PRESENT)
        self.assertEqual(fm.classify(_nginx(refusal=""), "nginx"), fm.MISSING)
        self.assertEqual(fm.classify(_nginx(policy_map=""), "nginx"), fm.MISSING)
        self.assertEqual(fm.classify(_nginx(refusal="        # " + NGINX_REFUSAL.lstrip()), "nginx"), fm.MISSING)


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.cwd = os.getcwd()
        self.root = _copy(SCHEMA_4, self)
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.cwd)

    def _check(self):
        return DockerComposeLauncher()._check_fetch_metadata()

    def test_a_generated_proxy_without_the_policy_is_a_warning_with_the_fix(self):
        result = self._check()
        self.assertEqual(result["level"], WARN)
        self.assertEqual(result["name"], "fetch-metadata")
        self.assertTrue(result["message"].startswith(".proxy/Caddyfile has no Fetch Metadata"))
        self.assertIn("@cross_site_request", result["fix"])
        self.assertIn("'respond @cross_site_request 403'", result["fix"])
        self.assertIn("'./start.sh restart caddy'", result["fix"])
        self.assertIn("Cross-site requests (Fetch Metadata)", result["fix"])
        self.assertNotIn("nginx", result["fix"])

    def test_the_policy_in_place_is_ok(self):
        Path(".proxy/Caddyfile").write_text(_caddyfile(), encoding="utf-8")
        self.assertEqual(self._check()["level"], OK)

    def test_each_proxy_file_present_is_checked(self):
        Path(".proxy/Caddyfile").write_text(_caddyfile(), encoding="utf-8")
        Path(".proxy/default.conf.template").write_text(_nginx(refusal=""), encoding="utf-8")
        result = self._check()
        self.assertEqual(result["level"], WARN)
        self.assertTrue(result["message"].startswith(".proxy/default.conf.template has no"))
        self.assertIn("'./start.sh restart nginx'", result["fix"])
        self.assertNotIn("Caddyfile", result["fix"])

        Path(".proxy/Caddyfile").write_text(_caddyfile(""), encoding="utf-8")
        result = self._check()
        self.assertTrue(result["message"].startswith(".proxy/Caddyfile and .proxy/default.conf.template have no"))
        self.assertIn("'./start.sh restart caddy or nginx'", result["fix"])

    def test_no_proxy_files_is_ok(self):
        shutil.rmtree(".proxy")
        result = self._check()
        self.assertEqual(result["level"], OK)
        self.assertIn("nothing to check", result["message"])

    def test_an_unreadable_proxy_file_is_a_warning(self):
        Path(".proxy/Caddyfile").write_bytes(b"\xff\xfe not utf-8")
        result = self._check()
        self.assertEqual(result["level"], WARN)
        self.assertIn("Could not read the proxy configuration", result["message"])

    def test_composer_check_reports_it_without_failing(self):
        launcher = DockerComposeLauncher()
        launcher.composer_version = "1.2.6"
        args = SimpleNamespace(file=None, dev=False, fix=False, yes=False, deep=False, deep_service="web",
                               deep_command="python manage.py dlux_doctor", json=False)
        with (
            patch.object(launcher, "run_command", return_value=(True, "27.0\n", "")),
            patch.object(launcher, "discover_services",
                         side_effect=lambda silent=False: setattr(launcher, "services", ["web", "caddy"]) or True),
            patch.object(launcher, "plaintext_env_candidates", return_value=["/x/.env"]),
            patch.object(launcher, "parse_env_file", return_value={"SECRET_KEY": "x"}),
            patch.object(launcher, "required_compose_vars", return_value=set()),
            patch.object(launcher, "run_docker_compose", return_value=(True, "1.2.6\n", "")),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            results, _fixed = launcher.collect_checkup(args)
        finding = next(result for result in results if result["name"] == "fetch-metadata")
        self.assertEqual(finding["level"], WARN)


class DjangoLuxScaffoldTests(unittest.TestCase):
    """The DjangoLux checkout alongside (or ``DLUX_CHECKOUT``) passes, and fails without its block."""

    def setUp(self):
        caddyfile = DLUX_PROXY / "Caddyfile.tmpl"
        if not caddyfile.is_file() or "@cross_site_request" not in caddyfile.read_text(encoding="utf-8"):
            self.skipTest(f"No DjangoLux checkout with the Fetch Metadata policy at {DLUX_CHECKOUT}")
        self.cwd = os.getcwd()
        self.root = _copy(SCHEMA_4, self)
        shutil.copy(DLUX_PROXY / "Caddyfile.tmpl", self.root / ".proxy" / "Caddyfile")
        shutil.copy(DLUX_PROXY / "default.conf.template.tmpl", self.root / ".proxy" / "default.conf.template")
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.cwd)

    def _check(self):
        return DockerComposeLauncher()._check_fetch_metadata()

    def test_a_fresh_scaffold_passes(self):
        self.assertEqual(self._check()["level"], OK)

    def test_the_scaffold_without_its_caddy_block_fails(self):
        path = Path(".proxy/Caddyfile")
        text, count = re.subn(
            r"\t\t@cross_site_request expression .*?respond @cross_site_request 403\n",
            "", path.read_text(encoding="utf-8"), flags=re.S,
        )
        self.assertEqual(count, 1)
        path.write_text(text, encoding="utf-8")
        result = self._check()
        self.assertEqual(result["level"], WARN)
        self.assertTrue(result["message"].startswith(".proxy/Caddyfile has no"))

    def test_the_scaffold_without_its_nginx_refusals_fails(self):
        path = Path(".proxy/default.conf.template")
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count("if ($cross_site_request) { return 403; }"), 3)
        path.write_text(text.replace("if ($cross_site_request) { return 403; }", ""), encoding="utf-8")
        result = self._check()
        self.assertEqual(result["level"], WARN)
        self.assertTrue(result["message"].startswith(".proxy/default.conf.template has no"))


if __name__ == "__main__":
    unittest.main()
