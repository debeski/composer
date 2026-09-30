import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from composer.relay import operation_digest
from composer.relay_cli import run_relay

PAGE = {"name": "finance.rates_page", "url": "https://rates.example.com/exchange/",
        "response": {"type": "text", "max_bytes": 4096}}
OTHER = {"name": "finance.other", "url": "https://other.example.com/x", "auth": {"placement": "bearer"},
         "response": {"type": "json", "fields": ["a.b"]}}


def run(action, directory):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = run_relay(action, ["--dir", str(directory)])
    return code, out.getvalue(), err.getvalue()


class RelayCliTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("composer.relay_cli.BUILTIN_OPERATIONS", [])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dir = Path(tempfile.mkdtemp())

    def declare(self, *operations):
        (self.dir / "operations.json").write_text(json.dumps({"schema_version": 1, "operations": list(operations)}))

    def lock(self):
        return json.loads((self.dir / "operations.lock").read_text())["operations"]

    def test_approve_pins_digests_and_shows_the_hosts(self):
        self.declare(PAGE, OTHER)
        code, out, _ = run("approve", self.dir)
        self.assertEqual(code, 0)
        self.assertIn("+ new      finance.rates_page  https://rates.example.com/exchange/", out)
        self.assertIn("secret via bearer", out)
        self.assertEqual(self.lock(), {PAGE["name"]: operation_digest(PAGE), OTHER["name"]: operation_digest(OTHER)})
        code, out, _ = run("approve", self.dir)
        self.assertIn("Already approved", out)

    def test_a_changed_host_is_shown_as_changed(self):
        self.declare(PAGE)
        run("approve", self.dir)
        moved = {**PAGE, "url": "https://elsewhere.example.net/exchange/"}
        self.declare(moved)
        code, out, _ = run("list", self.dir)
        self.assertIn("CHANGED", out)
        code, out, _ = run("approve", self.dir)
        self.assertIn("~ changed  finance.rates_page  https://elsewhere.example.net/exchange/", out)
        self.assertEqual(self.lock()[PAGE["name"]], operation_digest(moved))

    def test_removed_operations_leave_the_lock(self):
        self.declare(PAGE, OTHER)
        run("approve", self.dir)
        self.declare(PAGE)
        _, out, _ = run("approve", self.dir)
        self.assertIn("- removed  finance.other", out)
        self.assertEqual(list(self.lock()), [PAGE["name"]])

    def test_invalid_declarations_approve_nothing(self):
        self.declare(PAGE, {**OTHER, "url": "http://plain.example.com/x"})
        code, _, err = run("approve", self.dir)
        self.assertEqual(code, 2)
        self.assertIn("https", err)
        self.assertFalse((self.dir / "operations.lock").exists())

    def test_list_reports_state(self):
        self.declare(PAGE, OTHER)
        (self.dir / "operations.lock").write_text(json.dumps(
            {"schema_version": 1, "operations": {PAGE["name"]: operation_digest(PAGE), "finance.gone": "sha256:x"}}))
        code, out, _ = run("list", self.dir)
        self.assertEqual(code, 0)
        self.assertRegex(out, r"approved\s+finance.rates_page")
        self.assertRegex(out, r"unapproved\s+finance.other")
        self.assertRegex(out, r"stale-lock\s+finance.gone")

    def test_nothing_declared(self):
        code, out, _ = run("list", self.dir)
        self.assertEqual(code, 0)
        self.assertIn("No operations", out)
        code, out, _ = run("approve", self.dir)
        self.assertIn("Nothing to approve", out)


if __name__ == "__main__":
    unittest.main()
