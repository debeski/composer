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

DLUX_CHECKOUT = Path(
    os.environ.get("DLUX_CHECKOUT") or Path(__file__).resolve().parents[3] / "pkg-django-lux" / "main"
)
DLUX_CONTRACT = DLUX_CHECKOUT / "dlux" / "contracts" / "runtime.json"


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


def _dev_config(project):
    """`./start.sh -d`: the dev file resets `image:` and builds web/worker instead."""
    config = _config(project)
    config["name"] = "eservices"
    for name in ("web", "worker", "ocr"):
        config["services"][name].pop("image")
        config["services"][name]["build"] = {"context": project, "dockerfile": "Dockerfile"}
    return config


class DevModeTests(unittest.TestCase):
    """In development mode no app service had an `image`, so app_image() was ""
    and app-permissions was skipped before `up`: a USER 10001:0 image then
    stopped at DjangoLux's mount_access gate (exit 78)."""

    def test_the_built_image_is_composes_own_name(self):
        self.assertEqual(ap.app_image(_dev_config("/srv/e")), "eservices-web")

    def test_every_service_built_from_the_app_build_counts(self):
        self.assertEqual(ap.app_services(_dev_config("/srv/e")), ["ocr", "web", "worker"])

    def test_the_workers_mounts_are_included(self):
        labels = {t.label for t in ap.discover_targets(_dev_config("/srv/e"), "/srv/e")}
        self.assertIn("volume dlux_runtime", labels)
        self.assertIn("./media", labels)

    def test_a_short_build_string_is_the_same_build(self):
        config = _dev_config("/srv/e")
        for name in ("web", "worker", "ocr"):
            config["services"][name]["build"] = "."
        self.assertEqual(ap.app_services(config), ["ocr", "web", "worker"])

    def test_another_build_is_another_app(self):
        config = _dev_config("/srv/e")
        config["services"]["ocr"]["build"] = {"context": "/srv/e/ocr"}
        self.assertEqual(ap.app_services(config), ["web", "worker"])

    def test_production_matching_is_unchanged(self):
        self.assertEqual(ap.app_image(_config("/srv/a")), "debeski/archive:latest")
        self.assertEqual(ap.app_services(_config("/srv/a")), ["ocr", "web", "worker"])

    def test_without_a_project_name_nothing_is_guessed(self):
        config = _dev_config("/srv/e")
        config.pop("name")
        self.assertEqual(ap.app_image(config), "")

    def test_prepare_app_data_runs_in_dev_mode(self):
        from composer.launcher import DockerComposeLauncher

        launcher = DockerComposeLauncher()
        launcher.compose_config_json = lambda: _dev_config("/srv/e")
        with patch.object(ap, "ensure_for_image", return_value=(True, "granted")) as ensure:
            self.assertEqual(launcher.prepare_app_data(), (True, "granted"))
        self.assertEqual(ensure.call_args.args[1], "eservices-web")


class RuleTests(unittest.TestCase):
    def test_root_written_directory_gets_group_write_only(self):
        self.assertEqual(ap.planned(_st(0, 0, 0o755), 10001, 0, ap.needs(_st(0, 0, 0o755), "data", False)),
                         (None, 0o775))

    def test_root_written_files_are_readable_already(self):
        file = _st(0, 0, 0o644, stat.S_IFREG)
        self.assertIsNone(ap.planned(file, 10001, 0, ap.needs(file, "data", False)))

    def test_log_files_need_group_write(self):
        log = _st(0, 0, 0o644, stat.S_IFREG)
        self.assertEqual(ap.planned(log, 10001, 0, ap.needs(log, "logs", False)), (None, 0o664))

    def test_runtime_state_files_need_only_group_read(self):
        # DjangoLux replaces them by renaming a new file over the old one, which
        # takes write on the directory, never on the file.
        state = ("state", "image-available.json")
        published = _st(0, 0, 0o644, stat.S_IFREG)
        self.assertIsNone(ap.planned(published, 10001, 0, ap.needs(published, "runtime", False, state)))
        private = _st(0, 0, 0o600, stat.S_IFREG)
        self.assertEqual(ap.planned(private, 10001, 0, ap.needs(private, "runtime", False, state)), (None, 0o640))
        directory = _st(0, 0, 0o755)
        for parts in (("state",), ("state", "relay"), ()):
            self.assertEqual(ap.planned(directory, 10001, 0, ap.needs(directory, "runtime", False, parts)),
                             (None, 0o775))

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
    def __init__(self, project, sibling_targets, user="10001:0", local=True, pulled_user=None, pull_ok=True):
        self.project = project
        self.config = _config(project)
        self.sibling_targets = sibling_targets
        self.user = user
        self.local = local
        self.pulled_user = user if pulled_user is None else pulled_user
        self.pull_ok = pull_ok
        self.build_images = False
        self.commands = []
        self.compose = []
        self.events = []
        self.volumes_exist = True
        self.create_ok = True
        self.last_runtime_diagnostic = ""

    def compose_config_json(self):
        return self.config

    def run_docker_compose(self, args, timeout=None):
        self.compose.append(list(args))
        self.events.append(["compose", *args])
        if args[:2] == ["up", "--no-start"]:
            self.volumes_exist = self.volumes_exist or self.create_ok
            return (True, "", "") if self.create_ok else (False, "", "volume driver refused")
        if args[0] == "pull" and self.pull_ok:
            self.local, self.user = True, self.pulled_user
            return True, "", ""
        if args[0] == "build":
            self.local = True
            return True, "", ""
        return False, "", "pull access denied for debeski/archive"

    def run_command(self, argv, timeout=None, env=None):
        self.commands.append(argv)
        self.events.append(list(argv))
        if argv[:3] == ["docker", "image", "inspect"]:
            if not self.local:
                return False, "", "No such image"
            return True, self.user + "\n", ""
        if argv[:3] == ["docker", "volume", "inspect"]:
            return (True, "[]", "") if self.volumes_exist else (False, "", "no such volume")
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

    def test_a_fresh_install_has_compose_create_the_volumes_before_the_grant(self):
        # A plain USER 10001:0 image on empty volumes exited 78 at its first start:
        # the scan skipped static and dlux_runtime, which `up` then created from the
        # image's root 0755 mount points.
        def answer(action):
            if action == "scan":
                return [{"label": "volume static", "wrong": 1}]
            return [{"label": "volume static", "wrong": 1, "changed": 1}]

        launcher = FakeLauncher(self.project, answer)
        launcher.volumes_exist = False
        os.mkdir(os.path.join(self.project, "scanlink_releases"))
        with patch("os.getcwd", return_value=self.project):
            ready, note = ap.ensure_for_image(launcher, "debeski/archive:latest")

        self.assertTrue(ready, note)
        self.assertIn("granted group access to 1 entries", note)
        create = launcher.events.index(["compose", "up", "--no-start", "--no-deps", "ocr", "web", "worker"])
        walks = [i for i, event in enumerate(launcher.events) if event[:2] == ["docker", "run"]]
        self.assertTrue(walks and create < walks[0], launcher.events)
        scan = launcher.events[walks[0]]
        self.assertTrue(any(arg.startswith("type=volume,source=testbed-archive_static,") for arg in scan), scan)
        self.assertTrue(any("source=custom_runtime" in arg for arg in scan), scan)
        self.assertFalse(any(event[:2] == ["compose", "up"] and "--no-start" not in event for event in launcher.events))

    def test_volumes_that_cannot_be_created_refuse_before_anything_starts(self):
        launcher = FakeLauncher(self.project, lambda action: [])
        launcher.volumes_exist, launcher.create_ok = False, False
        with patch("os.getcwd", return_value=self.project):
            ready, note = ap.ensure_for_image(launcher, "debeski/archive:latest")

        self.assertFalse(ready)
        self.assertIn("volume static", note)
        self.assertIn("volume driver refused", note)
        self.assertFalse(any(event[:2] == ["docker", "run"] for event in launcher.events))

    def test_existing_volumes_create_nothing(self):
        launcher = FakeLauncher(self.project, lambda action: [{"label": "./media", "wrong": 0}])
        os.mkdir(os.path.join(self.project, "scanlink_releases"))
        with patch("os.getcwd", return_value=self.project):
            ap.ensure_for_image(launcher, "debeski/archive:latest")
        self.assertFalse(any(args[:1] == ["up"] for args in launcher.compose), launcher.compose)

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

    def test_a_root_image_needs_nothing(self):
        launcher = FakeLauncher(self.project, lambda action: [], user="")
        self.assertEqual(ap.ensure_for_image(launcher, "img"), (True, ""))
        self.assertFalse(any(c[:2] == ["docker", "run"] for c in launcher.commands))
        self.assertEqual(launcher.compose, [])

    def test_an_image_not_on_the_host_is_pulled_and_then_granted_for(self):
        # `up` would pull it and recreate web and worker straight onto data the
        # new uid cannot use; the pull happens here first so the grant can run.
        def answer(action):
            if action == "scan":
                return [{"label": "./media", "wrong": 2}]
            return [{"label": "./media", "wrong": 2, "changed": 2}]

        launcher = FakeLauncher(self.project, answer, user="", local=False, pulled_user="10001:0")
        with patch("os.getcwd", return_value=self.project):
            ready, note = ap.ensure_for_image(launcher, "debeski/archive:latest")

        self.assertTrue(ready)
        self.assertIn("granted group access to 2 entries", note)
        self.assertEqual(launcher.compose, [["pull", "ocr", "web", "worker"]])
        first_walk = next(i for i, c in enumerate(launcher.commands) if c[:2] == ["docker", "run"])
        inspects = [i for i, c in enumerate(launcher.commands) if c[:3] == ["docker", "image", "inspect"]]
        self.assertEqual(len(inspects), 2)
        self.assertLess(inspects[-1], first_walk)

    def test_a_pulled_root_image_needs_nothing(self):
        launcher = FakeLauncher(self.project, lambda action: [], user="", local=False, pulled_user="")
        self.assertEqual(ap.ensure_for_image(launcher, "img"), (True, ""))
        self.assertFalse(any(c[:2] == ["docker", "run"] for c in launcher.commands))

    def test_an_image_that_cannot_be_pulled_refuses_with_the_reason(self):
        launcher = FakeLauncher(self.project, lambda action: [], local=False, pull_ok=False)
        ready, note = ap.ensure_for_image(launcher, "debeski/archive:latest")
        self.assertFalse(ready)
        self.assertIn("nothing was recreated", note)
        self.assertIn("pull access denied", note)
        self.assertFalse(any(c[:2] == ["docker", "run"] for c in launcher.commands))

    def test_a_buildable_service_is_built_when_the_pull_leaves_no_image(self):
        launcher = FakeLauncher(self.project, lambda action: [], user="", local=False, pull_ok=False)
        launcher.config["services"]["web"]["build"] = {"context": "."}
        self.assertEqual(ap.ensure_for_image(launcher, "img"), (True, ""))
        self.assertEqual(launcher.compose, [["pull", "--ignore-pull-failures", "ocr", "web", "worker"], ["build", "web"]])

    def test_build_mode_builds_before_reading_the_user(self):
        # `up --build` would run a freshly built image, whatever the local one says.
        launcher = FakeLauncher(self.project, lambda action: [], user="")
        launcher.config["services"]["web"]["build"] = {"context": "."}
        launcher.build_images = True
        ap.ensure_for_image(launcher, "img")
        self.assertEqual(launcher.compose, [["build", "web"]])
        self.assertLess(0, len(launcher.commands))

    def test_a_named_user_is_refused(self):
        launcher = FakeLauncher(self.project, lambda action: [], user="app")
        ready, note = ap.ensure_for_image(launcher, "img")
        self.assertFalse(ready)
        self.assertIn("numeric USER", note)


class DevModeGrantTests(unittest.TestCase):
    def setUp(self):
        self.project = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.project, ignore_errors=True))
        for name in ("media", "logs", "imports"):
            os.mkdir(os.path.join(self.project, name))

    def test_the_dev_build_is_built_read_and_granted_before_up(self):
        wrong = [{"label": "./media", "kind": "bind", "wrong": 3, "missing": False}]
        launcher = FakeLauncher(
            self.project,
            lambda action: wrong if action == "scan" else [dict(wrong[0], changed=3, wrong=0)],
            local=False, pull_ok=False,
        )
        launcher.config = _dev_config(self.project)
        ready, note = ap.ensure_for_image(launcher, ap.app_image(launcher.config), launcher.config)
        self.assertTrue(ready, note)
        self.assertEqual(launcher.compose[-1], ["build", "ocr", "web", "worker"])
        inspected = [argv[3] for argv in launcher.commands if argv[:3] == ["docker", "image", "inspect"]]
        self.assertEqual(set(inspected), {"eservices-web"})
        self.assertTrue(any(argv[:2] == ["docker", "run"] and "apply" in argv for argv in launcher.commands))


class PrepareScopeTests(unittest.TestCase):
    """Recreates (``dlux update``, the Operations card) run the same pre-up step."""

    def _launcher(self, ready=(True, "")):
        from composer.launcher import DockerComposeLauncher

        launcher = DockerComposeLauncher()
        launcher.compose_config_json = lambda: _config("/srv/a")
        return launcher

    def test_a_recreate_of_other_services_skips_it(self):
        launcher = self._launcher()
        with patch.object(ap, "ensure_for_image") as ensure:
            self.assertEqual(launcher.prepare_app_data(["caddy"]), (True, ""))
        ensure.assert_not_called()

    def test_a_recreate_of_an_app_service_runs_it(self):
        launcher = self._launcher()
        with patch.object(ap, "ensure_for_image", return_value=(True, "")) as ensure:
            launcher.prepare_app_data(["worker"])
        ensure.assert_called_once()

    def test_a_refused_grant_recreates_nothing(self):
        launcher = self._launcher()
        launcher.run_docker_compose_streaming = lambda *a, **k: self.fail("recreated")
        with patch.object(ap, "ensure_for_image", return_value=(False, "Could not prepare")):
            self.assertEqual(launcher.recreate_containers(["web"]), (False, "", "Could not prepare"))

    def test_the_operations_recreate_refuses_too(self):
        from composer import executor_ops

        with patch.dict(os.environ, {"COMPOSER_AGENT_RESTART_SERVICES": "web"}), \
                patch("composer.launcher.DockerComposeLauncher.resolve_secrets", autospec=True, return_value=(True, "")), \
                patch("composer.launcher.DockerComposeLauncher.resolve_active_compose_files", autospec=True), \
                patch("composer.launcher.DockerComposeLauncher.prepare_app_data", autospec=True,
                      return_value=(False, "Could not prepare")) as prepare, \
                patch("composer.launcher.DockerComposeLauncher.run_docker_compose", autospec=True) as run:
            code, message = executor_ops._run_recreate("op", "web")
        self.assertEqual(code, 1)
        self.assertIn("web was not recreated: Could not prepare", message)
        self.assertEqual(prepare.call_args.args[1], ["web"])
        run.assert_not_called()


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

    def test_the_package_lock_is_group_shared(self):
        from composer.dlux_runtime import PACKAGE_LOCK_FILENAME, DluxRuntime

        os.umask(0o002)
        runtime = DluxRuntime(self.root)
        with runtime.package_operation_lock():
            pass
        self.assertEqual(stat.S_IMODE((self.root / "state" / PACKAGE_LOCK_FILENAME).stat().st_mode), 0o664)

    def test_dlux_commands_write_with_the_shared_umask(self):
        # They write state/ on the runtime volume, on the host or in the
        # sibling container they re-run in, outside the umask-0002 processes.
        from composer.launcher import DockerComposeLauncher

        seen = []
        with patch("sys.argv", ["composer", "dlux", "channel"]), \
                patch("composer.dlux_package_cli.run_dlux_channel", side_effect=lambda *a: seen.append(os.umask(0o022)) or 0):
            with self.assertRaises(SystemExit):
                DockerComposeLauncher().run()
        self.assertEqual(seen, [ap.SHARED_UMASK])

    def test_the_resident_update_helper_writes_with_the_shared_umask(self):
        from composer.executor_ops import _AGENT_UPDATE_SCRIPT

        self.assertEqual(_AGENT_UPDATE_SCRIPT.strip().splitlines()[0], "umask 0002")

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
