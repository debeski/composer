"""Health-gated inline DjangoLux update.

The happy path is the least interesting case. What earns this design its keep is
the failure branch: when the new release does not come back healthy, the
deployment must end up back on the previous one, with the bad release out of
reach — and when even that fails, it must say so loudly instead of pretending.
"""

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from composer.dlux_package_update import (
    apply_package_update,
    prune_releases,
    rollback_package_update,
)
from composer.dlux_runtime import DluxRuntime


class _FakeSource:
    """Stands in for PyPI: hands back an already-unpacked release directory."""

    def __init__(self, root: Path, version="1.8.0", error=None):
        self.root = root
        self.version = version
        self.error = error
        self.calls = []

    def obtain(self, target_version="", *, workdir=None, **_kwargs):
        self.calls.append(target_version)
        if self.error:
            raise self.error
        version = target_version or self.version
        unpacked = self.root / f"unpacked-{version}"
        (unpacked / "dlux").mkdir(parents=True, exist_ok=True)
        (unpacked / "dlux" / "release-manifest.json").write_text(
            json.dumps({"schema_version": 1, "version": version, "inline_safe": True}),
            encoding="utf-8",
        )
        return _Candidate(version), unpacked


class _Candidate:
    def __init__(self, version):
        self.version = version
        self.filename = f"django_lux-{version}-py3-none-any.whl"


class _Ops:
    """Scriptable restart/health pair. Each call pops the next scripted result."""

    def __init__(self, restarts=None, healths=None):
        self.restarts = list(restarts or [(True, "")])
        self.healths = list(healths or [(True, "")])
        self.restart_calls = 0
        self.health_calls = 0

    def restart(self):
        self.restart_calls += 1
        return self.restarts.pop(0) if self.restarts else (True, "")

    def health(self):
        self.health_calls += 1
        return self.healths.pop(0) if self.healths else (True, "")


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runtime = DluxRuntime(self.root / "dlux-runtime")
        self.source = _FakeSource(self.root)
        self.addCleanup(self._tmp.cleanup)

    def _stage(self, version):
        _candidate, unpacked = self.source.obtain(version)
        self.runtime.stage_release(version, unpacked)


class ApplySuccessTests(_Base):
    def test_a_healthy_update_stays_active(self):
        ops = _Ops()
        result = apply_package_update(
            self.runtime, restart=ops.restart, health_check=ops.health,
            target_version="1.8.0", source=self.source, workdir=self.root / "work",
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.version, "1.8.0")
        self.assertFalse(result.rolled_back)
        self.assertEqual(self.runtime.read_active()["version"], "1.8.0")
        self.assertEqual(ops.restart_calls, 1)

    def test_reapplying_the_active_release_is_a_no_op(self):
        ops = _Ops()
        apply_package_update(self.runtime, restart=ops.restart, health_check=ops.health,
                             target_version="1.8.0", source=self.source, workdir=self.root / "w1")
        again = apply_package_update(self.runtime, restart=ops.restart, health_check=ops.health,
                                     target_version="1.8.0", source=self.source,
                                     workdir=self.root / "w2")

        self.assertTrue(again.ok)
        self.assertIn("already the active release", again.message)
        self.assertEqual(ops.restart_calls, 1, "must not restart to re-apply what is running")


class ApplyFailureTests(_Base):
    def test_an_unhealthy_update_is_rolled_back_and_quarantined(self):
        self._stage("1.7.1")
        self.runtime.activate("1.7.1")
        ops = _Ops(restarts=[(True, ""), (True, "")], healths=[(False, "web unhealthy"), (True, "")])

        result = apply_package_update(
            self.runtime, restart=ops.restart, health_check=ops.health,
            target_version="1.8.0", source=self.source, workdir=self.root / "work",
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.rolled_back)
        self.assertFalse(result.critical)
        self.assertIn("web unhealthy", result.message)
        self.assertEqual(self.runtime.read_active()["version"], "1.7.1")
        self.assertNotIn("1.8.0", self.runtime.staged_versions())
        self.assertEqual(ops.restart_calls, 2, "restart the new release, then the restored one")

    def test_the_quarantined_release_records_why(self):
        self._stage("1.7.1")
        self.runtime.activate("1.7.1")
        ops = _Ops(restarts=[(True, ""), (True, "")], healths=[(False, "celery crashed"), (True, "")])

        apply_package_update(self.runtime, restart=ops.restart, health_check=ops.health,
                             target_version="1.8.0", source=self.source, workdir=self.root / "work")

        reason = (self.runtime.failed / "1.8.0" / "quarantine-reason.txt").read_text(encoding="utf-8")
        self.assertIn("celery crashed", reason)

    def test_a_first_ever_update_rolls_back_to_the_image_release(self):
        ops = _Ops(restarts=[(True, ""), (True, "")], healths=[(False, "boom"), (True, "")])

        result = apply_package_update(
            self.runtime, restart=ops.restart, health_check=ops.health,
            target_version="1.8.0", source=self.source, workdir=self.root / "work",
        )

        self.assertTrue(result.rolled_back)
        self.assertEqual(self.runtime.read_active(), {}, "back to the image copy")

    def test_a_failed_restart_is_treated_as_unhealthy(self):
        self._stage("1.7.1")
        self.runtime.activate("1.7.1")
        ops = _Ops(restarts=[(False, "compose restart failed"), (True, "")], healths=[(True, "")])

        result = apply_package_update(
            self.runtime, restart=ops.restart, health_check=ops.health,
            target_version="1.8.0", source=self.source, workdir=self.root / "work",
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.rolled_back)
        self.assertIn("compose restart failed", result.message)
        self.assertEqual(self.runtime.read_active()["version"], "1.7.1")

    def test_a_rollback_that_is_also_unhealthy_is_reported_as_critical(self):
        """Nothing automatic can fix this; it must not be reported as a tidy failure."""
        self._stage("1.7.1")
        self.runtime.activate("1.7.1")
        ops = _Ops(restarts=[(True, ""), (True, "")],
                   healths=[(False, "new release broke"), (False, "old release broke too")])

        result = apply_package_update(
            self.runtime, restart=ops.restart, health_check=ops.health,
            target_version="1.8.0", source=self.source, workdir=self.root / "work",
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.rolled_back)
        self.assertTrue(result.critical)
        self.assertIn("new release broke", result.message)
        self.assertIn("old release broke too", result.message)

    def test_a_fetch_failure_never_touches_the_running_deployment(self):
        self._stage("1.7.1")
        self.runtime.activate("1.7.1")
        broken = _FakeSource(self.root, error=RuntimeError("attestation invalid"))
        ops = _Ops()

        result = apply_package_update(self.runtime, restart=ops.restart, health_check=ops.health,
                                      source=broken, workdir=self.root / "work")

        self.assertFalse(result.ok)
        self.assertIn("attestation invalid", result.message)
        self.assertEqual(ops.restart_calls, 0)
        self.assertEqual(self.runtime.read_active()["version"], "1.7.1")


class RollbackTests(_Base):
    def test_rolls_back_to_the_previous_staged_release(self):
        self._stage("1.7.1")
        self._stage("1.8.0")
        self.runtime.activate("1.8.0")
        ops = _Ops()

        result = rollback_package_update(self.runtime, restart=ops.restart, health_check=ops.health)

        self.assertTrue(result.ok)
        self.assertEqual(self.runtime.read_active()["version"], "1.7.1")

    def test_rolls_back_to_the_image_when_nothing_else_is_staged(self):
        self._stage("1.8.0")
        self.runtime.activate("1.8.0")
        ops = _Ops()

        result = rollback_package_update(self.runtime, restart=ops.restart, health_check=ops.health)

        self.assertTrue(result.ok)
        self.assertEqual(self.runtime.read_active(), {})

    def test_a_two_digit_patch_is_ordered_by_number_not_by_string(self):
        """"1.8.10" sorts below "1.8.9" as a string, and did here."""
        self._stage("1.8.9")
        self._stage("1.8.10")
        self.runtime.activate("1.8.10")
        ops = _Ops()

        result = rollback_package_update(self.runtime, restart=ops.restart, health_check=ops.health)

        self.assertTrue(result.ok)
        self.assertEqual(self.runtime.read_active()["version"], "1.8.9")

    def test_a_release_above_the_active_one_is_never_a_rollback_target(self):
        """Rolling back twice must not roll forward onto what was just left."""
        self._stage("1.8.9")
        self._stage("1.8.10")
        self.runtime.activate("1.8.9")
        ops = _Ops()

        result = rollback_package_update(self.runtime, restart=ops.restart, health_check=ops.health)

        self.assertTrue(result.ok)
        self.assertEqual(self.runtime.read_active(), {}, "back to the image, not up to 1.8.10")

    def test_an_unhealthy_rollback_is_critical(self):
        self._stage("1.7.1")
        self._stage("1.8.0")
        self.runtime.activate("1.8.0")
        ops = _Ops(healths=[(False, "still broken")])

        result = rollback_package_update(self.runtime, restart=ops.restart, health_check=ops.health)

        self.assertFalse(result.ok)
        self.assertTrue(result.critical)


class PruneTests(_Base):
    def test_prunes_oldest_but_never_the_active_or_protected_release(self):
        for version in ("1.5.0", "1.6.0", "1.7.0", "1.7.1", "1.8.0"):
            self._stage(version)
        self.runtime.activate("1.8.0")

        removed = prune_releases(self.runtime, keep=2, protected=["1.7.1"])

        remaining = self.runtime.staged_versions()
        self.assertIn("1.8.0", remaining, "the active release is never pruned")
        self.assertIn("1.7.1", remaining, "a protected release is never pruned")
        self.assertTrue(removed)
        self.assertNotIn("1.5.0", remaining)

    def test_the_newest_kept_releases_are_the_newest_by_version(self):
        for version in ("1.8.8", "1.8.9", "1.8.10", "1.8.11"):
            self._stage(version)
        self.runtime.activate("1.8.11")

        prune_releases(self.runtime, keep=1)

        remaining = self.runtime.staged_versions()
        self.assertIn("1.8.11", remaining, "the active release is never pruned")
        self.assertIn("1.8.10", remaining, "the newest spare is the highest version")
        self.assertNotIn("1.8.8", remaining)


if __name__ == "__main__":
    unittest.main()
