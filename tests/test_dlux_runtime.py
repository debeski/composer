"""Composer's writer for the DjangoLux runtime volume.

The layout is a cross-repo contract with dlux/updater/runtime.py, so these tests
pin the shape as much as the behaviour.
"""

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from composer.dlux_runtime import DluxRuntime, DluxRuntimeError, normalize_version


def _stage_source(root: Path, version: str, *, manifest_version=None, inline_safe=True) -> Path:
    """Build an unpacked release directory the way a wheel would unpack."""
    src = root / f"unpacked-{version}"
    (src / "dlux").mkdir(parents=True)
    manifest = {
        "schema_version": 1,
        "version": manifest_version or version,
        "inline_safe": inline_safe,
    }
    (src / "dlux" / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return src


def _schema_two_source(root: Path, version: str, *, effect="none", inline="allowed") -> Path:
    """A real DjangoLux 1.8.7-shaped manifest: no `inline_safe` key at all."""
    src = root / f"unpacked-{version}"
    (src / "dlux").mkdir(parents=True)
    manifest = {
        "schema_version": 2,
        "version": version,
        "requires": {"updater_schema": ">=1", "baked_image": ">=1.2.7"},
        "migrations": {"effect": effect, "rollback_compatible": True, "downtime": "none"},
        "install": {"inline": inline},
        "rollback": {"supported": True},
    }
    (src / "dlux" / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return src


class VersionValidationTests(unittest.TestCase):
    def test_accepts_release_versions(self):
        for value in ("1.8.0", "1.8.0rc1", "2.0", "1.8.0.post1"):
            with self.subTest(value=value):
                self.assertEqual(normalize_version(value), value)

    def test_rejects_anything_that_could_escape_releases(self):
        for value in ("", "..", "../etc", "1.8.0/../..", "/abs/path", "1.8.0;rm -rf /", None):
            with self.subTest(value=value):
                with self.assertRaises(DluxRuntimeError):
                    normalize_version(value)


class StagingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runtime = DluxRuntime(self.root / "dlux-runtime")
        self.addCleanup(self._tmp.cleanup)

    def test_stage_then_verify_a_release(self):
        src = _stage_source(self.root, "1.8.0")
        target = self.runtime.stage_release("1.8.0", src)

        self.assertTrue((target / "dlux" / "release-manifest.json").exists())
        self.assertEqual(self.runtime.staged_versions(), ["1.8.0"])
        self.assertEqual(self.runtime.verify_release("1.8.0")["version"], "1.8.0")

    def test_staged_versions_are_ordered_by_version(self):
        """As strings, "1.8.10" sorts below "1.8.9" — and the rollback target and
        the prune are both taken from the end of this list."""
        for version in ("1.8.9", "1.8.10", "1.9.0", "1.10.0"):
            self.runtime.stage_release(version, _stage_source(self.root, version))
        self.assertEqual(
            self.runtime.staged_versions(), ["1.8.9", "1.8.10", "1.9.0", "1.10.0"]
        )

    def test_staging_rejects_a_directory_without_the_package(self):
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaises(DluxRuntimeError):
            self.runtime.stage_release("1.8.0", empty)

    def test_verify_accepts_a_schema_two_release(self):
        """Schema 2 derives inline safety; it never carries an `inline_safe` key.

        Reading that key directly refused every current release *after* the wheel
        had been fetched, verified and staged — the swap failed at the last step.
        """
        self.runtime.stage_release("1.8.7", _schema_two_source(self.root, "1.8.7"))
        self.assertTrue(self.runtime.verify_release("1.8.7")["inline_safe"])
        self.assertEqual(self.runtime.activate("1.8.7"), {})

    def test_verify_rejects_a_schema_two_release_that_forbids_inline_install(self):
        self.runtime.stage_release(
            "1.8.7", _schema_two_source(self.root, "1.8.7", inline="forbidden")
        )
        with self.assertRaises(DluxRuntimeError) as ctx:
            self.runtime.verify_release("1.8.7")
        self.assertIn("inline", str(ctx.exception))

    def test_verify_rejects_a_release_whose_manifest_disagrees(self):
        """The artifact must be what was asked for — caught before activation."""
        src = _stage_source(self.root, "1.8.0", manifest_version="9.9.9")
        self.runtime.stage_release("1.8.0", src)

        with self.assertRaises(DluxRuntimeError) as ctx:
            self.runtime.verify_release("1.8.0")
        self.assertIn("9.9.9", str(ctx.exception))

    def test_quarantine_moves_a_release_out_of_reach(self):
        self.runtime.stage_release("1.8.0", _stage_source(self.root, "1.8.0"))
        moved = self.runtime.quarantine("1.8.0", reason="health check failed")

        self.assertEqual(self.runtime.staged_versions(), [])
        self.assertIn("health check failed", (moved / "quarantine-reason.txt").read_text())


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runtime = DluxRuntime(self.root / "dlux-runtime")
        self.addCleanup(self._tmp.cleanup)

    def test_no_active_file_means_the_image_release_is_in_force(self):
        self.assertEqual(self.runtime.read_active(), {})

    def test_activate_writes_the_pointer_and_bumps_the_generation(self):
        self.runtime.stage_release("1.8.0", _stage_source(self.root, "1.8.0"))
        before = self.runtime.read_generation()

        previous = self.runtime.activate("1.8.0")
        active = self.runtime.read_active()

        self.assertEqual(previous, {})
        self.assertEqual(active["version"], "1.8.0")
        self.assertEqual(active["source"], "volume")
        self.assertEqual(active["path"], str(self.runtime.release_path("1.8.0")))
        self.assertGreater(active["generation"], before)

    def test_activation_refuses_a_release_that_is_not_staged(self):
        with self.assertRaises(DluxRuntimeError):
            self.runtime.activate("1.8.0")
        self.assertFalse(self.runtime.active_file.exists())

    def test_activation_refuses_a_staged_release_that_fails_verification(self):
        """Verification runs before the pointer moves, so a bad artifact is inert."""
        self.runtime.stage_release("1.8.0", _stage_source(self.root, "1.8.0", manifest_version="0.1"))

        with self.assertRaises(DluxRuntimeError):
            self.runtime.activate("1.8.0")
        self.assertEqual(self.runtime.read_active(), {})

    def test_rollback_restores_the_previous_release(self):
        self.runtime.stage_release("1.7.1", _stage_source(self.root, "1.7.1"))
        self.runtime.stage_release("1.8.0", _stage_source(self.root, "1.8.0"))
        self.runtime.activate("1.7.1")

        previous = self.runtime.activate("1.8.0")
        self.assertEqual(self.runtime.read_active()["version"], "1.8.0")

        self.runtime.restore(previous)
        restored = self.runtime.read_active()

        self.assertEqual(restored["version"], "1.7.1")
        self.assertGreater(restored["generation"], previous["generation"])

    def test_rollback_from_the_first_ever_update_clears_the_pointer(self):
        """Rolling back the first volume release returns to the image copy."""
        self.runtime.stage_release("1.8.0", _stage_source(self.root, "1.8.0"))
        previous = self.runtime.activate("1.8.0")

        self.runtime.restore(previous)

        self.assertEqual(self.runtime.read_active(), {})
        self.assertFalse(self.runtime.active_file.exists())

    def test_a_corrupt_pointer_is_reported_not_silently_ignored(self):
        self.runtime.state_dir.mkdir(parents=True, exist_ok=True)
        self.runtime.active_file.write_text("{not json", encoding="utf-8")
        with self.assertRaises(DluxRuntimeError):
            self.runtime.read_active()

    def test_a_pointer_to_a_deleted_release_is_reported(self):
        self.runtime.stage_release("1.8.0", _stage_source(self.root, "1.8.0"))
        self.runtime.activate("1.8.0")
        self.runtime.quarantine("1.8.0")

        with self.assertRaises(DluxRuntimeError):
            self.runtime.read_active()

    def test_active_json_is_written_atomically(self):
        """A half-written pointer would send the next start to a missing release."""
        self.runtime.stage_release("1.8.0", _stage_source(self.root, "1.8.0"))
        self.runtime.activate("1.8.0")

        leftovers = [p.name for p in self.runtime.state_dir.iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [])
        self.assertEqual(json.loads(self.runtime.active_file.read_text())["version"], "1.8.0")


def _dlux_runtime_store():
    """dlux's own reader, if a checkout/install is importable. Optional by design.

    Composer must not depend on DjangoLux. When both are present — a dev
    checkout, or an image with dlux installed — this proves the two
    implementations agree on the volume rather than merely looking similar.
    """
    import importlib

    for candidate in (Path(__file__).resolve().parents[2] / "pkg-django-lux",):
        if (candidate / "dlux" / "updater" / "runtime.py").exists():
            sys.path.insert(0, str(candidate))
            break
    try:
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dlux.tests.settings")
        import django

        django.setup()
        return importlib.import_module("dlux.updater.runtime").RuntimeStore
    except Exception:
        return None


class ContractInteropTests(unittest.TestCase):
    """Composer writes the volume; DjangoLux reads it. Both must agree."""

    def setUp(self):
        self.store_cls = _dlux_runtime_store()
        if self.store_cls is None:
            self.skipTest("DjangoLux is not importable here; interop check skipped")
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runtime = DluxRuntime(self.root / "dlux-runtime")
        self.addCleanup(self._tmp.cleanup)

    def test_dlux_accepts_an_activation_composer_wrote(self):
        self.runtime.stage_release("1.8.0", _stage_source(self.root, "1.8.0"))
        self.runtime.activate("1.8.0")

        active = self.store_cls(self.runtime.root).read_active(baked_version="1.7.1")

        self.assertEqual(active["version"], "1.8.0")
        self.assertEqual(active["source"], "volume")
        self.assertEqual(active["path"], str(self.runtime.release_path("1.8.0")))

    def test_dlux_falls_back_to_the_image_after_a_composer_rollback(self):
        self.runtime.stage_release("1.8.0", _stage_source(self.root, "1.8.0"))
        previous = self.runtime.activate("1.8.0")
        self.runtime.restore(previous)

        active = self.store_cls(self.runtime.root).read_active(baked_version="1.7.1")

        self.assertEqual(active["version"], "1.7.1")
        self.assertEqual(active["source"], "image")


class PublishedContractTests(unittest.TestCase):
    """Composer's writer must satisfy the contract DjangoLux publishes.

    dlux/runtime_contract.json is fetched in production through
    `manage.py dlux_runtime_contract`; here it is read from the sibling checkout
    when one is present, so the two implementations are checked against the same
    document rather than against each other's habits.
    """

    def setUp(self):
        path = (Path(__file__).resolve().parents[2] / "pkg-django-lux"
                / "dlux" / "runtime_contract.json")
        if not path.exists():
            self.skipTest("DjangoLux checkout not present; contract check skipped")
        self.contract = json.loads(path.read_text(encoding="utf-8"))
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runtime = DluxRuntime(self.root / "dlux-runtime")
        self.addCleanup(self._tmp.cleanup)

    def test_composer_knows_every_directory_the_contract_requires(self):
        for name, spec in self.contract["directories"].items():
            if not spec.get("required"):
                continue
            attribute = "state_dir" if name == "state" else name
            with self.subTest(directory=name):
                self.assertTrue(hasattr(self.runtime, attribute))
                self.assertEqual(getattr(self.runtime, attribute).name, name)

    def test_the_pointer_composer_writes_satisfies_the_contract(self):
        self.runtime.stage_release("1.8.0", _stage_source(self.root, "1.8.0"))
        self.runtime.activate("1.8.0")

        written = json.loads(self.runtime.active_file.read_text(encoding="utf-8"))
        spec = self.contract["active_json"]

        self.assertEqual(set(spec["required_keys"]) - set(written), set())
        self.assertIn(written["source"], spec["source_values"])
        self.assertTrue(written["path"], "source 'volume' requires a path")
        self.assertGreaterEqual(written["generation"], 0)

    def test_composer_only_writes_files_the_contract_assigns_to_it(self):
        self.runtime.stage_release("1.8.0", _stage_source(self.root, "1.8.0"))
        self.runtime.activate("1.8.0")

        owned = {
            name for name, spec in self.contract["state_files"].items()
            if spec.get("writer") in {"composer", "both"}
        }
        written = {p.name for p in self.runtime.state_dir.iterdir()}

        self.assertEqual(written - owned, set(),
                         "composer wrote a state file the contract does not assign to it")

    def test_the_contract_schema_version_is_the_one_composer_implements(self):
        from composer.dlux_runtime import CONTRACT_SCHEMA_VERSION

        self.assertEqual(self.contract["schema_version"], CONTRACT_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
