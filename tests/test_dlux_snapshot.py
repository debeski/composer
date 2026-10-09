"""The operator's `dlux update` / `dlux rollback` back up before they swap.

DjangoLux takes its own backup before it hands Composer an update, but the CLI
never passed through DjangoLux, so an operator's update ran with no snapshot
(django-lux testbed_break_scenarios.md S4). These tests pin where the backup
happens (after resolving, before anything changes), that a failed backup
changes nothing, how the snapshot is taken in the stack, and when the CLI
leaves it to DjangoLux.
"""

import json
import os
import sys
import unittest
import unittest.mock
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from composer import dlux_package_cli
from composer.dlux_package_update import apply_package_update, rollback_package_update
from composer.dlux_runtime import DluxRuntime
from composer.dlux_snapshot import take_snapshot
from tests.test_dlux_package_update import _FakeSource, _Ops


class _Snapshot:
    def __init__(self, result=(True, "backup abc12345 in celery (10 rows)")):
        self.result = result
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.result


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runtime = DluxRuntime(self.root / "dlux-runtime")
        self.source = _FakeSource(self.root)
        self.addCleanup(self._tmp.cleanup)

    def _apply(self, version, snapshot=None, ops=None):
        ops = ops or _Ops()
        return apply_package_update(
            self.runtime, restart=ops.restart, health_check=ops.health,
            target_version=version, source=self.source, workdir=self.root / f"w-{version}",
            before_swap=snapshot,
        ), ops


class ApplySnapshotTests(_Base):
    def test_the_backup_runs_before_staging(self):
        snapshot = _Snapshot()
        result, _ops = self._apply("1.8.0", snapshot)

        self.assertTrue(result.ok, result.message)
        self.assertEqual(snapshot.calls, 1)
        self.assertLess(result.steps.index("backing-up"), result.steps.index("staging"))

    def test_a_failed_backup_changes_nothing(self):
        self._apply("1.7.1")
        snapshot = _Snapshot((False, "the pre-update backup failed in celery: disk full"))
        result, ops = self._apply("1.8.0", snapshot)

        self.assertFalse(result.ok)
        self.assertIn("Nothing was changed", result.message)
        self.assertIn("disk full", result.message)
        self.assertEqual(self.runtime.read_active()["version"], "1.7.1")
        self.assertNotIn("1.8.0", self.runtime.staged_versions())
        self.assertEqual(ops.restart_calls, 0)

    def test_no_backup_when_the_release_is_already_active(self):
        self._apply("1.8.0")
        snapshot = _Snapshot()
        result, _ops = self._apply("1.8.0", snapshot)

        self.assertTrue(result.ok)
        self.assertEqual(snapshot.calls, 0)


class RollbackSnapshotTests(_Base):
    def test_the_rollback_backs_up_before_the_pointer_moves(self):
        self._apply("1.7.1")
        self._apply("1.8.0")
        snapshot = _Snapshot()
        ops = _Ops()
        result = rollback_package_update(
            self.runtime, restart=ops.restart, health_check=ops.health, before_swap=snapshot,
        )

        self.assertTrue(result.ok, result.message)
        self.assertEqual(snapshot.calls, 1)
        self.assertEqual(self.runtime.read_active()["version"], "1.7.1")

    def test_a_failed_backup_keeps_the_active_release(self):
        self._apply("1.7.1")
        self._apply("1.8.0")
        ops = _Ops()
        result = rollback_package_update(
            self.runtime, restart=ops.restart, health_check=ops.health,
            before_swap=_Snapshot((False, "no space")),
        )

        self.assertFalse(result.ok)
        self.assertEqual(self.runtime.read_active()["version"], "1.8.0")
        self.assertEqual(ops.restart_calls, 0)


class _Launcher:
    """compose exec stand-in: scripted (ok, stdout, stderr) per call."""

    def __init__(self, replies, services=("web", "celery")):
        self.replies = list(replies)
        self.services = services
        self.calls = []

    def compose_config_json(self):
        return {"services": {name: {} for name in self.services}}

    def supervised_prefix(self, service, command):
        return ["python", "-m", "dlux.updater.supervisor", "--no-watch", "--"]

    def run_docker_compose(self, argv, timeout=None):
        self.calls.append(argv)
        return self.replies.pop(0)


class TakeSnapshotTests(unittest.TestCase):
    def test_runs_dlux_backup_in_celery_under_the_supervisor(self):
        reply = json.dumps({"ok": True, "token": "tok123456789", "rows": 42})
        launcher = _Launcher([(True, f"some log\n{reply}\n", "")])

        ok, detail = take_snapshot(launcher, "full")

        self.assertTrue(ok)
        self.assertIn("tok12345", detail)
        self.assertIn("42 rows", detail)
        argv = launcher.calls[0]
        self.assertEqual(argv[:3], ["exec", "-T", "celery"])
        self.assertIn("dlux.updater.supervisor", argv)
        self.assertIn("dlux_backup", argv)
        self.assertEqual(argv[argv.index("--scope") + 1], "full")

    def test_an_older_djangolux_backs_up_through_the_shell(self):
        reply = json.dumps({"ok": True, "token": "oldtok123456", "rows": 7})
        launcher = _Launcher([
            (False, "", "Unknown command: 'dlux_backup'"),
            (True, reply, ""),
        ])

        ok, _detail = take_snapshot(launcher, "data")

        self.assertTrue(ok)
        fallback = launcher.calls[1]
        self.assertIn("shell", fallback)
        script = fallback[-1]
        self.assertIn("run_system_backup", script)
        self.assertIn("media_included=False", script)
        self.assertIn("status='failed', next_attempt_at=None", script, "a failure must not leave a retry armed")

    def test_a_failed_backup_reports_why(self):
        reply = json.dumps({"ok": False, "token": "t", "error": "disk full"})
        launcher = _Launcher([(False, reply, "CommandError: The backup did not complete")])

        ok, detail = take_snapshot(launcher, "data")

        self.assertFalse(ok)
        self.assertIn("disk full", detail)

    def test_web_is_used_when_there_is_no_celery(self):
        launcher = _Launcher([(True, json.dumps({"ok": True, "token": "x"}), "")], services=("web",))
        take_snapshot(launcher, "data")
        self.assertEqual(launcher.calls[0][2], "web")

    def test_no_service_to_back_up_in(self):
        ok, detail = take_snapshot(_Launcher([], services=("db",)), "data")
        self.assertFalse(ok)
        self.assertIn("no celery or web service", detail)


class CliChoiceTests(unittest.TestCase):
    def _args(self, *argv, action="update"):
        return dlux_package_cli.parse_dlux_update_args(list(argv), action=action)

    def test_operators_back_up_by_default(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COMPOSER_OPERATION_ID", None)
            args = self._args()
            self.assertEqual(args.backup, "data")
            self.assertIsNotNone(dlux_package_cli._pre_update_backup(args))
            self.assertIsNotNone(dlux_package_cli._pre_update_backup(self._args(action="rollback")))

    def test_skip_takes_none(self):
        os.environ.pop("COMPOSER_OPERATION_ID", None)
        self.assertIsNone(dlux_package_cli._pre_update_backup(self._args("--backup", "skip")))

    def test_djangolux_handoffs_leave_it_to_djangolux(self):
        with unittest.mock.patch.dict(os.environ, {"COMPOSER_OPERATION_ID": "op-1"}):
            self.assertIsNone(dlux_package_cli._pre_update_backup(self._args()))

    def test_the_default_can_come_from_the_environment(self):
        with unittest.mock.patch.dict(os.environ, {"COMPOSER_DLUX_BACKUP": "full"}):
            self.assertEqual(self._args().backup, "full")

    def test_check_takes_no_backup_flag(self):
        self.assertEqual(self._args(action="check").backup, "skip")


if __name__ == "__main__":
    unittest.main()
