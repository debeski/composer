"""DjangoLux's scaffold and Composer's `check --fix` describe the same stack.

Runs `scripts/scaffold_alignment.py` against the sibling DjangoLux checkout
(`DLUX_CHECKOUT`, or `../pkg-django-lux/main` beside this repository). Skipped
where that checkout, DjangoLux's dependencies or Docker are missing, as in CI.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DLUX = Path(os.environ.get("DLUX_CHECKOUT") or ROOT.parents[1] / "pkg-django-lux" / "main")


def _available() -> str:
    if not (DLUX / "dlux" / "scaffold").is_dir():
        return f"no DjangoLux checkout at {DLUX}"
    if importlib.util.find_spec("django") is None:
        return "DjangoLux's dependencies are not installed in this Python"
    if not shutil.which("docker"):
        return "docker is not installed"
    if subprocess.run(["docker", "version"], capture_output=True).returncode != 0:
        return "the Docker daemon is not reachable"
    return ""


def _load_gate():
    spec = importlib.util.spec_from_file_location("scaffold_alignment", ROOT / "scripts" / "scaffold_alignment.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipIf(_available(), _available())
class ScaffoldAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.gate = _load_gate()
        holder = tempfile.TemporaryDirectory(prefix="scaffold-alignment-")
        self.addCleanup(holder.cleanup)
        self.project = self.gate.generate(DLUX, sys.executable, Path(holder.name))

    def test_a_fresh_project_needs_nothing_from_check_fix(self):
        self.assertEqual(self.gate.verdict(self.gate.inspect(self.project)), [])

    def test_a_scaffold_that_drifts_from_composer_is_caught(self):
        compose = self.project / "compose.yml"
        text = compose.read_text(encoding="utf-8")
        self.assertIn('DLUX_STACK_SCHEMA: "5"', text)
        compose.write_text(text.replace('DLUX_STACK_SCHEMA: "5"', 'DLUX_STACK_SCHEMA: "3"'), encoding="utf-8")

        problems = self.gate.verdict(self.gate.inspect(self.project))

        self.assertTrue(any("stack-schema" in problem for problem in problems), problems)

    def test_a_fresh_project_keeps_redis_snapshots(self):
        results = {r["name"]: r for r in self.gate.inspect(self.project)["results"]}

        self.assertEqual(results["redis-persistence"]["level"], "ok", results.get("redis-persistence"))

    def test_an_append_only_redis_is_caught(self):
        compose = self.project / "compose.yml"
        text = compose.read_text(encoding="utf-8")
        old = '"--appendonly", "no", "--save", "60", "1"'
        self.assertIn(old, text)
        compose.write_text(text.replace(old, '"--appendonly", "yes", "--appendfsync", "everysec"'), encoding="utf-8")

        problems = self.gate.verdict(self.gate.inspect(self.project))

        self.assertTrue(any(p.startswith("WARN redis-persistence") for p in problems), problems)
        self.assertTrue(any(p.startswith("REPAIR redis-persistence") for p in problems), problems)

    def test_a_stale_wrapper_is_caught(self):
        wrapper = self.project / "start.sh"
        wrapper.write_text(wrapper.read_text(encoding="utf-8") + "# stale\n", encoding="utf-8")

        problems = self.gate.verdict(self.gate.inspect(self.project))

        self.assertTrue(any(problem.startswith("WRAPPER start.sh") for problem in problems), problems)


if __name__ == "__main__":
    unittest.main()
