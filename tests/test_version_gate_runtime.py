"""The version gate and a DjangoLux deployment's own runtime release.

The gate blocks a target image older than the active version because old code
against a forward-migrated schema cannot be undone. A DjangoLux deployment
carries its active release on the runtime volume, so an image baking an older
version does not by itself downgrade anything — `dlux_image_gate` says which
case this is. Anything that cannot answer must keep blocking.
"""
import json
import unittest
from typing import List, Optional, Tuple

from composer.version_gate import VersionGateMixin


class _Gate(VersionGateMixin):
    def __init__(self, *, verdict=None, exec_ok=True, force=False):
        self.active_version_file = "/opt/dlux-runtime/state/active.json"
        self.active_version_key = "version"
        self.version_label = "org.demo.dlux_baked_version"
        self.force = force
        self.runtime_gate_service = None
        self.gate_runtime_verdict = None
        self.gate_images: List[str] = []
        self.gate_target_version: Optional[str] = None
        self.gate_active_version: Optional[str] = None
        self._verdict = verdict
        self._exec_ok = exec_ok
        self.exec_calls: List[List[str]] = []

    # --- seams the mixin calls out through -------------------------------
    def read_active_version(self):
        return "1.8.11"

    def compose_config_images(self):
        return ["registry.example/demo:latest"]

    def image_label_version(self, image):
        return "1.8.6"

    def run_docker_compose(self, args, timeout=None) -> Tuple[bool, str, str]:
        self.exec_calls.append(list(args))
        if not self._exec_ok:
            return False, "", "no such service"
        if self._verdict is None:
            return True, "not json at all", ""
        return True, "Some compose noise\n" + json.dumps(self._verdict), ""


class RuntimeVerdictGateTests(unittest.TestCase):
    def test_a_keep_verdict_lets_an_older_image_through(self):
        gate = _Gate(verdict={"verdict": "keep", "reason": "The active release stays."})
        ok, message = gate.preflight_version_gate()
        self.assertTrue(ok)
        self.assertIn("keeps its active release", message)
        self.assertIn("The active release stays.", message)
        self.assertEqual(gate.gate_runtime_verdict, "keep")

    def test_an_abort_verdict_still_blocks_and_explains(self):
        gate = _Gate(verdict={"verdict": "abort", "reason": "Needs the v1.2.7 image."})
        ok, message = gate.preflight_version_gate()
        self.assertFalse(ok)
        self.assertIn("OLDER than the active", message)
        self.assertIn("Needs the v1.2.7 image.", message)

    def test_a_deployment_that_cannot_answer_still_blocks(self):
        # No such command / service down: the gate must not fail open.
        gate = _Gate(exec_ok=False)
        ok, message = gate.preflight_version_gate()
        self.assertFalse(ok)
        self.assertIn("--force to override", message)
        self.assertIsNone(gate.gate_runtime_verdict)

    def test_unparseable_output_still_blocks(self):
        gate = _Gate(verdict=None)
        self.assertFalse(gate.preflight_version_gate()[0])

    def test_force_still_wins_without_asking(self):
        gate = _Gate(force=True)
        ok, message = gate.preflight_version_gate()
        self.assertTrue(ok)
        self.assertIn("--force", message)
        self.assertEqual(gate.exec_calls, [], "force must not need the verdict")

    def test_the_verdict_is_asked_of_the_configured_service(self):
        gate = _Gate(verdict={"verdict": "keep"})
        gate.runtime_gate_service = "celery"
        gate.preflight_version_gate()
        self.assertEqual(len(gate.exec_calls), 1)
        argv = gate.exec_calls[0]
        self.assertEqual(argv[:3], ["exec", "-T", "celery"])
        self.assertIn("dlux_image_gate", argv)
        self.assertEqual(argv[-2:], ["--baked-dlux-version", "1.8.6"])

    def test_web_is_the_default_service(self):
        gate = _Gate(verdict={"verdict": "keep"})
        gate.preflight_version_gate()
        self.assertEqual(gate.exec_calls[0][:3], ["exec", "-T", "web"])


if __name__ == "__main__":
    unittest.main()
