"""Regression tests for the `check` / `check --fix` findings of the 2026-11-25 break-it run.

An old Decrees stack went from stack schema 3 and DjangoLux 1.15.1 to schema 5 and
1.16.6b1, then was attacked. Each class names the finding it pins
(``pkg-django-lux/main/.xclude/validation/staging-migration-breakit-2026-11-25/FINDINGS.md``).
"""

import errno
import json
import os
import shutil
import socket
import stat
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import importlib

from composer import stack_schema
from composer import dlux_release_source as release_source
from composer import worker_migration as wm
from composer.checkup import FAIL, OK, WARN
from composer.launcher import DockerComposeLauncher

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCHEMA_4 = FIXTURES / "schema-4"
DLUX_MAIN = Path(__file__).resolve().parents[3] / "pkg-django-lux" / "main"


class _Lazy:
    """A composer module imported on first use, so each test fails on its own before the fix."""

    def __init__(self, name):
        self._name = name

    def __getattr__(self, attribute):
        return getattr(importlib.import_module(f"composer.{self._name}"), attribute)


check_lock = _Lazy("check_lock")
dlux_upgrade_path = _Lazy("dlux_upgrade_path")
stack_layout = _Lazy("stack_layout")

# Decrees' and CRM gov's compose.yml: a header comment above the value DjangoLux reads.
HALF_RESTAMPED = '# dlux stack schema 5\n\nx-environment: &de\n  DLUX_STACK_SCHEMA: "3"\n'


@contextmanager
def _in(directory):
    previous = os.getcwd()
    os.chdir(directory)
    try:
        yield
    finally:
        os.chdir(previous)


class _Project(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name).resolve()

    def write(self, name, text, *, crlf=False):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        data = text.replace("\n", "\r\n") if crlf else text
        path.write_bytes(data.encode("utf-8"))
        return path

    def schema_5_project(self, *, header=None, env="5"):
        """The schema-4 fixture brought to schema 5, with an optional header stamp."""
        shutil.copytree(SCHEMA_4, self.root, dirs_exist_ok=True)
        compose = wm.migrate_compose((self.root / "compose.yml").read_text(encoding="utf-8"))
        compose = compose.replace('DLUX_STACK_SCHEMA: "4"', f'DLUX_STACK_SCHEMA: "{env}"')
        if header is not None:
            compose = f"# dlux stack schema {header}\n" + compose
        (self.root / "compose.yml").write_text(compose, encoding="utf-8")
        for name in ("Dockerfile", "entrypoint.sh", "compose.dev.yml", ".proxy/Caddyfile"):
            path = self.root / name
            path.write_text(stack_schema.restamp(path.read_text(encoding="utf-8"), 5), encoding="utf-8")
        return self.root / "compose.yml"


# --- #2 half restamp -----------------------------------------------------------


class RestampEveryStampTests(unittest.TestCase):
    """#2: restamp() and read_stamp() handled only the first stamp in a file."""

    def test_restamp_rewrites_the_comment_and_the_value(self):
        restamped = stack_schema.restamp(HALF_RESTAMPED, 5)
        self.assertEqual(stack_schema.read_stamps(restamped), [5, 5])
        self.assertIn('DLUX_STACK_SCHEMA: "5"', restamped)

    def test_every_stamp_is_read(self):
        self.assertEqual(stack_schema.read_stamps(HALF_RESTAMPED), [5, 3])

    def test_a_step_restamps_a_file_whose_stamps_a_half_restamp_split(self):
        text = '# dlux stack schema 4\nx:\n  DLUX_STACK_SCHEMA: "3"\n'
        self.assertEqual(stack_schema.read_stamps(stack_schema.restamp_step(text, 4, 5)), [5, 5])

    def test_a_future_step_restamps_every_stamp_too(self):
        text = '# dlux stack schema 5\nx:\n  DLUX_STACK_SCHEMA: "5"\n'
        self.assertEqual(stack_schema.read_stamps(stack_schema.restamp_step(text, 5, 6)), [6, 6])
        self.assertIsNone(stack_schema.restamp_step('# dlux stack schema 7\n', 5, 6))

    def test_the_deployment_schema_is_the_lowest_stamp(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "compose.yml").write_text(HALF_RESTAMPED, encoding="utf-8")
            self.assertEqual(stack_schema.deployment_schema(root), 3)


class WorkerStepStampTests(_Project):
    """#2: the 4 -> 5 step left the DLUX_STACK_SCHEMA value under a restamped comment."""

    def test_the_header_and_the_value_both_reach_5(self):
        shutil.copytree(SCHEMA_4, self.root, dirs_exist_ok=True)
        compose = self.root / "compose.yml"
        compose.write_text("# dlux stack schema 4\n" + compose.read_text(encoding="utf-8"), encoding="utf-8")
        _sources, updates, _notes = wm._plan(self.root, ["compose.yml"])
        self.assertEqual(stack_schema.read_stamps(updates[compose.resolve()]), [5, 5])

    def test_a_compose_file_stamped_5_in_a_comment_only_gains_the_value(self):
        compose = self.schema_5_project(header=5)
        compose.write_text(
            compose.read_text(encoding="utf-8").replace('  DLUX_STACK_SCHEMA: "5"\n', ""), encoding="utf-8"
        )
        _sources, updates, _notes = wm._plan(self.root, ["compose.yml"])
        self.assertTrue(stack_schema.has_env_stamp(updates[compose.resolve()]))


class StackSchemaCheckTests(_Project):
    """#2: `stack-schema` read compose.yml's first stamp and called a split file consistent."""

    def _check(self):
        launcher = DockerComposeLauncher()
        launcher.active_compose_files = ["compose.yml"]
        with _in(self.root), patch.object(launcher, "plaintext_env_candidates", return_value=[]):
            return launcher._check_stack_schema()

    def test_a_compose_file_whose_stamps_disagree_fails(self):
        self.schema_5_project(header=5, env="3")
        result = self._check()
        self.assertEqual(result["level"], FAIL)
        self.assertIn("compose.yml says 5 and 3", result["message"])
        self.assertIn("restamp them 5", result["fix"])

    def test_agreeing_stamps_pass(self):
        self.schema_5_project(header=5)
        self.assertEqual(self._check()["level"], OK)


class StampRepairTests(_Project):
    """#2: `check --fix` restamps a split file from the layout the stack really has."""

    def test_the_layout_decides(self):
        compose = self.schema_5_project(header=5, env="3")
        self.assertEqual(stack_layout.layout_schema(self.root, ["compose.yml"]), (5, ""))
        updates, notes, schema = stack_layout.plan_repair(self.root, ["compose.yml"])
        self.assertEqual(schema, 5)
        self.assertEqual(stack_schema.read_stamps(updates[compose.resolve()]), [5, 5])
        self.assertEqual(notes, [])

    def test_a_layout_without_worker_is_schema_4(self):
        shutil.copytree(SCHEMA_4, self.root, dirs_exist_ok=True)
        compose = self.root / "compose.yml"
        compose.write_text("# dlux stack schema 5\n" + compose.read_text(encoding="utf-8"), encoding="utf-8")
        updates, _notes, schema = stack_layout.plan_repair(self.root, ["compose.yml"])
        self.assertEqual(schema, 4)
        self.assertEqual(stack_schema.read_stamps(updates[compose.resolve()]), [4, 4])

    def test_a_stamp_newer_than_this_composer_knows_is_left_alone(self):
        self.schema_5_project(header=6, env="5")
        updates, notes, _schema = stack_layout.plan_repair(self.root, ["compose.yml"])
        self.assertEqual(updates, {})
        self.assertIn("newer than 5", notes[0])

    def test_repair_validates_archives_and_writes(self):
        compose = self.schema_5_project(header=5, env="3")
        runs = []

        def runner(args, **_kwargs):
            runs.append(args)
            return SimpleNamespace(returncode=0, stdout=json.dumps({"services": {"web": {}}}), stderr="")

        with patch("composer.stack_layout.shutil.which", return_value="/usr/bin/docker"):
            outcome = stack_layout.repair(str(self.root), ["compose.yml"], command_runner=runner)
        self.assertTrue(outcome["applied"])
        self.assertEqual(stack_schema.read_stamps(compose.read_text(encoding="utf-8")), [5, 5])
        backup = Path(outcome["backup_root"]) / "stack-schema" / "compose.yml"
        self.assertEqual(stack_schema.read_stamps(backup.read_text(encoding="utf-8")), [5, 3])
        self.assertIn("config", runs[0])


# --- #14 line endings, #13 read-only files -------------------------------------


class LineEndingTests(_Project):
    """#14: `check --fix` rewrote a CRLF compose.yml as LF."""

    def test_write_updates_keeps_crlf(self):
        path = self.write("compose.yml", "a: 1\nb: 2\n", crlf=True)
        stack_schema.write_updates(self.root, {path: "a: 1\nb: 3\n"}, self.root / ".xclude" / "backup")
        self.assertEqual(path.read_bytes(), b"a: 1\r\nb: 3\r\n")

    def test_write_updates_keeps_lf(self):
        path = self.write("compose.yml", "a: 1\n")
        stack_schema.write_updates(self.root, {path: "a: 2\n"}, self.root / ".xclude" / "backup")
        self.assertEqual(path.read_bytes(), b"a: 2\n")

    def test_the_resident_block_writer_keeps_crlf(self):
        from composer.agent_installer import _atomic_write

        path = self.write("compose.yml", "a: 1\n", crlf=True)
        _atomic_write(path, "a: 2\nb: 1\n")
        self.assertEqual(path.read_bytes(), b"a: 2\r\nb: 1\r\n")

    def test_a_staged_candidate_keeps_crlf(self):
        from composer.stack_cleanup import _stage_candidate

        path = self.write("compose.yml", "a: 1\n", crlf=True)
        candidate = _stage_candidate(path, "a: 2\n")
        self.addCleanup(candidate.unlink)
        self.assertEqual(candidate.read_bytes(), b"a: 2\r\n")


@unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root writes read-only files")
class ReadOnlyFileTests(_Project):
    """#13: a read-only proxy file failed the fix with a raw '[Errno 13] Permission denied'."""

    def test_the_message_names_the_file_and_the_fix_and_nothing_is_changed(self):
        caddyfile = self.write(".proxy/Caddyfile", ":80 {\n}\n")
        compose = self.write("compose.yml", "a: 1\n")
        caddyfile.chmod(0o444)
        self.addCleanup(caddyfile.chmod, 0o644)
        backup = self.root / ".xclude" / "backup"
        with self.assertRaises(OSError) as raised:
            stack_schema.write_updates(self.root, {compose: "a: 2\n", caddyfile: ":81 {\n}\n"}, backup)
        message = str(raised.exception)
        self.assertNotIn("Errno", message)
        self.assertIn(".proxy/Caddyfile", message)
        self.assertIn("chmod u+w", message)
        self.assertEqual(compose.read_text(encoding="utf-8"), "a: 1\n")
        self.assertFalse(backup.exists())


# --- #5 and #4: the task worker and services that keep restarting ---------------


def _ps(*rows):
    return "\n".join(json.dumps(row) for row in rows)


class ServiceStateTests(unittest.TestCase):
    """#4: celery crash-looped for good and `check` never said so."""

    def setUp(self):
        self.launcher = DockerComposeLauncher()
        self.launcher.services = ["web", "worker", "celery"]
        self.launcher._celery_probe_cache = ""

    def _states(self, ps, inspect, logs=""):
        def compose(args, timeout=None):
            if args[0] == "ps":
                return True, ps, ""
            if args[0] == "logs":
                return True, logs, ""
            return False, "", ""

        with (
            patch.object(self.launcher, "run_docker_compose", side_effect=compose),
            patch.object(self.launcher, "run_command", return_value=(True, inspect, "")),
        ):
            return self.launcher._check_service_states()

    def test_a_restarting_service_fails_with_its_last_error(self):
        now = datetime.now(timezone.utc).isoformat()
        result = self._states(
            _ps({"Service": "web", "Name": "p-web-1", "ID": "aaa", "State": "running", "Health": "healthy"},
                {"Service": "celery", "Name": "p-celery-1", "ID": "bbb", "State": "restarting"}),
            f"aaa111|/p-web-1|0|false|{now}\nbbb222|/p-celery-1|57|true|{now}",
            logs="celery-1  | ModuleNotFoundError: No module named 'celery'\n",
        )
        self.assertEqual(result["level"], FAIL)
        self.assertIn("celery keeps restarting (57 restart(s))", result["message"])
        self.assertIn("No module named 'celery'", result["message"])
        self.assertNotIn("web", result["message"])

    def test_a_loop_caught_between_restarts_counts(self):
        recent = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        long_ago = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        looping = {"state": "running", "restarts": 9, "started_at": recent}
        settled = {"state": "running", "restarts": 9, "started_at": long_ago}
        self.assertTrue(DockerComposeLauncher._crash_looping(looping))
        self.assertFalse(DockerComposeLauncher._crash_looping(settled))

    def test_a_healthy_stack_passes(self):
        now = datetime.now(timezone.utc).isoformat()
        result = self._states(
            _ps({"Service": "web", "Name": "p-web-1", "ID": "aaa", "State": "running", "Health": "healthy"}),
            f"aaa111|/p-web-1|0|false|{now}",
        )
        self.assertEqual(result["level"], OK)


class CeleryProbeTests(unittest.TestCase):
    """#4: a project image without Celery is detected, so `--fix` can offer to remove celery."""

    def _probe(self, output, command=None):
        launcher = DockerComposeLauncher()
        launcher.services = ["web", "worker", "celery"]
        config = {"services": {"celery": {"command": command or [
            "python", "-m", "dlux.updater.supervisor", "--", "python", "-m", "celery", "-A", "config", "worker"]}}}
        config["services"]["celery"]["image"] = "registry.example/demo:2.2"
        with (
            patch.object(launcher, "compose_config_json", return_value=config),
            patch.object(launcher, "run_command", return_value=(True, output, "")) as run,
        ):
            reason = launcher._celery_unusable()
            check = launcher._check_celery()
        return reason, check, run.call_args.args[0]

    def test_no_celery_in_the_image(self):
        reason, check, args = self._probe('{"celery": false, "app": null}')
        self.assertIn("no Celery", reason)
        self.assertEqual(check["level"], WARN)
        self.assertIn("check --fix", check["fix"])
        self.assertEqual(args[-1], "config")
        self.assertEqual(args[:4], ["docker", "run", "--rm", "--pull"])
        self.assertIn("none", args)
        self.assertIn("registry.example/demo:2.2", args)

    def test_no_such_app(self):
        reason, _check, _args = self._probe('{"celery": true, "app": false}')
        self.assertIn("no Celery app 'config'", reason)

    def test_a_working_celery_is_left_alone(self):
        reason, check, _args = self._probe('{"celery": true, "app": true}')
        self.assertEqual(reason, "")
        self.assertIsNone(check)


class CeleryRemovalTests(_Project):
    """#4: `--fix` removes celery once worker exists and the image cannot run it."""

    def test_the_block_and_its_restart_entry_go(self):
        compose = self.schema_5_project()
        updated = wm.without_celery(compose.read_text(encoding="utf-8"))
        self.assertNotIn("\n  celery:\n", updated)
        self.assertIn("\n  worker:\n", updated)
        self.assertNotIn(",celery", updated)
        self.assertIn('COMPOSER_AGENT_RESTART_SERVICES: "web,worker,smtp-relay,caddy,ocr"', updated)

    def test_retire_validates_removes_the_container_then_writes(self):
        compose = self.schema_5_project()
        dev = self.root / "compose.dev.yml"
        calls = []

        def runner(args, **_kwargs):
            calls.append(list(args))
            if "config" in args:
                files = [args[index + 1] for index, arg in enumerate(args) if arg == "-f"]
                services = wm._services(Path(files[0]).read_text(encoding="utf-8"))
                return SimpleNamespace(returncode=0, stdout=json.dumps({"services": {s: {} for s in services}}), stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch("composer.worker_migration.shutil.which", return_value="/usr/bin/docker"):
            outcome = wm.retire_celery(str(self.root), ["compose.yml"], command_runner=runner)
        self.assertTrue(outcome["applied"])
        self.assertEqual(outcome["files"], ["compose.dev.yml", "compose.yml"])
        self.assertNotIn("celery", wm._services(compose.read_text(encoding="utf-8")))
        self.assertNotIn("celery", wm._services(dev.read_text(encoding="utf-8")))
        self.assertEqual(calls[-1][-4:], ["rm", "-s", "-f", "celery"])
        self.assertTrue((Path(outcome["backup_root"]) / "celery-removal" / "compose.yml").is_file())

    def test_a_development_celery_override_moves_to_worker(self):
        self.schema_5_project()
        dev = self.root / "compose.dev.yml"
        dev.write_text(
            "services:\n  web:\n    platform: linux/amd64\n  celery:\n    platform: linux/amd64\n",
            encoding="utf-8",
        )
        _sources, updates = wm._celery_plan(self.root, ["compose.yml"])
        services = wm._services(updates[dev.resolve()])
        self.assertEqual(services, {"web", "worker"})
        self.assertIn("  worker:\n    platform: linux/amd64\n", updates[dev.resolve()])

    def test_a_stack_without_worker_keeps_celery(self):
        shutil.copytree(SCHEMA_4, self.root, dirs_exist_ok=True)
        self.assertEqual(wm.celery_removal_preview(str(self.root), ["compose.yml"])["files"], [])


# --- #9 one check --fix at a time -------------------------------------------------


class CheckFixLockTests(_Project):
    """#9: nothing stopped a second `check --fix`, and a killed run went unnoticed."""

    def test_a_second_run_is_refused_with_who_holds_it(self):
        with check_lock.hold(self.root):
            with self.assertRaises(check_lock.CheckFixLockError) as raised:
                with check_lock.hold(self.root):
                    pass
        message = str(raised.exception)
        self.assertIn("Another 'composer check --fix' is running", message)
        self.assertIn(f"pid {os.getpid()}", message)
        self.assertIn("nothing was changed", message)

    def test_a_finished_run_leaves_no_record(self):
        with check_lock.hold(self.root) as previous:
            self.assertIsNone(previous)
        self.assertEqual((self.root / check_lock.LOCK_PATH).read_bytes(), b"")
        with check_lock.hold(self.root) as previous:
            self.assertIsNone(previous)

    def test_a_killed_run_is_reported_by_the_next(self):
        lock = self.root / check_lock.LOCK_PATH
        lock.parent.mkdir(parents=True)
        lock.write_text(json.dumps({"pid": 999999, "host": "gone", "started": 1764056000}))
        with check_lock.hold(self.root) as previous:
            self.assertEqual(previous["pid"], 999999)

    def test_without_flock_a_dead_holder_on_this_host_is_stale(self):
        lock = self.root / check_lock.LOCK_PATH
        lock.parent.mkdir(parents=True)
        unsupported = OSError(errno.ENOLCK, "No locks available")
        now = 1764056000.0
        lock.write_text(json.dumps({"pid": 999999, "host": socket.gethostname(), "started": now}))
        with patch("composer.check_lock._lock", side_effect=unsupported), \
                patch("composer.check_lock._alive", return_value=False):
            with check_lock.hold(self.root, clock=lambda: now + 5) as previous:
                self.assertEqual(previous["pid"], 999999)
        lock.write_text(json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "started": now}))
        with patch("composer.check_lock._lock", side_effect=unsupported):
            with self.assertRaises(check_lock.CheckFixLockError):
                with check_lock.hold(self.root, clock=lambda: now + 5):
                    pass

    def test_check_fix_refuses_while_another_holds_the_lock(self):
        launcher = DockerComposeLauncher()
        args = SimpleNamespace(fix=True)
        with _in(self.root), check_lock.hold("."):
            with patch.object(launcher, "_collect_checkup") as collect:
                results, fixed = launcher.collect_checkup(args)
        collect.assert_not_called()
        self.assertEqual([(r["level"], r["name"]) for r in results], [(FAIL, "check-fix")])
        self.assertEqual(fixed, [])

    def test_check_fix_reports_an_interrupted_run(self):
        launcher = DockerComposeLauncher()
        lock = self.root / check_lock.LOCK_PATH
        lock.parent.mkdir(parents=True)
        lock.write_text(json.dumps({"pid": 4242, "host": "abc", "started": 1764056000}))
        with _in(self.root), patch.object(launcher, "_collect_checkup", return_value=([], [])):
            _results, fixed = launcher.collect_checkup(SimpleNamespace(fix=True))
        self.assertEqual(fixed[0]["name"], "check-fix")
        self.assertIn("pid 4242", fixed[0]["message"])


# --- #12 a missing secrets file ---------------------------------------------------


class MissingSecretsTests(_Project):
    """#12: a missing secrets file was a WARN, so a deploy would run on compose defaults."""

    def _check(self, environ):
        launcher = DockerComposeLauncher()
        launcher.services = ["web"]
        launcher.active_compose_files = [str(self.root / "compose.yml")]
        with (
            patch.object(launcher, "plaintext_env_candidates", return_value=[]),
            patch.object(launcher, "inherited_secret_keys", return_value=[]),
            patch.dict(os.environ, environ, clear=True),
        ):
            return launcher._check_secrets()

    def test_compose_reading_the_files_values_fails(self):
        self.write("compose.yml", (
            "services:\n  web:\n    environment:\n"
            "      DJANGO_SECRET_KEY: ${DJANGO_SECRET_KEY:-local_secret}\n"
            "      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-admin_pass}\n"
            "      TIME_ZONE: ${TIME_ZONE:-UTC}\n"
        ))
        result = self._check({})
        self.assertEqual(result["level"], FAIL)
        self.assertIn("DJANGO_SECRET_KEY, POSTGRES_PASSWORD, TIME_ZONE", result["message"])

    def test_values_from_the_environment_keep_it_a_warning(self):
        self.write("compose.yml", "services:\n  web:\n    environment:\n      A: ${DJANGO_SECRET_KEY:-x}\n")
        self.assertEqual(self._check({"DJANGO_SECRET_KEY": "from-ci"})["level"], WARN)

    @unittest.skipUnless((DLUX_MAIN / "dlux/scaffold/templates/project/.secrets/.env.tmpl").is_file(),
                         "needs the DjangoLux checkout")
    def test_the_keys_mirror_djangolux_secrets_template(self):
        from composer.secrets_manager import SECRETS_FILE_KEYS

        template = (DLUX_MAIN / "dlux/scaffold/templates/project/.secrets/.env.tmpl").read_text(encoding="utf-8")
        keys = {
            line.split("=", 1)[0].strip() for line in template.splitlines()
            if "=" in line and not line.lstrip().startswith("#")
        }
        self.assertEqual(set(SECRETS_FILE_KEYS), keys)


# --- #1 circular advice -------------------------------------------------------------


def _manifest(version, floor=None):
    requires = {"stack_schema": f">={floor}"} if floor else {}
    return {
        "schema_version": 2, "version": version, "requires": requires,
        "migrations": {"effect": "additive", "rollback_compatible": True},
        "install": {"inline": "allowed"},
    }


RELEASES = {
    "1.15.1": None, "1.15.3": None, "1.15.4": None, "1.15.5": None,
    "1.16.0": 5, "1.16.3": 5, "1.16.5": 5, "1.16.6b1": 5,
}


class SteppingStoneTests(_Project):
    """#1: both refusals pointed at each other; the way out is worked out from release data."""

    def setUp(self):
        super().setUp()
        self.write("compose.yml", 'x-environment: &de\n  DLUX_STACK_SCHEMA: "3"\n')
        self.probed = []

    def _download(self, candidate, destination, opener=None):
        self.probed.append(candidate.version)
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_text(candidate.version)
        return Path(destination)

    def _find(self, running, **kwargs):
        index = [
            release_source.ReleaseCandidate(version, f"django_lux-{version}-py3-none-any.whl", "https://x", "0" * 64)
            for version in RELEASES
        ]
        with (
            _in(self.root),
            patch.object(release_source, "fetch_index", return_value=index),
            patch.object(release_source, "download_wheel", side_effect=self._download),
            patch.object(release_source, "read_wheel_manifest",
                         side_effect=lambda wheel: _manifest(Path(wheel).read_text(), RELEASES[Path(wheel).read_text()])),
        ):
            return dlux_upgrade_path.stepping_stone(running, **kwargs)

    def test_the_newest_release_this_stack_takes_inline(self):
        self.assertEqual(self._find("1.15.1", below="1.16.5"), "1.15.5")
        self.assertLessEqual(len(self.probed), dlux_upgrade_path.MAX_PROBES)

    def test_nothing_when_the_stack_already_runs_it(self):
        self.assertIsNone(self._find("1.15.5"))

    def test_the_refusal_dlux_release_source_words_is_recognised(self):
        with _in(self.root):
            reason = release_source.normalize_manifest(_manifest("1.16.5", 5), "1.16.5")["stack_schema_reason"]
        refusal = dlux_upgrade_path.parse_refusal(reason)
        self.assertEqual((refusal["version"], refusal["floor"], refusal["stamp"]), ("1.16.5", 5, 3))

    def test_the_update_advice_names_the_stone_and_the_image(self):
        advice = dlux_upgrade_path.update_way_out(
            refused="1.16.5", needed=5, stamp=3, running="1.15.1", stone="1.15.5"
        )
        self.assertIn("'./start.sh dlux update --version 1.15.5'", advice)
        self.assertIn("'./start.sh check --fix' (stack schema 4)", advice)
        self.assertIn("project image on DjangoLux 1.16.0 or later", advice)

    def test_check_fix_first_only_when_it_can_get_there(self):
        advice = dlux_upgrade_path.update_way_out(refused="1.16.0", needed=4, stamp=3, running="1.15.5", stone=None)
        self.assertTrue(advice.startswith("Run './start.sh check --fix' first"))

    def test_the_update_command_prints_the_way_out(self):
        from composer.dlux_package_cli import stack_schema_way_out

        runtime = SimpleNamespace(
            exists=lambda: True,
            read_active=lambda: {"version": "1.15.1", "source": "volume"},
            quarantined_versions=lambda: [],
        )
        message = ("DjangoLux 1.16.5 needs stack schema 5 (this stack is on 3): run "
                   "'./start.sh check --fix' with Composer 1.10 or later, first.")
        with patch("composer.dlux_upgrade_path.stepping_stone", return_value="1.15.5") as stone:
            text = stack_schema_way_out(message, runtime, channel="stable")
        self.assertTrue(text.startswith("DjangoLux 1.16.5 needs stack schema 5 (this stack is on 3)."))
        self.assertIn("--version 1.15.5", text)
        self.assertNotIn("Composer 1.10 or later, first", text)
        self.assertEqual(stone.call_args.kwargs["below"], "1.16.5")

    def test_the_stored_files_check_names_the_stone(self):
        launcher = DockerComposeLauncher()
        exposed = {"files": {".proxy/Caddyfile": "legacy"}, "custom": [], "exposed": True}
        with (
            _in(self.root),
            patch("composer.checkup.media_migration.inspect", return_value=exposed),
            patch.object(launcher, "_running_dlux_version", return_value=(1, 15, 1)),
            patch("composer.dlux_upgrade_path.stepping_stone", return_value="1.15.5"),
        ):
            result = launcher._check_media_privacy()
        self.assertEqual(result["level"], FAIL)
        self.assertIn("'./start.sh dlux update --version 1.15.5'", result["fix"])

    def test_no_stone_means_the_image(self):
        advice = dlux_upgrade_path.media_way_out(running=(1, 14, 0), stamp=3, stone=None)
        self.assertIn("put the project image on 1.15.3 or later", advice)


# --- the Operations preview shows every repair a panel apply makes -----------------


class OperationsPreviewTests(_Project):
    """A panel apply runs `check --fix`, so its preview must list the stamp repair and the celery removal."""

    def repairs(self, services, celery_reason=""):
        from composer import ops

        launcher = SimpleNamespace(
            services=services,
            active_compose_files=["compose.yml"],
            _maintenance_compose_files=lambda: ["compose.yml"],
            _celery_unusable=lambda: celery_reason,
            _worker_fix_plan=lambda *_args: ("", ""),
        )
        quiet = {name: [] for name in ("_maintenance_repair", "_front_proxy_repair", "_relay_env_repair", "_worker_repair")}
        previous = os.getcwd()
        os.chdir(self.root)
        try:
            with patch.object(ops, "_PREVIEWABLE", ()), \
                    patch.multiple(ops, **{name: (lambda _launcher, value=value: value) for name, value in quiet.items()}):
                repairs, _digest = ops._dry_run_repairs(launcher, SimpleNamespace(file=None))
        finally:
            os.chdir(previous)
        return {repair["name"]: repair for repair in repairs}

    def test_a_half_restamped_stack_previews_the_stamp_repair(self):
        self.schema_5_project(header=5, env="3")

        repair = self.repairs(["web", "worker"])["stack-schema"]

        self.assertEqual(repair["files"], ["compose.yml"])
        self.assertIn('-  DLUX_STACK_SCHEMA: "3"', repair["diff"])
        self.assertIn('+  DLUX_STACK_SCHEMA: "5"', repair["diff"])

    def test_a_consistent_stack_previews_no_stamp_repair(self):
        self.schema_5_project(header=5)

        self.assertNotIn("stack-schema", self.repairs(["web", "worker"]))

    def test_celery_without_celery_in_its_image_previews_its_removal(self):
        self.schema_5_project(header=5)

        repair = self.repairs(["web", "worker", "celery"], "The project image has no Celery, so celery cannot start.")["celery"]

        self.assertIn("compose.yml", repair["files"])
        self.assertIn("-  celery:", repair["diff"])
        self.assertIn("no Celery", repair["note"])

    def test_a_usable_celery_previews_nothing(self):
        self.schema_5_project(header=5)

        self.assertNotIn("celery", self.repairs(["web", "worker", "celery"], ""))


if __name__ == "__main__":
    unittest.main()
