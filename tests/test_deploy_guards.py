"""Deploy guards: break-it round 2 (R2-006, R2-058, 2026-11-27).

R2-006: an older image deployed without ``--force`` onto a database a newer
release had migrated, whenever the Postgres volume was not the one the deploy
record names (a host move, a restore into a new volume, a fresh clone). The
guard now reads the database's applied migrations and compares them with the
migrations the target image ships.

R2-058: older DjangoLux code on a newer schema follows DjangoLux's rule: allowed,
with the same warning, only when the releases being left declare
``migrations.rollback_compatible``.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from composer import downgrade_guard
from composer.launcher import DockerComposeLauncher


def _labels(app, dlux):
    return {
        "org.opencontainers.image.version": app,
        "org.archive.dlux_baked_version": dlux,
    }


TARGET = "debeski/archive:latest"
OLD = _labels("1.6.3", "1.16.3")
NEW = _labels("1.6.3", "1.17.0b2")
CONFIG = {
    "name": "stack",
    "services": {
        "web": {"image": TARGET},
        "worker": {"image": TARGET},
        "db": {"image": "postgres:17", "volumes": [
            {"type": "volume", "source": "postgres_data", "target": "/var/lib/postgresql/data"}]},
    },
    "volumes": {"postgres_data": {"name": "stack_postgres_data"}},
}
#: What DjangoLux 1.16.3 ships, and what 1.17.0b2 added (dlux.0031, 0032).
OLD_KNOWN = {"dlux": [f"00{n:02d}_x" for n in range(1, 31)], "auth": ["0001_initial", "0012_alter"],
             "archive": ["0001_initial", "0002_more"]}
NEW_EXTRA = [("dlux", "0031_background_jobs"), ("dlux", "0032_singleton_cache_revision")]


def scan_output(known, project=("archive",), manifest=None):
    groups = [{"root": "/app", "kind": "project", "known": {k: v for k, v in known.items() if k in project},
               "manifest": None},
              {"root": "/usr/lib/site-packages", "kind": "package",
               "known": {k: v for k, v in known.items() if k not in project}, "manifest": manifest}]
    return json.dumps({"groups": groups})


class GuardHarness:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, cwd)
        self.launcher = DockerComposeLauncher()
        self.launcher.services = ["web", "worker", "db"]
        self.launcher.compose_config_json = lambda: CONFIG
        self.images = {TARGET: OLD}
        self.image_scans = {TARGET: scan_output(OLD_KNOWN, manifest={"version": "1.16.3",
                                                                     "rollback_compatible": True})}
        self.applied = {(app, name) for app, names in OLD_KNOWN.items() for name in names} | set(NEW_EXTRA)
        self.db_running = False
        self.volume = (True, json.dumps("2026-11-27T09:36:32Z"), "")
        self.compose_calls = []
        self.containers = []
        self.container_images = {}

        def run_command(argv, timeout=None, env=None):
            if argv[:3] == ["docker", "image", "inspect"]:
                ref = argv[-1] if "--format" in argv and argv[3] == "--format" else argv[3]
                if argv[3] == "--format":
                    return (ref in self.images), f"sha256:{abs(hash(ref))}\n", ""
                labels = self.images.get(ref)
                return (labels is not None), json.dumps(labels), ""
            if argv[:3] == ["docker", "volume", "inspect"]:
                if argv[3] == "stack_postgres_data":
                    return self.volume
                return False, "", "no such volume"
            if argv[:2] == ["docker", "inspect"]:
                return True, self.container_images.get(argv[-1], "sha256:composer") + "\n", ""
            if argv[:2] == ["docker", "run"] and downgrade_guard.MIGRATIONS_SCRIPT in argv:
                image = argv[argv.index("--entrypoint") + 2]
                if image not in self.image_scans:
                    return False, "", "no such image"
                return True, self.image_scans[image] + "\n", ""
            return False, "", "unexpected"

        def run_docker_compose(args, timeout=None):
            self.compose_calls.append(args)
            if args[:1] == ["up"]:
                self.db_running = True
                return True, "", ""
            if args[:1] == ["exec"]:
                if not self.db_running:
                    return False, "", "service db is not running"
                return True, "\n".join(f"{a} {n}" for a, n in sorted(self.applied)) + "\n", ""
            if args[:1] == ["stop"]:
                self.db_running = False
            return True, "", ""

        def ps(include_all=False):
            rows = list(self.containers)
            if self.db_running:
                rows.append({"Service": "db", "ID": "db1", "State": "running"})
            return True, rows, ""

        self.launcher.run_command = run_command
        self.launcher.run_docker_compose = run_docker_compose
        self.launcher.get_compose_ps_entries = ps
        self.printed = patch("builtins.print").start()
        self.addCleanup(patch.stopall)

    def output(self):
        return "\n".join(str(call.args[0]) for call in self.printed.call_args_list if call.args)


class MigrationComparisonTests(GuardHarness, unittest.TestCase):
    """R2-006: the database itself says which code migrated it."""

    def test_a_restore_into_a_new_volume_is_refused(self):
        # The record names the old volume: a host move / DR restore.
        downgrade_guard.write_record(
            downgrade_guard.label_versions(NEW), "debeski/archive:overlay", replace=False,
            database={"volume": "stack_postgres_data", "created_at": "2026-11-27T09:27:28Z"},
        )
        ok, message = self.launcher.downgrade_guard()
        self.assertFalse(ok)
        self.assertIn("2 migration(s) the image does not know", message)
        self.assertIn("dlux.0031_background_jobs", message)
        self.assertIn("--force", message)
        self.assertIn("restored or moved database carries its schema", message)
        note = self.output()
        self.assertNotIn("did not migrate", note)
        self.assertIn("The database's own migrations were compared", note)

    def test_a_fresh_clone_with_no_record_and_no_containers_is_refused(self):
        ok, message = self.launcher.downgrade_guard()
        self.assertFalse(ok)
        self.assertIn("dlux.0032_singleton_cache_revision", message)

    def test_the_database_started_for_the_read_is_stopped_after_a_refusal(self):
        self.assertFalse(self.launcher.downgrade_guard()[0])
        self.assertIn(["stop", "db"], self.compose_calls)
        self.assertFalse(self.db_running)

    def test_a_running_database_is_read_and_left_running(self):
        self.db_running = True
        self.assertFalse(self.launcher.downgrade_guard()[0])
        self.assertNotIn(["stop", "db"], self.compose_calls)
        self.assertFalse(any(call[:1] == ["up"] for call in self.compose_calls))

    def test_force_deploys_anyway(self):
        self.launcher.force = True
        ok, message = self.launcher.downgrade_guard()
        self.assertTrue(ok)
        self.assertTrue(message.startswith("--force: Deploying debeski/archive:latest"))

    def test_a_database_the_target_knows_passes(self):
        self.applied -= set(NEW_EXTRA)
        self.assertEqual(self.launcher.downgrade_guard(), (True, ""))

    def test_an_app_the_target_does_not_ship_is_not_compared(self):
        self.applied -= set(NEW_EXTRA)
        self.applied.add(("health_check_db", "0001_initial"))
        self.assertEqual(self.launcher.downgrade_guard(), (True, ""))

    def test_only_names_the_target_does_not_know_count(self):
        self.assertEqual(
            downgrade_guard.unknown_migrations(
                {("dlux", "0031_a"), ("dlux", "0001_x"), ("gone", "0001_initial")}, {"dlux": {"0001_x"}}),
            [("dlux", "0031_a")],
        )

    def test_no_data_volume_means_nothing_was_migrated(self):
        self.volume = (False, "", "Error: no such volume")
        self.assertEqual(self.launcher.downgrade_guard(), (True, ""))
        self.assertFalse(any(call[:1] in (["up"], ["exec"]) for call in self.compose_calls))

    def test_an_empty_database_passes(self):
        self.applied = set()
        self.assertEqual(self.launcher.downgrade_guard(), (True, ""))

    def test_a_database_that_cannot_be_read_warns_and_compares_the_records(self):
        def broken(args, timeout=None):
            if args[:1] == ["exec"]:
                return False, "", "psql: FATAL:  role \"x\" does not exist"
            return True, "", ""

        self.db_running = True
        self.launcher.run_docker_compose = broken
        self.assertEqual(self.launcher.downgrade_guard(), (True, ""))
        self.assertIn("could not compare this database's migrations", self.output())
        self.assertIn('role "x" does not exist', self.output())

    def test_a_missing_migrations_table_is_an_empty_database(self):
        def fresh(args, timeout=None):
            if args[:1] == ["exec"]:
                return False, "", 'ERROR:  relation "django_migrations" does not exist'
            return True, "", ""

        self.db_running = True
        self.launcher.run_docker_compose = fresh
        self.assertEqual(self.launcher.downgrade_guard(), (True, ""))
        self.assertNotIn("could not compare", self.output())

    def test_the_target_reads_its_active_runtime_release_too(self):
        calls = []
        self.launcher.runtime_volume = lambda: "stack_dlux_runtime"
        self.launcher.runtime_state = lambda volume: {"active": "1.17.0b2"}
        original = self.launcher.scan_migrations

        def scan(image, extra=(), volume="", only=False):
            calls.append((image, extra, volume))
            return original(image, extra, volume, only)

        self.launcher.scan_migrations = scan
        self.launcher.downgrade_guard()
        self.assertIn((TARGET, ("/opt/dlux-runtime/releases/1.17.0b2",), "stack_dlux_runtime"), calls)


class RollbackRuleTests(GuardHarness, unittest.TestCase):
    """R2-058: the rule both products follow for older DjangoLux code on a newer schema."""

    def setUp(self):
        super().setUp()
        self.images["debeski/archive:overlay"] = NEW
        self.image_scans["debeski/archive:overlay"] = scan_output(
            {**OLD_KNOWN, "dlux": OLD_KNOWN["dlux"] + ["0031_background_jobs", "0032_singleton_cache_revision"]},
            manifest={"version": "1.17.0b2", "rollback_compatible": True},
        )
        downgrade_guard.write_record(
            downgrade_guard.label_versions(NEW), "debeski/archive:overlay", replace=False,
            database={"volume": "stack_postgres_data", "created_at": "2026-11-27T09:36:32Z"},
        )

    def test_a_compatible_release_being_left_allows_it_with_the_warning(self):
        ok, message = self.launcher.downgrade_guard()
        self.assertTrue(ok, message)
        self.assertIn("DjangoLux 1.16.3 now runs on a schema with DjangoLux 1.17.0b2's migrations", self.output())
        self.assertIn("migrations are never reversed", self.output())
        self.assertIn("Ignore version mismatch", self.output())

    def test_a_release_that_does_not_declare_it_is_refused(self):
        self.image_scans["debeski/archive:overlay"] = self.image_scans["debeski/archive:overlay"].replace(
            '"rollback_compatible": true', '"rollback_compatible": false')
        ok, message = self.launcher.downgrade_guard()
        self.assertFalse(ok)
        self.assertIn("DjangoLux 1.17.0b2 does not declare migrations.rollback_compatible", message)

    def test_a_release_being_left_that_cannot_be_read_is_refused(self):
        del self.images["debeski/archive:overlay"]
        ok, message = self.launcher.downgrade_guard()
        self.assertFalse(ok)
        self.assertIn("1.17.0b2", message)

    def test_the_project_apps_own_migrations_are_never_covered(self):
        self.applied.add(("archive", "0003_newer"))
        ok, message = self.launcher.downgrade_guard()
        self.assertFalse(ok)
        self.assertIn("the project's own app archive is among them", message)

    def test_an_older_app_version_is_never_covered(self):
        self.images[TARGET] = _labels("1.6.2", "1.16.3")
        ok, message = self.launcher.downgrade_guard()
        self.assertFalse(ok)
        self.assertIn("app 1.6.2 is older than 1.6.3", message)

    def test_every_readable_release_crossed_must_declare_it(self):
        verdict = downgrade_guard.rollback_verdict(
            "1.16.3", "1.17.0b2", [],
            [{"version": "1.17.0b2", "rollback_compatible": True, "known": None},
             {"version": "1.17.0b1", "rollback_compatible": False, "known": None},
             {"version": "1.16.0", "rollback_compatible": False, "known": None}],
        )
        self.assertEqual(verdict, (False, "DjangoLux 1.17.0b1 does not declare migrations.rollback_compatible"))

    def test_the_oldest_release_that_knows_the_migrations_is_the_one_left(self):
        unknown = [("dlux", "0031_x")]
        allowed, left = downgrade_guard.rollback_verdict(
            "1.16.3", "", unknown,
            [{"version": "1.18.0", "rollback_compatible": False, "known": {"dlux": {"0031_x", "0040_y"}}},
             {"version": "1.17.0", "rollback_compatible": True, "known": {"dlux": {"0031_x"}}}],
        )
        self.assertEqual((allowed, left), (True, "1.17.0"))

    def test_releases_on_the_runtime_volume_count(self):
        verdict = downgrade_guard.rollback_verdict(
            "1.16.3", "1.17.0b2", [], [{"version": "1.17.0b2", "rollback_compatible": True, "known": None}])
        self.assertEqual(verdict, (True, "1.17.0b2"))


class MigrationsScriptTests(unittest.TestCase):
    """The scan runs in the app image with nothing imported: run it for real here."""

    def _tree(self, root: Path, label: str, names, body: str = ""):
        folder = root / label / "migrations"
        folder.mkdir(parents=True)
        (folder / "__init__.py").write_text("")
        for name in names:
            (folder / f"{name}.py").write_text(body)

    def test_labels_names_replaces_and_the_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = Path(tmp) / "releases" / "1.17.0b2"
            self._tree(release, "dlux", ["0031_background_jobs", "_helper"])
            self._tree(release, "dlux_extra", ["0001_squashed"], body=(
                "class Migration:\n    replaces = [('dlux_extra', '0001_a'), ('dlux_extra', '0002_b')]\n"))
            (release / "dlux" / "release-manifest.json").write_text(json.dumps(
                {"version": "1.17.0b2", "migrations": {"rollback_compatible": True}}))
            (release / "dlux" / "__pycache__").mkdir()
            out = subprocess.run(
                [sys.executable, "-I", "-c", downgrade_guard.MIGRATIONS_SCRIPT, "--only", str(release)],
                capture_output=True, text=True, check=True,
            ).stdout
        groups = downgrade_guard.parse_scan(out)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["known"]["dlux"], {"0031_background_jobs"})
        self.assertEqual(groups[0]["known"]["dlux_extra"], {"0001_squashed", "0001_a", "0002_b"})
        self.assertEqual(downgrade_guard.group_manifest(groups),
                         {"version": "1.17.0b2", "rollback_compatible": True})

    def test_the_working_directory_is_the_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._tree(Path(tmp), "archive", ["0001_initial"])
            out = subprocess.run(
                [sys.executable, "-I", "-c", downgrade_guard.MIGRATIONS_SCRIPT],
                capture_output=True, text=True, check=True, cwd=tmp,
            ).stdout
        known, project = downgrade_guard.merged_known(downgrade_guard.parse_scan(out))
        self.assertEqual(known["archive"], {"0001_initial"})
        self.assertIn("archive", project)

    def test_applied_rows_parse(self):
        self.assertEqual(
            downgrade_guard.parse_applied("dlux 0031_background_jobs\nauth 0001_initial\n\n"),
            {("dlux", "0031_background_jobs"), ("auth", "0001_initial")},
        )

    def test_the_runtime_script_lists_every_release_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for version, compatible in (("1.17.0b1", True), ("1.17.0b2", False)):
                folder = root / "releases" / version / "dlux"
                folder.mkdir(parents=True)
                (folder / "release-manifest.json").write_text(json.dumps(
                    {"version": version, "migrations": {"rollback_compatible": compatible}}))
            out = subprocess.run(
                [sys.executable, "-I", "-c", downgrade_guard.RUNTIME_STATE_SCRIPT, str(root)],
                capture_output=True, text=True, check=True,
            ).stdout
        self.assertEqual(json.loads(out)["manifests"], {
            "1.17.0b1": {"version": "1.17.0b1", "rollback_compatible": True},
            "1.17.0b2": {"version": "1.17.0b2", "rollback_compatible": False},
        })


if __name__ == "__main__":
    unittest.main()
