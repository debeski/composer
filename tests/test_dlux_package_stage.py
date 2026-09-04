"""The two halves of an inline update: the agent fetches, the executor swaps.

`composer-executor` holds the Docker authority and sits on an internal network
by design, so it can never fetch a release itself. The agent stages the verified
wheel on the shared runtime volume and sends its identity over the private
socket. What matters here is that the executor's half re-checks everything it
can check locally — digest, declared version, inline safety — because the volume
it reads the bytes from is writable by `celery` too.
"""

import hashlib
import json
import os
import sys
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from composer import executor_ops, executor_protocol as proto
from composer.agent_protocol import ProtocolError
from composer.dlux_package_stage import StagedRelease, StagingError, stage_release
from composer.dlux_release_source import ReleaseSourceError
from composer.dlux_runtime import DluxRuntime


def _wheel_bytes(version="1.8.7", *, declared=None, inline_safe=True) -> bytes:
    from io import BytesIO

    manifest = {
        "schema_version": 1,
        "version": declared or version,
        "inline_safe": inline_safe,
    }
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("dlux/__init__.py", "__version__ = '%s'\n" % version)
        archive.writestr("dlux/release-manifest.json", json.dumps(manifest))
    return buffer.getvalue()


class StagedReleaseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.runtime = DluxRuntime(self._tmp.name)
        self.runtime.downloads.mkdir(parents=True, exist_ok=True)

    def _stage(self, payload=None, *, version="1.8.7", filename="django_lux-1.8.7-py3-none-any.whl"):
        payload = _wheel_bytes(version) if payload is None else payload
        (self.runtime.downloads / filename).write_bytes(payload)
        return StagedRelease(
            self.runtime, filename=filename,
            sha256=hashlib.sha256(payload).hexdigest(), version=version,
        )

    def test_a_staged_wheel_is_unpacked_without_touching_the_network(self):
        staged = self._stage()
        candidate, unpacked = staged.obtain(workdir=Path(self._tmp.name) / "work")
        self.assertEqual(candidate.version, "1.8.7")
        self.assertEqual(candidate.url, "", "nothing was fetched, so there is no source URL")
        self.assertTrue((unpacked / "dlux" / "release-manifest.json").is_file())

    def test_bytes_that_do_not_match_the_agents_digest_are_refused(self):
        """The volume is writable by celery; the digest arrived over the socket."""
        staged = self._stage()
        staged.wheel.write_bytes(_wheel_bytes("1.8.7") + b"tampered")
        with self.assertRaises(ReleaseSourceError) as raised:
            staged.obtain()
        self.assertIn("digest the agent verified", str(raised.exception))

    def test_a_wheel_that_is_not_staged_is_refused(self):
        staged = self._stage()
        staged.wheel.unlink()
        with self.assertRaises(ReleaseSourceError):
            staged.obtain()

    def test_a_wheel_declaring_another_version_is_refused(self):
        payload = _wheel_bytes("1.8.7", declared="1.9.0")
        staged = self._stage(payload)
        with self.assertRaises(ReleaseSourceError):
            staged.obtain()

    def test_a_release_that_forbids_inline_installation_is_refused(self):
        staged = self._stage(_wheel_bytes("1.8.7", inline_safe=False))
        with self.assertRaises(ReleaseSourceError) as raised:
            staged.obtain()
        self.assertIn("image rebuild", str(raised.exception))

    def test_the_requested_version_must_be_the_staged_one(self):
        staged = self._stage()
        with self.assertRaises(ReleaseSourceError):
            staged.obtain("1.9.0")

    def test_a_filename_cannot_escape_the_downloads_directory(self):
        staged = StagedRelease(
            self.runtime, filename="../../etc/django_lux-1.8.7-py3-none-any.whl",
            sha256="0" * 64, version="1.8.7",
        )
        self.assertEqual(staged.wheel.parent, self.runtime.downloads)


class StageReleaseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.runtime = DluxRuntime(self._tmp.name)
        self.runtime.state_dir.mkdir(parents=True, exist_ok=True)

    def _source(self, version="1.8.7", *, inline_safe=True):
        """A stand-in for dlux_release_source: writes the wheel it describes."""
        filename = f"django_lux-{version}-py3-none-any.whl"
        payload = _wheel_bytes(version)

        def describe(target_version="", *, workdir=None, **_kwargs):
            Path(workdir).mkdir(parents=True, exist_ok=True)
            (Path(workdir) / filename).write_bytes(payload)
            return {
                "version": version,
                "filename": filename,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "inline_safe": inline_safe,
                "reason": "" if inline_safe else f"DjangoLux {version} requires a rebuild.",
            }

        return SimpleNamespace(describe=describe)

    def test_the_wheel_lands_on_the_volume_and_its_identity_comes_back(self):
        staged = stage_release(self.runtime.root, source=self._source())
        self.assertEqual(staged["version"], "1.8.7")
        self.assertEqual(set(staged), {"version", "filename", "sha256"},
                         "the executor is told the identity and nothing else")
        wheel = self.runtime.downloads / staged["filename"]
        self.assertTrue(wheel.is_file())
        self.assertEqual(hashlib.sha256(wheel.read_bytes()).hexdigest(), staged["sha256"])

    def test_a_release_that_needs_an_image_rebuild_is_refused_here(self):
        with self.assertRaises(StagingError) as raised:
            stage_release(self.runtime.root, source=self._source(inline_safe=False))
        self.assertIn("rebuild", str(raised.exception))

    def test_older_wheels_are_pruned_but_the_new_one_is_kept(self):
        self.runtime.downloads.mkdir(parents=True, exist_ok=True)
        for old in ("django_lux-1.8.0-py3-none-any.whl", "django_lux-1.8.5-py3-none-any.whl",
                    "django_lux-1.8.6-py3-none-any.whl"):
            (self.runtime.downloads / old).write_bytes(b"old")
        staged = stage_release(self.runtime.root, source=self._source())
        remaining = sorted(item.name for item in self.runtime.downloads.glob("*.whl"))
        self.assertIn(staged["filename"], remaining)
        self.assertEqual(len(remaining), 2, "one spare is kept, the rest are dropped")

    def test_a_missing_runtime_volume_is_a_staging_error(self):
        with self.assertRaises(StagingError):
            stage_release(Path(self._tmp.name) / "absent", source=self._source())


class PackageOpProtocolTests(unittest.TestCase):
    def _request(self, op, payload):
        return {
            "protocol_version": proto.EXECUTOR_PROTOCOL_VERSION,
            "operation_id": "2f1a1c3e-4b5d-4e6f-8a9b-0c1d2e3f4a5b",
            "op": op,
            "payload": payload,
        }

    def _apply(self, **overrides):
        payload = {
            "version": "1.8.7",
            "filename": "django_lux-1.8.7-py3-none-any.whl",
            "sha256": "a" * 64,
        }
        payload.update(overrides)
        return self._request("dlux_package_apply", payload)

    def test_an_apply_request_is_accepted_and_normalized(self):
        validated = proto.validate_executor_request(self._apply(sha256="A" * 64))
        self.assertEqual(validated["payload"]["sha256"], "a" * 64)
        self.assertEqual(validated["op"], "dlux_package_apply")

    def test_a_filename_with_a_path_separator_is_rejected(self):
        with self.assertRaises(ProtocolError):
            proto.validate_executor_request(
                self._apply(filename="../releases/django_lux-1.8.7-py3-none-any.whl")
            )

    def test_a_digest_that_is_not_sha256_is_rejected(self):
        with self.assertRaises(ProtocolError):
            proto.validate_executor_request(self._apply(sha256="deadbeef"))

    def test_an_invalid_version_is_rejected(self):
        with self.assertRaises(ProtocolError):
            proto.validate_executor_request(self._apply(version="1.8.7; rm -rf /"))

    def test_an_extra_payload_field_is_rejected(self):
        request = self._apply()
        request["payload"]["url"] = "https://example.invalid/wheel.whl"
        with self.assertRaises(ProtocolError):
            proto.validate_executor_request(request)

    def test_a_rollback_takes_no_payload(self):
        validated = proto.validate_executor_request(self._request("dlux_package_rollback", {}))
        self.assertEqual(validated["payload"], {})
        with self.assertRaises(ProtocolError):
            proto.validate_executor_request(
                self._request("dlux_package_rollback", {"version": "1.8.7"})
            )


class PackageOpHandlerTests(unittest.TestCase):
    def _handle(self, op, payload, exit_code=0):
        request = proto.validate_executor_request({
            "protocol_version": proto.EXECUTOR_PROTOCOL_VERSION,
            "operation_id": "2f1a1c3e-4b5d-4e6f-8a9b-0c1d2e3f4a5b",
            "op": op,
            "payload": payload,
        })
        with patch.object(executor_ops, "_run", return_value=(exit_code, "")) as run:
            result = executor_ops.default_operation_handler(request)
        return result, (run.call_args[0][0] if run.call_args else [])

    def test_an_apply_runs_the_offline_staged_install(self):
        result, argv = self._handle("dlux_package_apply", {
            "version": "1.8.7",
            "filename": "django_lux-1.8.7-py3-none-any.whl",
            "sha256": "a" * 64,
        })
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(argv[1:5], ["-m", "composer", "dlux-update", "apply"])
        self.assertIn("--staged-wheel", argv)
        self.assertEqual(argv[argv.index("--staged-wheel") + 1],
                         "django_lux-1.8.7-py3-none-any.whl")
        self.assertEqual(argv[argv.index("--staged-sha256") + 1], "a" * 64)

    def test_needs_a_human_survives_the_socket(self):
        """Exit 3 is 'rollback also unhealthy'; a caller must not retry it."""
        result, _argv = self._handle("dlux_package_rollback", {}, exit_code=3)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["exit_code"], 3)

    def test_a_rollback_needs_no_staged_release(self):
        _result, argv = self._handle("dlux_package_rollback", {})
        self.assertEqual(argv[1:5], ["-m", "composer", "dlux-update", "rollback"])
        self.assertNotIn("--staged-wheel", argv)


if __name__ == "__main__":
    unittest.main()
