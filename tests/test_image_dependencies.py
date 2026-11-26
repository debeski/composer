"""Composer's own modules import only what the Composer image installs.

The unit suite runs in a developer environment with extra packages (PyYAML,
Django), so a stray import passes every test and then crashes Composer at
start-up inside the image. The allowed set is read from the Dockerfile's
`pip install` line, so it cannot drift from what the image really ships.
"""

import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMPORT_NAMES = {"pypi-attestations": "pypi_attestations"}


def image_packages():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    line = next(line for line in dockerfile.splitlines() if "pip install" in line)
    names = re.findall(r'"([A-Za-z0-9_.-]+)[<>=!~]', line)
    return {IMPORT_NAMES.get(name, name.replace("-", "_")) for name in names}


class ImageDependencyTests(unittest.TestCase):
    def test_the_dockerfile_names_the_packages_composer_uses(self):
        self.assertEqual(image_packages(), {"pypi_attestations", "sigstore", "packaging", "cryptography"})

    def test_composer_imports_only_the_standard_library_and_image_packages(self):
        allowed = set(sys.stdlib_module_names) | image_packages() | {"composer"}
        found = []
        for path in sorted((ROOT / "composer").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module.split(".")[0]]
                else:
                    continue
                found += [f"{path.relative_to(ROOT)}:{node.lineno} {name}" for name in names if name not in allowed]
        self.assertEqual(found, [], "imports the Composer image does not install")


if __name__ == "__main__":
    unittest.main()
