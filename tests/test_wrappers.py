import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from composer import wrappers

REPO_ROOT = Path(__file__).resolve().parents[1]


class WrapperManifestTests(unittest.TestCase):
    """The shipped files must agree with the manifest that describes them."""

    def test_every_wrapper_carries_a_marker(self):
        for name in wrappers.WRAPPER_NAMES:
            with self.subTest(name=name):
                text = (REPO_ROOT / name).read_text(encoding="utf-8")
                self.assertIsNotNone(
                    wrappers.read_marker(text),
                    f"{name} has no '# composer-wrapper: N' marker",
                )

    def test_history_records_the_current_bytes(self):
        document = json.loads((REPO_ROOT / wrappers.HISTORY_NAME).read_text(encoding="utf-8"))
        history = document["history"]
        for name in wrappers.WRAPPER_NAMES:
            with self.subTest(name=name):
                payload = (REPO_ROOT / name).read_bytes()
                version = str(wrappers.read_marker(payload.decode("utf-8")))
                digest = hashlib.sha256(payload).hexdigest()
                self.assertIn(
                    version,
                    history[name],
                    f"{name} is at version {version} with no entry in {wrappers.HISTORY_NAME}",
                )
                self.assertEqual(
                    history[name][version],
                    digest,
                    f"{name} changed without a version bump; if that was intentional, "
                    f"bump the marker and record {digest}",
                )

    def test_both_wrappers_share_one_version(self):
        markers = {
            name: wrappers.read_marker((REPO_ROOT / name).read_text(encoding="utf-8"))
            for name in wrappers.WRAPPER_NAMES
        }
        self.assertEqual(len(set(markers.values())), 1, f"wrapper versions disagree: {markers}")


class WrapperInspectionTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.baked = self.root / "baked"
        self.project = self.root / "project"
        self.baked.mkdir()
        self.project.mkdir()
        self.reference = "#!/bin/bash\n# composer-wrapper: 4\necho current\n"
        (self.baked / "start.sh").write_text(self.reference, encoding="utf-8")
        (self.baked / "start.ps1").write_text("# composer-wrapper: 4\nWrite-Host current\n", encoding="utf-8")
        self.previous = "#!/bin/bash\n# composer-wrapper: 3\necho previous\n"
        (self.baked / wrappers.HISTORY_NAME).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "history": {
                        "start.sh": {
                            "3": hashlib.sha256(self.previous.encode()).hexdigest(),
                            "4": hashlib.sha256(self.reference.encode()).hexdigest(),
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self.addCleanup(self._temp.cleanup)

    def _inspect(self):
        found = wrappers.inspect_wrappers(str(self.project), override=str(self.baked))
        return {entry["name"]: entry for entry in found}

    def test_matching_bytes_are_current(self):
        (self.project / "start.sh").write_text(self.reference, encoding="utf-8")
        self.assertEqual(self._inspect()["start.sh"]["status"], wrappers.CURRENT)

    def test_pristine_older_version_is_stale(self):
        (self.project / "start.sh").write_text(self.previous, encoding="utf-8")
        entry = self._inspect()["start.sh"]
        self.assertEqual(entry["status"], wrappers.STALE)
        self.assertEqual(entry["version"], 3)
        self.assertEqual(entry["baked_version"], 4)

    def test_edited_copy_of_an_old_version_is_modified_not_stale(self):
        (self.project / "start.sh").write_text(self.previous + "echo mine\n", encoding="utf-8")
        self.assertEqual(self._inspect()["start.sh"]["status"], wrappers.MODIFIED)

    def test_same_version_different_bytes_is_modified(self):
        (self.project / "start.sh").write_text(self.reference + "echo mine\n", encoding="utf-8")
        self.assertEqual(self._inspect()["start.sh"]["status"], wrappers.MODIFIED)

    def test_marker_absent_is_unversioned(self):
        (self.project / "start.sh").write_text("#!/bin/bash\necho ancient\n", encoding="utf-8")
        self.assertEqual(self._inspect()["start.sh"]["status"], wrappers.UNVERSIONED)

    def test_newer_marker_reports_the_image_as_behind(self):
        (self.project / "start.sh").write_text(
            "#!/bin/bash\n# composer-wrapper: 9\necho future\n", encoding="utf-8"
        )
        entry = self._inspect()["start.sh"]
        self.assertEqual(entry["status"], wrappers.AHEAD)
        self.assertNotIn(wrappers.AHEAD, wrappers.FIXABLE)

    def test_absent_wrapper_is_missing_when_the_other_exists(self):
        (self.project / "start.sh").write_text(self.reference, encoding="utf-8")
        self.assertEqual(self._inspect()["start.ps1"]["status"], wrappers.MISSING)

    def test_directory_with_no_wrappers_is_not_reported_on(self):
        self.assertEqual(wrappers.inspect_wrappers(str(self.project), override=str(self.baked)), [])

    def test_missing_reference_directory_skips_the_check(self):
        (self.project / "start.sh").write_text(self.reference, encoding="utf-8")
        self.assertEqual(
            wrappers.inspect_wrappers(str(self.project), override=str(self.root / "absent")), []
        )


class WrapperInstallTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.baked = self.root / "baked"
        self.project = self.root / "project"
        self.baked.mkdir()
        self.project.mkdir()
        (self.baked / "start.sh").write_text("# composer-wrapper: 4\necho new\n", encoding="utf-8")
        self.addCleanup(self._temp.cleanup)

    def test_replacement_archives_the_original_and_keeps_mode(self):
        target = self.project / "start.sh"
        target.write_text("# composer-wrapper: 3\necho old\n", encoding="utf-8")
        target.chmod(0o755)
        archive = self.project / ".xpose" / "composer-check" / "stamp"

        wrappers.install_wrapper(self.project, "start.sh", self.baked, archive)

        self.assertIn("echo new", target.read_text(encoding="utf-8"))
        self.assertIn("echo old", (archive / "start.sh").read_text(encoding="utf-8"))
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)

    def test_replacement_swaps_the_inode_so_a_running_shell_is_unaffected(self):
        target = self.project / "start.sh"
        target.write_text("# composer-wrapper: 3\necho old\n", encoding="utf-8")
        before = target.stat().st_ino

        with open(target, "rb") as still_open:
            wrappers.install_wrapper(self.project, "start.sh", self.baked, None)
            # What an already-executing bash would keep reading.
            self.assertIn(b"echo old", still_open.read())

        self.assertNotEqual(target.stat().st_ino, before)

    def test_replacement_repairs_a_non_executable_shell_wrapper(self):
        target = self.project / "start.sh"
        target.write_text("# composer-wrapper: 3\necho old\n", encoding="utf-8")
        target.chmod(0o644)

        wrappers.install_wrapper(self.project, "start.sh", self.baked, None)

        self.assertTrue(os.access(target, os.X_OK))

    def test_a_newly_written_shell_wrapper_is_executable(self):
        wrappers.install_wrapper(self.project, "start.sh", self.baked, None)
        self.assertTrue(os.access(self.project / "start.sh", os.X_OK))

    def test_no_staging_file_is_left_behind(self):
        wrappers.install_wrapper(self.project, "start.sh", self.baked, None)
        self.assertEqual(
            sorted(p.name for p in self.project.iterdir()),
            ["start.sh"],
        )


if __name__ == "__main__":
    unittest.main()
