"""Group access to app data for DjangoLux images that run as a non-root uid.

The data on deployed stacks was written by root images: root:root, directories
0755. These tests pin what Composer may change (modes; the group only on odd
entries), what it never touches (links, multiply-linked files, contents), the
container it runs the walk in, the `check` levels before and after an image
moves off root, and the modes Composer's own runtime-volume writers produce.
"""

import gzip
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from composer import app_permissions as ap
from composer.checkup import FAIL, OK, WARN, CheckupMixin

DLUX_CONTRACT = (
    Path(__file__).resolve().parents[3] / "pkg-django-lux" / "main" / "dlux" / "contracts" / "runtime.json"
)


def _st(uid, gid, mode, kind=stat.S_IFDIR, nlink=1):
    return os.stat_result((kind | mode, 0, 0, nlink, uid, gid, 0, 0, 0, 0))


def _config(project):
    image = "debeski/archive:latest"
    return {
        "name": "testbed-archive",
        "services": {
            "web": {"image": image, "volumes": [
                {"type": "volume", "source": "static", "target": "/app/staticfiles"},
                {"type": "bind", "source": f"{project}/media", "target": "/app/media"},
                {"type": "bind", "source": f"{project}/logs", "target": "/app/logs"},
                {"type": "bind", "source": f"{project}/imports", "target": "/app/imports", "read_only": True},
                {"type": "bind", "source": f"{project}/scanlink_releases", "target": "/app/scanlink_releases"},
                {"type": "volume", "source": "dlux_runtime", "target": "/opt/dlux-runtime", "read_only": True},
            ]},
            "worker": {"image": image, "volumes": [
                {"type": "volume", "source": "dlux_runtime", "target": "/opt/dlux-runtime"},
                {"type": "bind", "source": f"{project}/media", "target": "/app/media"},
            ]},
            "ocr": {"image": image},
            "caddy": {"image": "caddy:latest", "volumes": [
                {"type": "bind", "source": f"{project}/media", "target": "/app/media", "read_only": True},
                {"type": "volume", "source": "caddy_data", "target": "/data"},
            ]},
            "composer-executor": {"image": "debeski/composer:latest", "volumes": [
                {"type": "bind", "source": project, "target": project, "read_only": True},
                {"type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock"},
            ]},
        },
        "volumes": {"static": {}, "dlux_runtime": {"name": "custom_runtime"}},
    }


class IdentityTests(unittest.TestCase):
    def test_user_strings(self):
        self.assertEqual(ap.parse_user(""), ("root", 0, 0))
        self.assertEqual(ap.parse_user("root"), ("root", 0, 0))
        self.assertEqual(ap.parse_user("0:0"), ("root", 0, 0))
        self.assertEqual(ap.parse_user("10001:0"), ("user", 10001, 0))
        self.assertEqual(ap.parse_user("10001"), ("user", 10001, 0))
        self.assertEqual(ap.parse_user("app"), ("named", None, None))
        self.assertEqual(ap.parse_user("10001:app"), ("named", None, None))


class DiscoveryTests(unittest.TestCase):
    def test_every_app_mount_once_with_its_role_and_access(self):
        project = "/srv/archive"
        targets = {t.label: t for t in ap.discover_targets(_config(project), project)}

        self.assertEqual(sorted(targets), [
            "./imports", "./logs", "./media", "./scanlink_releases", "volume dlux_runtime", "volume static",
        ])
        self.assertTrue(targets["./imports"].read_only)
        self.assertFalse(targets["./media"].read_only)
        runtime = targets["volume dlux_runtime"]
        self.assertEqual((runtime.source, runtime.role, runtime.read_only), ("custom_runtime", "runtime", False))
        self.assertEqual(targets["volume static"].source, "testbed-archive_static")
        self.assertEqual(targets["./logs"].role, "logs")

    def test_no_app_image_means_nothing_to_do(self):
        self.assertEqual(ap.discover_targets({"services": {"db": {"image": "postgres:17"}}}, "/srv"), [])


class RuleTests(unittest.TestCase):
    def test_root_written_directory_gets_group_write_only(self):
        self.assertEqual(ap.planned(_st(0, 0, 0o755), 10001, 0, ap.needs(_st(0, 0, 0o755), "data", False)),
                         (None, 0o775))

    def test_root_written_files_are_readable_already(self):
        file = _st(0, 0, 0o644, stat.S_IFREG)
        self.assertIsNone(ap.planned(file, 10001, 0, ap.needs(file, "data", False)))

    def test_log_files_and_runtime_state_need_group_write(self):
        log = _st(0, 0, 0o644, stat.S_IFREG)
        self.assertEqual(ap.planned(log, 10001, 0, ap.needs(log, "logs", False)), (None, 0o664))
        private = _st(0, 0, 0o600, stat.S_IFREG)
        state = ("state", "deploy-status.json")
        self.assertEqual(ap.planned(private, 10001, 0, ap.needs(private, "runtime", False, state)), (None, 0o660))

    def test_installed_releases_are_only_read(self):
        package_dir = _st(0, 0, 0o755)
        module = _st(0, 0, 0o644, stat.S_IFREG)
        release = ("releases", "1.16.2", "dlux")
        self.assertIsNone(ap.planned(package_dir, 10001, 0, ap.needs(package_dir, "runtime", False, release)))
        self.assertIsNone(ap.planned(module, 10001, 0, ap.needs(module, "runtime", False, release + ("x.py",))))
        top = ("releases",)
        self.assertEqual(ap.planned(package_dir, 10001, 0, ap.needs(package_dir, "runtime", False, top)),
                         (None, 0o775))

    def test_a_foreign_group_is_moved_to_group_zero(self):
        copied = _st(501, 20, 0o644, stat.S_IFREG)
        self.assertEqual(ap.planned(copied, 10001, 0, ap.needs(copied, "logs", False)), (0, 0o664))

    def test_app_owned_entries_use_owner_bits(self):
        mine = _st(10001, 0, 0o755)
        self.assertIsNone(ap.planned(mine, 10001, 0, ap.needs(mine, "data", False)))

    def test_read_only_mounts_need_no_write(self):
        directory = _st(0, 0, 0o755)
        self.assertIsNone(ap.planned(directory, 10001, 0, ap.needs(directory, "data", True)))


class WalkTests(unittest.TestCase):
    """A tree owned by the test runner, granted to a uid that shares only its group."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
        self.uid = os.geteuid() + 1 if os.geteuid() else 10001
        self.gid = os.stat(self.root).st_gid

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def _tree(self):
        media = self.root / "media"
        (media / "decree").mkdir(parents=True)
        (media / "decree" / "a.pdf").write_bytes(b"%PDF original")
        os.chmod(media / "decree" / "a.pdf", 0o644)
        os.chmod(media / "decree", 0o755)
        os.chmod(media, 0o755)
        return media

    def _spec(self, path, role="data", read_only=False):
        return {"path": str(path), "label": "./media", "role": role, "read_only": read_only}

    def test_scan_changes_nothing(self):
        media = self._tree()
        before = {p: os.lstat(p).st_mode for p in (media, media / "decree", media / "decree" / "a.pdf")}

        summary = ap.process_target(self._spec(media), apply=False, uid=self.uid, gid=self.gid, depth=None)

        self.assertEqual((summary["wrong"], summary["changed"]), (2, 0))
        self.assertEqual(before, {p: os.lstat(p).st_mode for p in before})

    def test_apply_grants_directories_keeps_contents_and_mtime_and_records_a_manifest(self):
        media = self._tree()
        pdf = media / "decree" / "a.pdf"
        mtime = os.lstat(pdf).st_mtime_ns
        manifest_path = self.root / "manifest.tsv.gz"
        manifest = ap._LazyManifest(str(manifest_path))

        summary = ap.process_target(self._spec(media), apply=True, uid=self.uid, gid=self.gid, depth=None,
                                    manifest=manifest)
        manifest.close()

        self.assertEqual(summary["changed"], 2)
        self.assertEqual(stat.S_IMODE(os.lstat(media).st_mode), 0o775)
        self.assertEqual(stat.S_IMODE(os.lstat(media / "decree").st_mode), 0o775)
        self.assertEqual(stat.S_IMODE(os.lstat(pdf).st_mode), 0o644)
        self.assertEqual(pdf.read_bytes(), b"%PDF original")
        self.assertEqual(os.lstat(pdf).st_mtime_ns, mtime)
        with gzip.open(manifest_path, "rt", encoding="utf-8") as stream:
            rows = [line.rstrip("\n").split("\t") for line in stream]
        self.assertEqual(sorted(row[1] for row in rows), [".", "decree"])
        self.assertTrue(all(row[3] == "0755" and row[5] == "0775" for row in rows))

        again = ap.process_target(self._spec(media), apply=True, uid=self.uid, gid=self.gid, depth=None)
        self.assertEqual((again["wrong"], again["changed"]), (0, 0))

        reverted = ap.revert_target(self._spec(media), rows)
        self.assertEqual(reverted["reverted"], 2)
        self.assertEqual(stat.S_IMODE(os.lstat(media).st_mode), 0o755)

    def test_revert_leaves_entries_that_changed_since(self):
        media = self._tree()
        rows = [["./media", ".", "0:0", "0755", str(self.gid), "0775"]]
        os.chmod(media, 0o750)

        reverted = ap.revert_target(self._spec(media), rows)

        self.assertEqual((reverted["reverted"], reverted["skipped"]), (0, 1))
        self.assertEqual(stat.S_IMODE(os.lstat(media).st_mode), 0o750)

    def test_no_change_means_no_manifest_file(self):
        media = self._tree()
        os.chmod(media, 0o775)
        os.chmod(media / "decree", 0o775)
        manifest = ap._LazyManifest(str(self.root / "m.tsv.gz"))

        ap.process_target(self._spec(media), apply=True, uid=self.uid, gid=self.gid, depth=None, manifest=manifest)
        manifest.close()

        self.assertFalse((self.root / "m.tsv.gz").exists())

    def test_links_are_never_followed_and_hard_links_never_changed(self):
        media = self._tree()
        outside = self.root / "outside"
        outside.mkdir(mode=0o755)
        os.chmod(outside, 0o755)
        os.symlink(outside, media / "link")
        logs = self.root / "logs"
        logs.mkdir()
        os.chmod(logs, 0o775)
        (logs / "django.log").write_text("x")
        os.chmod(logs / "django.log", 0o644)
        os.link(logs / "django.log", self.root / "elsewhere.log")

        ap.process_target(self._spec(media), apply=True, uid=self.uid, gid=self.gid, depth=None)
        logs_summary = ap.process_target(self._spec(logs, role="logs"), apply=True, uid=self.uid, gid=self.gid,
                                         depth=None)

        self.assertEqual(stat.S_IMODE(os.lstat(outside).st_mode), 0o755)
        self.assertEqual(logs_summary["hardlinks"], 1)
        self.assertEqual(stat.S_IMODE(os.lstat(logs / "django.log").st_mode), 0o644)

    def test_a_missing_target_is_reported(self):
        summary = ap.process_target(self._spec(self.root / "nope"), apply=False, uid=self.uid, gid=self.gid,
                                    depth=None)
        self.assertTrue(summary["missing"])

    def test_quick_depth_stops_at_two_levels(self):
        media = self.root / "media"
        deep = media / "a" / "b" / "c"
        deep.mkdir(parents=True)
        for path in (media, media / "a", media / "a" / "b"):
            os.chmod(path, 0o775)
        os.chmod(deep, 0o755)

        quick = ap.process_target(self._spec(media), apply=False, uid=self.uid, gid=self.gid, depth=ap.QUICK_DEPTH)
        full = ap.process_target(self._spec(media), apply=False, uid=self.uid, gid=self.gid, depth=None)

        self.assertEqual((quick["wrong"], full["wrong"]), (0, 1))


class SiblingCommandTests(unittest.TestCase):
    def _targets(self):
        return [
            (ap.Target("./media", "bind", "/srv/a/media", "data", False), False),
            (ap.Target("./logs", "bind", "/srv/a/logs", "logs", False), True),
            (ap.Target("volume static", "volume", "a_static", "data", False), False),
        ]

    def test_scan_is_read_only_with_no_network_and_no_write_capabilities(self):
        command = ap.sibling_command(self._targets()[:1] + self._targets()[2:], image="img", action="scan",
                                     uid=10001, gid=0, depth=2)

        self.assertIn("--network", command)
        self.assertEqual(command[command.index("--network") + 1], "none")
        self.assertIn("no-new-privileges:true", command)
        self.assertNotIn("FOWNER", command)
        self.assertNotIn("CHOWN", command)
        mounts = [command[i + 1] for i, token in enumerate(command) if token == "--mount"]
        self.assertTrue(all(mount.endswith(",readonly") for mount in mounts))
        self.assertNotIn("-v", command)
        spec = json.loads(command[-1])
        self.assertEqual((spec["uid"], spec["gid"], spec["depth"]), (10001, 0, 2))

    def test_apply_may_create_a_missing_bind_directory_and_write_a_manifest(self):
        command = ap.sibling_command(self._targets(), image="img", action="apply", uid=10001, gid=0, depth=None,
                                     manifest_dir="/srv/a/.xclude/x", manifest_name=ap.MANIFEST_NAME)

        self.assertIn("FOWNER", command)
        self.assertIn("CHOWN", command)
        self.assertIn(f"/srv/a/logs:{ap.SIBLING_ROOT}/1:rw", command)
        self.assertIn(f"/srv/a/.xclude/x:{ap.SIBLING_MANIFEST}:rw", command)
        self.assertEqual(json.loads(command[-1])["manifest"], ap.MANIFEST_NAME)


class FakeLauncher:
    def __init__(self, project, sibling_targets, user="10001:0"):
        self.project = project
        self.config = _config(project)
        self.sibling_targets = sibling_targets
        self.user = user
        self.commands = []
        self.last_runtime_diagnostic = ""

    def compose_config_json(self):
        return self.config

    def run_command(self, argv, timeout=None, env=None):
        self.commands.append(argv)
        if argv[:3] == ["docker", "image", "inspect"]:
            return True, self.user + "\n", ""
        if argv[:3] == ["docker", "volume", "inspect"]:
            return True, "[]", ""
        if argv[:2] == ["docker", "inspect"]:
            return True, "sha256:self\n", ""
        if argv[:2] == ["docker", "run"]:
            action = argv[argv.index("app-permissions") + 1]
            return True, json.dumps({"targets": self.sibling_targets(action)}), ""
        return False, "", "unexpected"


class RunTests(unittest.TestCase):
    def setUp(self):
        self.project = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.project, ignore_errors=True))
        for name in ("media", "logs", "imports"):
            os.mkdir(os.path.join(self.project, name))

    def test_scan_reports_a_missing_bind_directory_without_creating_it(self):
        launcher = FakeLauncher(self.project, lambda action: [])

        summary = ap.run(launcher, "scan", project_dir=self.project)

        self.assertEqual(summary["missing"], ["./scanlink_releases"])
        self.assertFalse(os.path.exists(os.path.join(self.project, "scanlink_releases")))

    def test_a_ready_non_root_stack_is_left_alone(self):
        launcher = FakeLauncher(self.project, lambda action: [{"label": "./media", "wrong": 0}])
        os.mkdir(os.path.join(self.project, "scanlink_releases"))

        with patch("os.getcwd", return_value=self.project):
            ready, note = ap.ensure_for_image(launcher, "debeski/archive:latest")

        self.assertEqual((ready, note), (True, ""))
        actions = [c[c.index("app-permissions") + 1] for c in launcher.commands if c[:2] == ["docker", "run"]]
        self.assertEqual(actions, ["scan"])

    def test_wrong_entries_are_granted_before_up(self):
        def answer(action):
            if action == "scan":
                return [{"label": "./media", "wrong": 2}]
            return [{"label": "./media", "wrong": 2, "changed": 2}]

        launcher = FakeLauncher(self.project, answer)
        with patch("os.getcwd", return_value=self.project):
            ready, note = ap.ensure_for_image(launcher, "debeski/archive:latest")

        self.assertTrue(ready)
        self.assertIn("granted group access to 2 entries", note)

    def test_a_failed_grant_refuses(self):
        def answer(action):
            if action == "scan":
                return [{"label": "./media", "wrong": 1}]
            return [{"label": "./media", "wrong": 1, "errors": 1}]

        launcher = FakeLauncher(self.project, answer)
        with patch("os.getcwd", return_value=self.project):
            ready, note = ap.ensure_for_image(launcher, "debeski/archive:latest")

        self.assertFalse(ready)
        self.assertIn("nothing was recreated", note)

    def test_root_and_unknown_images_need_nothing(self):
        launcher = FakeLauncher(self.project, lambda action: [], user="")
        self.assertEqual(ap.ensure_for_image(launcher, "img"), (True, ""))
        self.assertFalse(any(c[:2] == ["docker", "run"] for c in launcher.commands))

    def test_a_named_user_is_refused(self):
        launcher = FakeLauncher(self.project, lambda action: [], user="app")
        ready, note = ap.ensure_for_image(launcher, "img")
        self.assertFalse(ready)
        self.assertIn("numeric USER", note)


class CheckLevelTests(unittest.TestCase):
    def _check(self, user, wrong, missing=()):
        launcher = FakeLauncher("/srv/a", lambda action: [], user=user)
        summary = {"targets": [{"label": "./media", "wrong": wrong, "sample": []}], "missing": list(missing)}
        with patch.object(ap, "run", return_value=summary):
            return CheckupMixin._check_app_permissions(launcher, deep=False)

    def test_root_image_with_ready_data_is_ok(self):
        self.assertEqual(self._check("", 0)["level"], OK)

    def test_root_image_with_root_only_data_warns(self):
        result = self._check("", 3)
        self.assertEqual(result["level"], WARN)
        self.assertIn("uid 10001 (gid 0)", result["message"])
        self.assertIn("check --fix", result["fix"])

    def test_non_root_image_on_root_only_data_fails(self):
        self.assertEqual(self._check("10001:0", 3)["level"], FAIL)
        self.assertEqual(self._check("10001:0", 0, missing=["./media"])["level"], FAIL)

    def test_non_root_image_with_ready_data_is_ok(self):
        self.assertEqual(self._check("10001:0", 0)["level"], OK)


class RuntimeWriterModeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        self.old = os.umask(0o022)
        self.addCleanup(os.umask, self.old)

    def test_runtime_pointer_and_status_files_are_0664_not_0600(self):
        from composer import dlux_runtime
        from composer.status_writer import StatusWriterMixin

        dlux_runtime._atomic_write(self.root / "active.json", "{}\n")
        self.assertEqual(stat.S_IMODE((self.root / "active.json").stat().st_mode), 0o664)

        writer = StatusWriterMixin()
        writer.status_file = str(self.root / "deploy-status.json")
        writer.composer_version = "test"
        writer.active_compose_files = []
        writer.write_status("ready")
        self.assertEqual(stat.S_IMODE((self.root / "deploy-status.json").stat().st_mode), 0o664)

    def test_owned_runtime_directories_are_opened_to_the_group(self):
        directory = self.root / "state"
        directory.mkdir()
        os.chmod(directory, 0o755)

        ap.share_runtime_dirs(directory, self.root / "missing")

        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o775)


class ContractMirrorTests(unittest.TestCase):
    def test_identity_and_modes_match_djangolux(self):
        if not DLUX_CONTRACT.is_file():
            self.skipTest("DjangoLux checkout not beside this one")
        permissions = json.loads(DLUX_CONTRACT.read_text(encoding="utf-8")).get("permissions")
        if not permissions:
            self.skipTest("this DjangoLux has no permissions block")
        self.assertEqual((permissions["app_uid"], permissions["app_gid"]), (ap.APP_UID, ap.APP_GID))
        self.assertEqual(int(permissions["file_mode"], 8), ap.SHARED_FILE_MODE)
        self.assertEqual(0o777 & ~ap.SHARED_UMASK, int(permissions["directory_mode"], 8))


if __name__ == "__main__":
    unittest.main()
