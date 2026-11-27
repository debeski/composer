"""Break-it round 2: recovering the runtime volume and trusting what lands on it.

R2-002 an unusable ``active.json`` has an operator route out; R2-011 only the
``dlux`` package is unpacked; R2-012 ``rollback --version`` is honoured; R2-013 a
release the image is below its floor is refused; R2-014 and R2-059 nothing
unpacked is left behind; R2-058 the rollback schema rule; R2-112 a self-restarting
task service is not restarted twice.
"""

import io
import json
import os
import sys
import unittest
import zipfile
from collections import namedtuple
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from composer import dlux_package_cli as cli
from composer import dlux_release_source as source
from composer.dlux_package_update import (
    apply_package_update,
    image_floor_problem,
    rollback_package_update,
)
from composer.dlux_release_source import ReleaseSourceError
from composer.dlux_runtime import DluxRuntime, UnusablePointer

from tests.test_dlux_package_update import _FakeSource, _Ops


def _manifest(version, **extra):
    return {"schema_version": 1, "version": version, "inline_safe": True, **extra}


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.runtime = DluxRuntime(self.root / "dlux-runtime")
        self.runtime.state_dir.mkdir(parents=True)
        self.source = _FakeSource(self.root)

    def stage(self, version, **manifest):
        package = self.runtime.release_path(version) / "dlux"
        package.mkdir(parents=True)
        (package / "release-manifest.json").write_text(json.dumps(_manifest(version, **manifest)), encoding="utf-8")

    def activate(self, version):
        self.runtime.write_active(version, source="volume", generation=1)


class UnusablePointerTests(_Base):
    """R2-002: `null` or `[]` in active.json, and nothing an operator ran recovered it."""

    def test_every_non_object_pointer_is_an_unusable_pointer_naming_the_way_out(self):
        for content in ("null", "[]", '"1.17.0"', "{not json"):
            with self.subTest(content=content):
                self.runtime.active_file.write_text(content, encoding="utf-8")
                with self.assertRaises(UnusablePointer) as caught:
                    self.runtime.read_active()
                self.assertIn("./start.sh dlux rollback", str(caught.exception))

    def test_a_fifo_pointer_is_refused_without_blocking(self):
        os.mkfifo(self.runtime.active_file)
        with self.assertRaises(UnusablePointer):
            self.runtime.read_active()

    def test_rollback_sets_the_pointer_aside_and_returns_to_the_image(self):
        self.stage("1.17.0")
        self.runtime.active_file.write_text("null", encoding="utf-8")
        ops = _Ops()
        result = rollback_package_update(self.runtime, restart=ops.restart, health_check=ops.health)
        self.assertTrue(result.ok, result.message)
        self.assertIn("pointer-set-aside", result.steps)
        self.assertFalse(self.runtime.active_file.exists())
        kept = list(self.runtime.state_dir.glob("active.json.unusable-*"))
        self.assertEqual(len(kept), 1, "the unusable file is kept, never deleted")
        self.assertEqual(kept[0].read_text(encoding="utf-8"), "null")
        self.assertEqual(self.runtime.read_generation(), 1)
        self.assertEqual(ops.restart_calls, 1)

    def test_check_reports_the_pointer_instead_of_a_traceback(self):
        self.runtime.active_file.write_text("[]", encoding="utf-8")
        args = cli.parse_dlux_update_args(["--runtime-root", str(self.runtime.root)], action="check")
        payload = {"available": True, "version": "1.17.0", "inline_safe": True, "channel": "stable",
                   "quarantined": [], "error": "", "reason": ""}
        stderr = io.StringIO()
        with mock.patch.object(cli, "build_availability_payload", return_value=payload), \
                redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            code = cli.run_availability_check(args, self.runtime)
        self.assertEqual(code, 1)
        self.assertIn("not a JSON object", stderr.getvalue())
        self.assertIn("./start.sh dlux rollback", stderr.getvalue())


def _wheel(path, members):
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


class UnpackTests(_Base):
    """R2-011, R2-014, R2-059: what an unpack may put on the interpreter path, and its leftovers."""

    BASE = {
        "dlux/__init__.py": "",
        "dlux/release-manifest.json": json.dumps(_manifest("1.17.1")),
        "django_lux-1.17.1.dist-info/METADATA": "Name: django-lux\n",
    }

    def test_only_the_dlux_package_and_its_metadata_are_unpacked(self):
        target = source.unpack(_wheel(self.root / "ok.whl", self.BASE), self.root / "staging" / "unpacked")
        self.assertEqual(sorted(entry.name for entry in target.iterdir()), ["django_lux-1.17.1.dist-info", "dlux"])

    def test_any_other_top_level_entry_is_refused_and_nothing_is_left(self):
        for extra in ("sitecustomize.py", "c2evil.pth", "django/__init__.py", "json.py",
                      "other_pkg-1.0.dist-info/METADATA"):
            with self.subTest(extra=extra):
                wheel = _wheel(self.root / "evil.whl", {**self.BASE, extra: "print('ran')"})
                destination = self.root / "staging" / "unpacked"
                with self.assertRaisesRegex(ReleaseSourceError, "outside the dlux package"):
                    source.unpack(wheel, destination)
                self.assertFalse(destination.exists())

    def test_an_oversized_release_is_refused_before_it_is_written(self):
        wheel = _wheel(self.root / "big.whl", self.BASE)
        destination = self.root / "staging" / "unpacked"
        with mock.patch.object(source, "MAX_UNPACKED_BYTES", 10), \
                self.assertRaisesRegex(ReleaseSourceError, "unpacks to"):
            source.unpack(wheel, destination)
        self.assertFalse(destination.exists())

    def test_a_volume_without_room_is_refused_before_it_fills(self):
        wheel = _wheel(self.root / "ok.whl", self.BASE)
        destination = self.root / "staging" / "unpacked"
        usage = namedtuple("usage", "total used free")(12 << 20, 11 << 20, 1 << 20)
        with mock.patch.object(source.shutil, "disk_usage", return_value=usage), \
                self.assertRaisesRegex(ReleaseSourceError, "1 MB free"):
            source.unpack(wheel, destination)
        self.assertFalse(destination.exists())

    def test_a_failure_while_writing_leaves_nothing_behind(self):
        wheel = _wheel(self.root / "ok.whl", self.BASE)
        destination = self.root / "staging" / "unpacked"
        with mock.patch.object(zipfile.ZipFile, "extractall", side_effect=OSError(28, "No space left on device")), \
                self.assertRaises(OSError):
            source.unpack(wheel, destination)
        self.assertFalse(destination.exists())

    def test_the_active_release_is_not_unpacked_again(self):
        self.stage("1.17.1")
        self.activate("1.17.1")
        ops = _Ops()
        result = apply_package_update(self.runtime, restart=ops.restart, health_check=ops.health,
                                      target_version="1.17.1", source=self.source)
        self.assertTrue(result.ok)
        self.assertIn("already-active", result.steps)
        self.assertEqual(self.source.calls, [], "the pinned active release is never fetched or unpacked")

    def test_an_unpinned_update_to_the_active_release_drops_its_unpacked_copy(self):
        self.stage("1.8.0")
        self.activate("1.8.0")
        ops = _Ops()
        result = apply_package_update(self.runtime, restart=ops.restart, health_check=ops.health, source=self.source)
        self.assertIn("already-active", result.steps)
        self.assertFalse((self.root / "unpacked-1.8.0").exists())


class _FloorSource(_FakeSource):
    def __init__(self, root, floor):
        super().__init__(root, version="1.17.0b101")
        self.floor = floor

    def obtain(self, target_version="", **kwargs):
        candidate, unpacked = super().obtain(target_version, **kwargs)
        path = unpacked / "dlux" / "release-manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["requires"] = {"baked_image": self.floor}
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return candidate, unpacked


class ImageFloorTests(_Base):
    """R2-013: Composer activated a release the image could not run and said it succeeded."""

    def test_a_release_above_the_image_is_refused_before_anything_changes(self):
        floor_source = _FloorSource(self.root, ">=9.0")
        ops = _Ops()
        backups = []
        result = apply_package_update(
            self.runtime, restart=ops.restart, health_check=ops.health, source=floor_source,
            baked_version=lambda: "1.17.0b2", before_swap=lambda: backups.append(1) or (True, "ok"),
        )
        self.assertFalse(result.ok)
        self.assertIn("DjangoLux 9.0 or later", result.message)
        self.assertIn("bakes 1.17.0b2", result.message)
        self.assertEqual((backups, ops.restart_calls), ([], 0))
        self.assertFalse(self.runtime.active_file.exists())
        self.assertFalse((self.root / "unpacked-1.17.0b101").exists())

    def test_the_floor_reads_like_the_supervisor_reads_it(self):
        self.assertEqual(image_floor_problem("1.17.0b9", ">=1.17.0b1,<2.0", "1.17.0b2"), "")
        self.assertEqual(image_floor_problem("1.17.0b9", ">= 1.16.0, <1.18", "1.17.0b2"), "")
        self.assertEqual(image_floor_problem("1.17.0b9", "1.16.0b1", "1.17.0b2"), "")
        self.assertIn("needs", image_floor_problem("1.17.0b9", ">=1.17.0b3", "1.17.0b2"))
        self.assertIn("cannot read", image_floor_problem("1.17.0b9", "garbage!!", "1.17.0b2"))
        self.assertEqual(image_floor_problem("1.17.0b9", ">=9.0", ""), "", "an unknown image is not refused")


class RollbackTargetTests(_Base):
    """R2-012 and R2-058."""

    def setUp(self):
        super().setUp()
        for version in ("1.17.0b1", "1.17.0b2", "1.17.0b99"):
            self.stage(version, migrations={"rollback_compatible": True})
        self.activate("1.17.0b99")

    def rollback(self, **kwargs):
        ops = _Ops()
        return rollback_package_update(self.runtime, restart=ops.restart, health_check=ops.health,
                                       baked_version=lambda: "1.16.3", **kwargs)

    def test_the_named_version_is_the_one_activated(self):
        result = self.rollback(target_version="1.17.0b1")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.runtime.read_active()["version"], "1.17.0b1")

    def test_the_image_version_returns_to_the_image(self):
        result = self.rollback(target_version="1.16.3")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.runtime.read_active(), {})

    def test_a_version_that_is_no_target_is_refused_with_nothing_changed(self):
        for wanted in ("1.17.0b99", "1.17.0b50", "2.0.0"):
            with self.subTest(wanted=wanted):
                result = self.rollback(target_version=wanted)
                self.assertFalse(result.ok)
                self.assertIn("not a rollback target", result.message)
                self.assertIn("1.17.0b2", result.message)
                self.assertEqual(self.runtime.read_active()["version"], "1.17.0b99")

    def test_a_release_between_that_forbids_older_code_refuses_without_force(self):
        manifest = self.runtime.release_path("1.17.0b2") / "dlux" / "release-manifest.json"
        manifest.write_text(json.dumps(_manifest("1.17.0b2", migrations={"rollback_compatible": False})),
                            encoding="utf-8")
        refused = self.rollback(target_version="1.17.0b1")
        self.assertFalse(refused.ok)
        self.assertIn("DjangoLux 1.17.0b2 declares", refused.message)
        self.assertEqual(self.runtime.read_active()["version"], "1.17.0b99")
        self.assertTrue(self.rollback(target_version="1.17.0b2").ok, "a target at the release itself is allowed")
        self.activate("1.17.0b99")
        self.assertTrue(self.rollback(target_version="1.17.0b1", force=True).ok)

    def test_the_cli_passes_version_and_force_through(self):
        args = cli.parse_dlux_update_args(["--version", "1.17.0b1", "--force", "--backup", "skip"], action="rollback")
        self.assertEqual((args.version, args.force), ("1.17.0b1", True))
        self.assertFalse(cli.parse_dlux_update_args([], action="update").force)


class SelfRestartingServiceTests(unittest.TestCase):
    """R2-112: celery was stopped by its supervisor's generation restart, then again by Composer."""

    def test_only_a_watching_supervisor_counts(self):
        watching = {"command": ["python", "-m", "dlux.updater.supervisor", "--", "celery", "-A", "config", "worker"]}
        shell = {"command": "sh -c 'python -m dlux.updater.supervisor -- celery -A config worker'"}
        no_watch = {"command": ["python", "-m", "dlux.updater.supervisor", "--no-watch", "--", "celery"]}
        plain = {"command": ["celery", "-A", "config", "worker"]}
        self.assertTrue(cli.restarts_on_generation(watching))
        self.assertTrue(cli.restarts_on_generation(shell))
        self.assertFalse(cli.restarts_on_generation(no_watch))
        self.assertFalse(cli.restarts_on_generation(plain))
        self.assertFalse(cli.restarts_on_generation(None))

    def test_the_restart_skips_it_but_still_restarts_web(self):
        config = {"services": {
            "web": {"command": ["python", "-m", "dlux.updater.supervisor", "--", "gunicorn"]},
            "worker": {"command": ["python", "-m", "dlux.updater.supervisor", "--", "python", "manage.py", "dlux_worker"]},
            "celery": {"command": ["python", "-m", "dlux.updater.supervisor", "--", "celery", "-A", "config", "worker"]},
        }}
        launcher = mock.MagicMock()
        launcher.compose_config_json.return_value = config
        launcher.resolve_secrets.return_value = (True, "")
        launcher.migration_applier_services.return_value = ["worker"]
        launcher.recreate_containers.return_value = (True, "", "")
        restarted = []
        launcher.restart_containers.side_effect = lambda: restarted.append(list(launcher.restart_services)) or (True, "", "")
        args = cli.parse_dlux_update_args([], action="update")
        with mock.patch("composer.launcher.DockerComposeLauncher", return_value=launcher):
            restart, _health = cli._build_operations(args, ["web", "worker", "celery"], only_defined=True)
            self.assertEqual(restart(), (True, ""))
        launcher.recreate_containers.assert_called_once_with(["worker"])
        self.assertEqual(restarted, [["web"]])


if __name__ == "__main__":
    unittest.main()
