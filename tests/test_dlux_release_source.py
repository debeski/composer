"""Fetching a DjangoLux release — every failure path must be closed.

These tests deliberately spend most of their weight on refusals rather than the
happy path: an updater that accepts an unverified, oversized, mis-digested or
path-traversing artifact is worse than one that does not run at all.
"""

import hashlib
import io
import json
import os
import sys
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from composer import dlux_release_source as source
from composer.dlux_release_source import ReleaseCandidate, ReleaseSourceError


def _wheel_bytes(
    version="1.8.0", *, manifest_version=None, inline_safe=True, manifest=None, extra=None
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("dlux/__init__.py", "")
        archive.writestr(
            "dlux/release-manifest.json",
            json.dumps(manifest or {
                "schema_version": 1,
                "version": manifest_version or version,
                "inline_safe": inline_safe,
            }),
        )
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


class _Response(io.BytesIO):
    def __init__(self, payload: bytes, url: str, headers=None):
        super().__init__(payload)
        self._url = url
        self.headers = headers or {}

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _opener_for(payload: bytes, url="https://files.pythonhosted.org/x.whl", headers=None):
    def opener(request, timeout=None):
        return _Response(payload, url, headers)
    return opener


def _index(entries) -> bytes:
    links = "".join(
        f'<a href="https://files.pythonhosted.org/{name}#sha256={digest}">{name}</a>\n'
        for name, digest in entries
    )
    return f"<html><body>{links}</body></html>".encode("utf-8")


class IndexTests(unittest.TestCase):
    def test_parses_wheels_with_digests(self):
        payload = _index([
            ("django_lux-1.7.1-py3-none-any.whl", "a" * 64),
            ("django_lux-1.8.0-py3-none-any.whl", "b" * 64),
        ])
        found = source.fetch_index(opener=_opener_for(payload, PYPI := source.PYPI_SIMPLE_URL))

        self.assertEqual([c.version for c in found], ["1.7.1", "1.8.0"])
        self.assertEqual(found[1].sha256, "b" * 64)

    def test_ignores_entries_without_a_usable_digest(self):
        payload = ('<a href="https://files.pythonhosted.org/django_lux-1.8.0-py3-none-any.whl">'
                   'django_lux-1.8.0-py3-none-any.whl</a>').encode()
        self.assertEqual(source.fetch_index(opener=_opener_for(payload, source.PYPI_SIMPLE_URL)), [])

    def test_ignores_downloads_from_unapproved_hosts(self):
        payload = ('<a href="https://evil.example.com/django_lux-1.8.0-py3-none-any.whl#sha256='
                   + "c" * 64 + '">django_lux-1.8.0-py3-none-any.whl</a>').encode()
        self.assertEqual(source.fetch_index(opener=_opener_for(payload, source.PYPI_SIMPLE_URL)), [])

    def test_refuses_an_oversized_index(self):
        huge = b"<html>" + b"x" * (source.MAX_INDEX_BYTES + 10)
        with self.assertRaises(ReleaseSourceError):
            source.fetch_index(opener=_opener_for(huge, source.PYPI_SIMPLE_URL))


class SelectionTests(unittest.TestCase):
    @staticmethod
    def _candidates(*versions):
        return [
            ReleaseCandidate(v, f"django_lux-{v}-py3-none-any.whl",
                             f"https://files.pythonhosted.org/{v}.whl", "a" * 64)
            for v in versions
        ]

    def test_picks_the_newest_stable_release(self):
        chosen = source.select_candidate(self._candidates("1.7.1", "1.8.0", "1.9.0rc1"))
        self.assertEqual(chosen.version, "1.8.0")

    def test_pins_an_explicit_target(self):
        chosen = source.select_candidate(self._candidates("1.7.1", "1.8.0"), "1.7.1")
        self.assertEqual(chosen.version, "1.7.1")

    def test_unknown_target_is_an_error_not_a_silent_fallback(self):
        with self.assertRaises(ReleaseSourceError):
            source.select_candidate(self._candidates("1.8.0"), "9.9.9")

    def test_no_releases_is_an_error(self):
        with self.assertRaises(ReleaseSourceError):
            source.select_candidate([])


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _candidate(self, payload):
        return ReleaseCandidate(
            "1.8.0", "django_lux-1.8.0-py3-none-any.whl",
            "https://files.pythonhosted.org/django_lux-1.8.0-py3-none-any.whl",
            hashlib.sha256(payload).hexdigest(),
        )

    def test_downloads_and_verifies_the_digest(self):
        payload = _wheel_bytes()
        path = source.download_wheel(self._candidate(payload), self.root / "w.whl",
                                     opener=_opener_for(payload))
        self.assertEqual(path.read_bytes(), payload)

    def test_refuses_a_wheel_whose_digest_does_not_match(self):
        payload = _wheel_bytes()
        candidate = ReleaseCandidate("1.8.0", "w.whl",
                                     "https://files.pythonhosted.org/w.whl", "f" * 64)
        with self.assertRaises(ReleaseSourceError):
            source.download_wheel(candidate, self.root / "w.whl", opener=_opener_for(payload))
        self.assertFalse((self.root / "w.whl").exists())

    def test_refuses_a_redirect_to_an_unapproved_host(self):
        payload = _wheel_bytes()
        opener = _opener_for(payload, url="https://evil.example.com/w.whl")
        with self.assertRaises(ReleaseSourceError):
            source.download_wheel(self._candidate(payload), self.root / "w.whl", opener=opener)


class AttestationTests(unittest.TestCase):
    CANDIDATE = ReleaseCandidate(
        "1.8.0", "django_lux-1.8.0-py3-none-any.whl",
        "https://files.pythonhosted.org/django_lux-1.8.0-py3-none-any.whl", "a" * 64,
    )

    def test_missing_verifier_refuses_rather_than_passes(self):
        """Fail closed: no verifier must never mean 'unsigned is fine'."""
        with patch.object(source.importlib.util, "find_spec", return_value=None):
            with self.assertRaises(ReleaseSourceError) as ctx:
                source.verify_attestation(self.CANDIDATE)
        self.assertIn("unverified", str(ctx.exception).lower())

    def test_a_failing_verification_is_refused(self):
        class _Completed:
            returncode = 1
            stdout = stderr = ""

        with patch.object(source.importlib.util, "find_spec", return_value=object()):
            with self.assertRaises(ReleaseSourceError):
                source.verify_attestation(self.CANDIDATE, runner=lambda *a, **k: _Completed())

    def test_a_passing_verification_is_accepted(self):
        class _Completed:
            returncode = 0
            stdout = stderr = ""

        with patch.object(source.importlib.util, "find_spec", return_value=object()):
            source.verify_attestation(self.CANDIDATE, runner=lambda *a, **k: _Completed())


class AssessAndUnpackTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _write(self, payload) -> Path:
        path = self.root / "w.whl"
        path.write_bytes(payload)
        return path

    @staticmethod
    def _candidate(version="1.8.0"):
        return ReleaseCandidate(version, f"django_lux-{version}-py3-none-any.whl",
                                f"https://files.pythonhosted.org/{version}.whl", "a" * 64)

    def test_accepts_an_inline_safe_release(self):
        manifest = source.assess(self._candidate(), self._write(_wheel_bytes()))
        self.assertTrue(manifest["inline_safe"])

    def test_refuses_a_release_that_requires_an_image_rebuild(self):
        """`inline_safe` is the release's call; composer honours it."""
        wheel = self._write(_wheel_bytes(inline_safe=False))
        with self.assertRaises(ReleaseSourceError) as ctx:
            source.assess(self._candidate(), wheel)
        self.assertIn("image rebuild", str(ctx.exception))

    def test_accepts_a_compatible_schema_two_release(self):
        wheel = self._write(_wheel_bytes(manifest={
            "schema_version": 2,
            "version": "1.8.0",
            "requires": {"services": {"composer": ">=1.3.8"}},
            "migrations": {
                "effect": "additive",
                "rollback_compatible": True,
            },
            "install": {"inline": "allowed"},
        }))

        manifest = source.assess(self._candidate(), wheel)

        self.assertTrue(manifest["inline_safe"])
        self.assertEqual(manifest["required_services"], {"composer": ">=1.3.8"})

    def test_refuses_an_unsafe_schema_two_migration(self):
        wheel = self._write(_wheel_bytes(manifest={
            "schema_version": 2,
            "version": "1.8.0",
            "requires": {},
            "migrations": {
                "effect": "destructive",
                "rollback_compatible": False,
            },
            "install": {"inline": "allowed"},
        }))

        with self.assertRaises(ReleaseSourceError) as ctx:
            source.assess(self._candidate(), wheel)
        self.assertIn("image rebuild", str(ctx.exception))

    def test_refuses_a_schema_two_release_requiring_newer_composer(self):
        wheel = self._write(_wheel_bytes(manifest={
            "schema_version": 2,
            "version": "1.8.0",
            "requires": {"services": {"composer": ">=9.0.0"}},
            "migrations": {
                "effect": "none",
                "rollback_compatible": True,
            },
            "install": {"inline": "allowed"},
        }))

        with self.assertRaises(ReleaseSourceError) as ctx:
            source.assess(self._candidate(), wheel)
        self.assertIn("requires Composer >=9.0.0", str(ctx.exception))

    def test_refuses_a_wheel_whose_manifest_names_another_version(self):
        wheel = self._write(_wheel_bytes(manifest_version="9.9.9"))
        with self.assertRaises(ReleaseSourceError):
            source.assess(self._candidate(), wheel)

    def test_refuses_a_wheel_without_a_manifest(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("dlux/__init__.py", "")
        with self.assertRaises(ReleaseSourceError):
            source.assess(self._candidate(), self._write(buffer.getvalue()))

    def test_unpacks_into_a_fresh_directory(self):
        target = source.unpack(self._write(_wheel_bytes()), self.root / "unpacked")
        self.assertTrue((target / "dlux" / "release-manifest.json").exists())

    def test_refuses_a_wheel_with_a_path_that_escapes_the_target(self):
        payload = _wheel_bytes(extra={"../../etc/evil": "x"})
        with self.assertRaises(ReleaseSourceError) as ctx:
            source.unpack(self._write(payload), self.root / "unpacked")
        self.assertIn("unsafe path", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
