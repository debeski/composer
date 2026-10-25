"""DjangoLux 1.15 OCR: the `ocr` service is allow-listed, startable/stoppable, and pulled with every image update."""

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from composer import agent_installer
from composer.ops import _service_table
from composer.watcher import WatchRuntime

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.dumps({"services": {
    "web": {"labels": {"org.dlux.restart": "safe"}},
    "ocr": {"labels": {"org.dlux.restart": "safe"}},
    "db": {"labels": {"org.dlux.restart": "protected"}},
}})


def _ps(ocr_state):
    return "\n".join(json.dumps(row) for row in (
        {"Service": "web", "State": "running", "Health": "healthy", "Status": "Up"},
        {"Service": "ocr", "State": ocr_state, "Health": "", "Status": ocr_state},
        {"Service": "db", "State": "running", "Health": "healthy", "Status": "Up"},
    ))


class OcrServiceTests(unittest.TestCase):
    def test_generated_blocks_allow_list_and_label_ocr(self):
        self.assertIn("ocr", agent_installer.SAFE_RESTART_CANDIDATES)
        self.assertEqual(agent_installer.RESTART_LABELS["ocr"], "safe")

    def test_ocr_can_be_stopped_when_running_and_started_when_stopped(self):
        launcher = SimpleNamespace()
        for state, can_start, can_stop in (("running", False, True), ("exited", True, False)):
            with self.subTest(state=state), \
                    patch.dict(os.environ, {"COMPOSER_AGENT_RESTART_SERVICES": "web,ocr"}):
                launcher.run_docker_compose = lambda args, timeout=None, state=state: (
                    True, CONFIG if args[0] == "config" else _ps(state), "")
                row = {r["name"]: r for r in _service_table(launcher=launcher)}["ocr"]
                self.assertTrue(row["can_restart"])
                self.assertEqual((row["can_start"], row["can_stop"]), (can_start, can_stop))
                web = {r["name"]: r for r in _service_table(launcher=launcher)}["web"]
                self.assertFalse(web["can_stop"])

    def test_the_in_app_image_update_pulls_every_service_not_only_web(self):
        with TemporaryDirectory() as tmp:
            args = SimpleNamespace(
                trigger_file=str(Path(tmp) / "image-update-request.json"), package_trigger_file=None,
                status_file=None, log_file=None, interval=2, dev=False, file=None,
                check_image=["debeski/app:latest"], availability_file=None, check_interval=900,
            )
            runtime = WatchRuntime(args)
        self.assertEqual(runtime.child[-1], "update")
        self.assertNotIn("ocr", runtime.env.get("COMPOSER_EXCLUDE_SERVICES", ""))

    def test_windows_launcher_is_ascii_so_powershell_5_cannot_misread_it(self):
        payload = (ROOT / "start.ps1").read_bytes()
        self.assertTrue(all(byte < 128 for byte in payload), "start.ps1 must stay ASCII for Windows PowerShell 5.1")
        history = json.loads((ROOT / "wrappers-history.json").read_text())["history"]
        self.assertIn("4", history["start.ps1"])


if __name__ == "__main__":
    unittest.main()
