"""Composer's own image is pinned to a digest its release job signed.

A compromised registry tag must not be able to swap the deployer or the resident
pair. These tests defend each step: which version is looked up, which signature
is accepted, when a pin may move, and what the wrapper and the block run.
"""

import functools
import io
import shutil
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from composer import agent_installer, channel_config, image_pin
from composer.agent_installer import COMPOSER_AGENT_END, COMPOSER_AGENT_START

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
REF_10 = f"debeski/composer:v1.11.0@{DIGEST_A}"


def _statement(version="1.11.0", digest=DIGEST_A, **override):
    document = {
        "schema": 1,
        "image": "debeski/composer",
        "version": version,
        "tag": f"v{version}",
        "digest": digest,
        "prerelease": image_pin.versions.is_prerelease(version),
    }
    document.update(override)
    return json.dumps(document).encode()


class _Proc:
    def __init__(self, code, stderr=""):
        self.returncode, self.stdout, self.stderr = code, "", stderr


class _Fetch:
    """Serves the two release assets, and records what was asked for."""

    def __init__(self, statement=None, bundle=b"{}", error=None):
        self.statement, self.bundle, self.error, self.urls = statement, bundle, error, []

    def __call__(self, url, limit):
        self.urls.append(url)
        if self.error:
            raise self.error
        return self.bundle if url.endswith(image_pin.BUNDLE_NAME) else self.statement


class LockTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_a_written_lock_reads_back_as_the_same_pin(self):
        pin = image_pin.Pin(version="1.11.0", digest=DIGEST_A)
        image_pin.write_lock(pin, self.project)
        self.assertEqual(image_pin.read_lock(self.project).ref, REF_10)
        self.assertEqual((self.project / ".composer-image").read_text(), REF_10 + "\n")

    def test_a_malformed_lock_reads_as_no_lock(self):
        # The wrapper applies the same pattern: both fall back to the channel tag.
        for text in ("debeski/composer:latest", f"evil/composer:v1.11.0@{DIGEST_A}",
                     f"debeski/composer:v1.11.0@sha256:{'a' * 63}", "", "\x00"):
            (self.project / ".composer-image").write_text(text, encoding="utf-8")
            self.assertIsNone(image_pin.read_lock(self.project), text)

    def test_the_lock_beats_the_channel_and_the_explicit_pin_beats_the_lock(self):
        channel_config.write_channel("beta", self.project)
        self.assertEqual(channel_config.resolve_self_image(self.project, env={}), "debeski/composer:beta")
        image_pin.write_lock(image_pin.Pin("1.11.0", DIGEST_A), self.project)
        self.assertEqual(channel_config.resolve_self_image(self.project, env={}), REF_10)
        env = {"COMPOSER_SELF_IMAGE": "debeski/composer:v1.9.0"}
        self.assertEqual(channel_config.resolve_self_image(self.project, env=env), "debeski/composer:v1.9.0")


class ChannelVersionTests(unittest.TestCase):
    def test_stable_reads_only_latest(self):
        asked = []
        version = image_pin.channel_version(
            "stable", label_reader=lambda ref: asked.append(ref) or "1.11.0")
        self.assertEqual(version, "1.11.0")
        self.assertEqual(asked, ["debeski/composer:latest"])

    def test_beta_takes_the_newer_of_beta_and_latest(self):
        labels = {"debeski/composer:beta": "1.11.0b2", "debeski/composer:latest": "1.11.0"}
        self.assertEqual(image_pin.channel_version("beta", label_reader=labels.get), "1.11.0")

    def test_stable_refuses_a_prerelease_behind_latest(self):
        with self.assertRaises(image_pin.PinError) as caught:
            image_pin.channel_version("stable", label_reader=lambda ref: "1.12.0b1")
        self.assertFalse(caught.exception.unavailable)

    def test_an_unreadable_registry_is_unavailable(self):
        with self.assertRaises(image_pin.PinError) as caught:
            image_pin.channel_version("stable", label_reader=lambda ref: None)
        self.assertTrue(caught.exception.unavailable)


class VerifiedPinTests(unittest.TestCase):
    def test_the_bundle_is_checked_against_the_release_workflow_on_that_exact_tag(self):
        calls = []
        fetch = _Fetch(_statement())
        pin = image_pin.verified_pin(
            "1.11.0", fetch=fetch, available=lambda: True,
            runner=lambda argv, **kw: calls.append(argv) or _Proc(0),
        )
        self.assertEqual(pin.ref, REF_10)
        self.assertEqual(fetch.urls, [
            "https://github.com/debeski/composer/releases/download/v1.11.0/composer-image.json",
            "https://github.com/debeski/composer/releases/download/v1.11.0/composer-image.json.sigstore.json",
        ])
        argv = calls[0]
        self.assertEqual(argv[1:5], ["-m", "sigstore", "verify", "identity"])
        self.assertIn(
            "https://github.com/debeski/composer/.github/workflows/release.yml@refs/tags/v1.11.0", argv)
        self.assertIn("https://token.actions.githubusercontent.com", argv)
        self.assertNotIn("--offline", argv)

    def test_a_sigstore_outage_falls_back_to_offline_verification(self):
        calls = []

        def runner(argv, **kw):
            calls.append(argv)
            return _Proc(0 if "--offline" in argv else 1, "TUF fetch failed")

        pin = image_pin.verified_pin("1.11.0", fetch=_Fetch(_statement()), available=lambda: True, runner=runner)
        self.assertEqual(pin.digest, DIGEST_A)
        self.assertEqual(len(calls), 2)

    def test_a_signature_that_fails_is_invalid_not_unavailable(self):
        with self.assertRaises(image_pin.PinError) as caught:
            image_pin.verified_pin("1.11.0", fetch=_Fetch(_statement()), available=lambda: True,
                                   runner=lambda argv, **kw: _Proc(1, "certificate identity mismatch"))
        self.assertFalse(caught.exception.unavailable)
        self.assertIn("certificate identity mismatch", str(caught.exception))

    def test_missing_sigstore_is_unavailable(self):
        with self.assertRaises(image_pin.PinError) as caught:
            image_pin.verified_pin("1.11.0", fetch=_Fetch(_statement()), available=lambda: False,
                                   runner=lambda argv, **kw: _Proc(0))
        self.assertTrue(caught.exception.unavailable)

    def test_a_statement_for_another_release_is_refused_even_when_signed(self):
        # A genuine statement replayed under another version's URL.
        for statement in (
            _statement(version="1.10.4"),
            _statement(tag="v1.10.4"),
            _statement(image="debeski/other"),
            _statement(prerelease=True),
            _statement(digest="sha256:short"),
            b"not json",
            json.dumps({"schema": 2}).encode(),
        ):
            with self.assertRaises(image_pin.PinError, msg=statement):
                image_pin.verified_pin("1.11.0", fetch=_Fetch(statement), available=lambda: True,
                                       runner=lambda argv, **kw: _Proc(0))


class FetchTests(unittest.TestCase):
    def test_a_redirect_off_github_is_refused(self):
        handler = image_pin._AssetRedirects()
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/asset")
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(None, None, 302, "Found", {}, "http://objects.githubusercontent.com/x")

    def test_a_missing_asset_is_unavailable(self):
        class Opener:
            def open(self, request, timeout):
                raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

        with self.assertRaises(image_pin.PinError) as caught:
            image_pin.fetch_asset("https://github.com/x", 10, opener=Opener())
        self.assertTrue(caught.exception.unavailable)

    def test_an_oversized_asset_is_refused(self):
        class Response(io.BytesIO):
            def __enter__(self):
                return self

        class Opener:
            def open(self, request, timeout):
                return Response(b"x" * 11)

        with self.assertRaises(image_pin.PinError) as caught:
            image_pin.fetch_asset("https://github.com/x", 10, opener=Opener())
        self.assertFalse(caught.exception.unavailable)


class ResolveTests(unittest.TestCase):
    def _resolve(self, channel="stable", label="1.11.0", pin_reader=None, **kwargs):
        return image_pin.resolve(
            channel,
            label_reader=lambda ref: label,
            pin_reader=pin_reader or (lambda version: image_pin.Pin(version, DIGEST_A)),
            **kwargs,
        )

    def test_a_new_release_is_pinned_by_its_signed_digest(self):
        resolution = self._resolve(current=image_pin.Pin("1.10.4", DIGEST_B))
        self.assertTrue(resolution.changed)
        self.assertEqual(resolution.pin.ref, REF_10)
        self.assertTrue(resolution.pin.verified)

    def test_the_same_version_keeps_the_current_pin_and_fetches_nothing(self):
        current = image_pin.Pin("1.11.0", DIGEST_B)
        resolution = self._resolve(current=current, pin_reader=lambda v: self.fail("fetched"))
        self.assertFalse(resolution.changed)
        self.assertEqual(resolution.pin, current)

    def test_a_tag_pointed_back_at_an_old_release_never_moves_the_pin(self):
        current = image_pin.Pin("1.11.1", DIGEST_B)
        resolution = self._resolve(current=current, pin_reader=lambda v: self.fail("fetched"))
        self.assertFalse(resolution.changed)
        self.assertEqual(resolution.pin, current)
        self.assertIn("older than the pinned 1.11.1", resolution.note)

    def test_an_explicit_downgrade_is_allowed(self):
        resolution = self._resolve(current=image_pin.Pin("1.11.1", DIGEST_B), allow_downgrade=True)
        self.assertEqual(resolution.pin.version, "1.11.0")

    def test_the_escape_never_overrides_a_failed_signature(self):
        def invalid(version):
            raise image_pin.PinError("bad signature")

        with self.assertRaises(image_pin.PinError):
            self._resolve(pin_reader=invalid, allow_unverified=True,
                          digest_reader=lambda ref: self.fail("fell back"))

    def test_the_escape_pins_the_release_tags_registry_digest_when_github_is_down(self):
        def down(version):
            raise image_pin.PinError("GitHub could not be reached", unavailable=True)

        asked = []
        resolution = self._resolve(
            pin_reader=down, allow_unverified=True,
            digest_reader=lambda ref: (asked.append(ref) or DIGEST_B, ""),
        )
        self.assertEqual(asked, ["debeski/composer:v1.11.0"], "the version's own tag, never :latest")
        self.assertFalse(resolution.pin.verified)
        self.assertIn("UNVERIFIED", resolution.note)

    def test_without_the_escape_an_outage_refuses(self):
        def down(version):
            raise image_pin.PinError("GitHub could not be reached", unavailable=True)

        with self.assertRaises(image_pin.PinError):
            self._resolve(pin_reader=down)

    def test_the_environment_form_of_the_escape(self):
        self.assertTrue(image_pin.allow_unverified_from_env({"COMPOSER_ALLOW_UNVERIFIED_COMPOSER": "1"}))
        self.assertFalse(image_pin.allow_unverified_from_env({}))


BLOCK = f"""name: demo
services:
{COMPOSER_AGENT_START}
  docker-socket-proxy:
    image: tecnativa/docker-socket-proxy:latest
  composer-agent:
    image: debeski/composer:latest
  composer-executor:
    image: debeski/composer:latest
{COMPOSER_AGENT_END}
  web:
    image: demo:latest
"""


def _compose_ok(argv, **kwargs):
    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


class PinBlockTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        (self.project / "compose.yml").write_text(BLOCK, encoding="utf-8")
        which = patch("composer.agent_installer.shutil.which", return_value="/usr/bin/docker")
        which.start()
        self.addCleanup(which.stop)

    def _pin(self, apply):
        return agent_installer.pin_composer_image(
            str(self.project), pin=image_pin.Pin("1.11.0", DIGEST_A), apply=apply,
            command_runner=_compose_ok,
        )

    def test_the_block_and_the_lock_move_together(self):
        outcome = self._pin(apply=True)
        self.assertEqual(outcome["files"], ["compose.yml"])
        contents = (self.project / "compose.yml").read_text(encoding="utf-8")
        self.assertEqual(agent_installer.composer_images_in_block(contents), [REF_10, REF_10])
        self.assertIn("image: demo:latest", contents)
        self.assertEqual(image_pin.read_lock(self.project).ref, REF_10)

    def test_a_dry_run_writes_nothing(self):
        outcome = self._pin(apply=False)
        self.assertEqual(outcome["files"], ["compose.yml"])
        self.assertIsNone(image_pin.read_lock(self.project))
        self.assertEqual((self.project / "compose.yml").read_text(encoding="utf-8"), BLOCK)

    def test_a_pinned_block_is_read_back_with_its_digest(self):
        self._pin(apply=True)
        again = self._pin(apply=False)
        self.assertEqual(again["files"], [], "a pinned block is already what the pin wants")
        self.assertFalse(again["lock_changed"])

    def test_a_project_without_compose_gets_the_lock_only(self):
        (self.project / "compose.yml").unlink()
        outcome = self._pin(apply=True)
        self.assertEqual(outcome["files"], [])
        self.assertEqual(image_pin.read_lock(self.project).ref, REF_10)

    def test_update_pin_follows_the_channel_and_the_current_lock(self):
        channel_config.write_channel("beta", self.project)
        image_pin.write_lock(image_pin.Pin("1.11.0b1", DIGEST_B), self.project)
        seen = {}

        def resolver(channel, **kwargs):
            seen.update(kwargs, channel=channel)
            return image_pin.Resolution(pin=image_pin.Pin("1.11.0b2", DIGEST_A), current=kwargs["current"],
                                        changed=True)

        with patch("composer.agent_installer.pin_composer_image",
                   functools.partial(agent_installer.pin_composer_image, command_runner=_compose_ok)):
            image_pin.update_pin(str(self.project), env={}, resolver=resolver)
        self.assertEqual(seen["channel"], "beta")
        self.assertEqual(seen["current"].version, "1.11.0b1")
        self.assertEqual(image_pin.read_lock(self.project).version, "1.11.0b2")

    def test_update_pin_never_resolves_over_an_explicit_pin(self):
        with self.assertRaises(image_pin.PinError):
            image_pin.update_pin(str(self.project), env={"COMPOSER_SELF_IMAGE": "x"},
                                 resolver=lambda *a, **k: self.fail("resolved"))


class CheckupPinTests(unittest.TestCase):
    """`check` reports the pin offline; `check --fix` pins only to an existing lock."""

    def setUp(self):
        from composer.launcher import DockerComposeLauncher

        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        (self.project / "compose.yml").write_text(BLOCK, encoding="utf-8")
        previous = os.getcwd()
        os.chdir(self.project)
        self.addCleanup(os.chdir, previous)
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("COMPOSER_SELF_IMAGE", None)
        self.launcher = DockerComposeLauncher()
        self.launcher.compose_file = None

    def _finding(self):
        return self.launcher._check_composer_channel()

    def test_a_fresh_unpinned_project_is_ok(self):
        # The state `dlux startproject` writes: the scaffold alignment gate needs it clean.
        result = self._finding()
        self.assertEqual(result["level"], "ok")
        self.assertIn("Not pinned yet", result["message"])

    def test_a_lock_the_block_does_not_follow_warns(self):
        image_pin.write_lock(image_pin.Pin("1.11.0", DIGEST_A), self.project)
        result = self._finding()
        self.assertEqual(result["level"], "warn")
        self.assertIn("check --fix", result["fix"])

    def test_a_beta_pin_on_the_stable_channel_warns(self):
        image_pin.write_lock(image_pin.Pin("1.11.0b1", DIGEST_A), self.project)
        result = self._finding()
        self.assertEqual(result["level"], "warn")
        self.assertIn("check --stable", result["fix"])

    def test_fix_pins_the_block_to_the_lock_and_the_finding_clears(self):
        image_pin.write_lock(image_pin.Pin("1.11.0", DIGEST_A), self.project)
        with patch("composer.agent_installer.shutil.which", return_value="/usr/bin/docker"), \
             patch("composer.agent_installer.pin_composer_image",
                   functools.partial(agent_installer.pin_composer_image, command_runner=_compose_ok)):
            fixed = self.launcher._fix_composer_pin(SimpleNamespace(file=None, yes=True))
        self.assertEqual([f["level"] for f in fixed], ["ok"])
        self.assertEqual(self._finding()["level"], "ok")
        self.assertIn("pinned to Composer 1.11.0", self._finding()["message"])

    def test_fix_without_a_lock_changes_nothing(self):
        self.assertEqual(self.launcher._fix_composer_pin(SimpleNamespace(file=None, yes=True)), [])
        self.assertEqual((self.project / "compose.yml").read_text(encoding="utf-8"), BLOCK)

    def test_the_repair_preview_shows_the_pin_only_when_a_lock_exists(self):
        from composer.ops import _composer_pin_repair

        self.assertEqual(_composer_pin_repair(self.launcher), [])
        image_pin.write_lock(image_pin.Pin("1.11.0", DIGEST_A), self.project)
        repairs = _composer_pin_repair(self.launcher)
        self.assertEqual(repairs[0]["files"], ["compose.yml"])
        self.assertIn(REF_10, repairs[0]["diff"])

    def test_a_channel_switch_pins_the_verified_release(self):
        resolution = image_pin.Resolution(pin=image_pin.Pin("1.11.0b2", DIGEST_A), current=None, changed=True)
        with patch("composer.image_pin.resolve", return_value=resolution) as resolve, \
             patch("composer.agent_installer.shutil.which", return_value="/usr/bin/docker"), \
             patch("composer.agent_installer.switch_composer_channel",
                   functools.partial(agent_installer.switch_composer_channel, command_runner=_compose_ok)):
            outcome = self.launcher._switch_channel(SimpleNamespace(file=None, yes=True), "beta")
        self.assertEqual(outcome[0]["level"], "ok", outcome)
        self.assertTrue(resolve.call_args.kwargs["allow_downgrade"], "a switch is a deliberate move")
        pinned = f"debeski/composer:v1.11.0b2@{DIGEST_A}"
        self.assertEqual(image_pin.read_lock(self.project).ref, pinned)
        self.assertEqual(channel_config.read_channel(self.project), "beta")
        self.assertEqual(agent_installer.composer_images_in_block(
            (self.project / "compose.yml").read_text(encoding="utf-8")), [pinned, pinned])

    def test_a_channel_switch_that_cannot_be_verified_changes_nothing(self):
        with patch("composer.image_pin.resolve", side_effect=image_pin.PinError("bad signature")):
            outcome = self.launcher._switch_channel(SimpleNamespace(file=None, yes=True), "beta")
        self.assertEqual(outcome[0]["level"], "fail")
        self.assertEqual(channel_config.read_channel(self.project), "stable")
        self.assertIsNone(image_pin.read_lock(self.project))


class AgentUpdatePinTests(unittest.TestCase):
    def test_agent_update_pins_before_it_pulls_and_stops_when_it_cannot(self):
        from composer.launcher import DockerComposeLauncher

        launcher = DockerComposeLauncher()
        with patch.object(launcher, "_resident_pair_scope", return_value=["composer-agent"]):
            launcher.configure_agent_update(["--allow-unverified-composer"])
        self.assertEqual(launcher.pin_resident, {"allow_downgrade": False, "allow_unverified": True})
        with patch("composer.image_pin.update_pin", side_effect=image_pin.PinError("older than the pinned")), \
             patch.dict(os.environ, {"COMPOSER_SELF_IMAGE": ""}):
            ok, detail = launcher.pin_resident_pair()
        self.assertFalse(ok)
        self.assertIn("older than the pinned", detail)

    def test_the_executor_helper_can_write_the_pin(self):
        from composer import executor_ops

        captured = {}

        def capture(argv):
            captured["argv"] = argv
            return True, "id", ""

        with patch.object(executor_ops, "_capture", side_effect=capture), \
             patch("composer.dlux_runtime_access.self_image", return_value="sha256:image"):
            executor_ops._run_agent_update("op-1", {"token": "t"})
        argv = captured["argv"]
        here = os.getcwd()
        self.assertIn(f"{here}:{here}:rw", argv)
        self.assertGreater(argv.index(f"{here}:{here}:rw"), argv.index("--volumes-from"),
                           "the rw mount must come after, and so replace, the inherited read-only one")


if __name__ == "__main__":
    unittest.main()


class BootstrapTests(unittest.TestCase):
    """The pin moves only when there is something to move to (1.11.0b1 smoke test).

    The 1.11.0b1 candidate's own smoke test ran `agent update` while the stable
    channel still advertised 1.10.4, which predates signed releases. Nothing was
    newer than the Composer doing the update, yet it refused because 1.10.4 had
    no statement. A real update still verifies, never moves back, fails closed.
    """

    def _resolve(self, label, *, current=None, running="1.11.0b1", pin_reader=None, **kwargs):
        fetched = []

        def reader(version):
            fetched.append(version)
            if pin_reader:
                return pin_reader(version)
            return image_pin.Pin(version, DIGEST_A)

        resolution = image_pin.resolve("stable" if not image_pin.versions.is_prerelease(str(label))
                                       else "beta", current=current, running=running,
                                       label_reader=lambda ref: label, pin_reader=reader, **kwargs)
        return resolution, fetched

    def test_a_channel_older_than_the_running_composer_is_nothing_to_update(self):
        resolution, fetched = self._resolve("1.10.4")
        self.assertFalse(resolution.changed)
        self.assertIsNone(resolution.pin)
        self.assertEqual(fetched, [], "nothing to update: no statement is needed")
        self.assertIn("newer than", resolution.note)

    def test_a_channel_older_than_the_pin_is_nothing_to_update_not_an_error(self):
        current = image_pin.Pin("1.11.0b2", DIGEST_B)
        resolution, fetched = self._resolve("1.11.0b1", current=current, running="1.11.0b2")
        self.assertFalse(resolution.changed)
        self.assertEqual(resolution.pin, current)
        self.assertEqual(fetched, [])
        self.assertIn("never moves a pin backwards", resolution.note)

    def test_an_unreadable_channel_changes_nothing_and_says_so(self):
        resolution = image_pin.resolve("stable", running="1.11.0b1", label_reader=lambda ref: None,
                                       pin_reader=lambda v: self.fail("fetched"))
        self.assertFalse(resolution.changed)
        self.assertIn("could not be read", resolution.note)

    def test_an_unreadable_channel_still_fails_an_explicit_switch(self):
        with self.assertRaises(image_pin.PinError):
            image_pin.resolve("beta", running="1.11.0b1", allow_downgrade=True, label_reader=lambda ref: None)

    def test_a_newer_release_without_a_statement_still_fails_closed(self):
        def missing(version):
            raise image_pin.PinError("not published", unavailable=True)

        with self.assertRaises(image_pin.PinError):
            self._resolve("1.11.1", running="1.11.0", pin_reader=missing)

    def test_a_newer_release_that_fails_its_signature_still_fails(self):
        def forged(version):
            raise image_pin.PinError("bad signature")

        with self.assertRaises(image_pin.PinError):
            self._resolve("1.11.1", running="1.11.0", pin_reader=forged)

    def test_the_running_release_is_pinned_when_it_verifies(self):
        resolution, fetched = self._resolve("1.11.0", running="1.11.0")
        self.assertTrue(resolution.changed)
        self.assertEqual(resolution.pin.version, "1.11.0")
        self.assertEqual(fetched, ["1.11.0"])

    def test_the_running_release_that_cannot_be_verified_yet_changes_nothing(self):
        def missing(version):
            raise image_pin.PinError("GitHub could not be reached", unavailable=True)

        resolution, _ = self._resolve("1.11.0", running="1.11.0", pin_reader=missing)
        self.assertFalse(resolution.changed)
        self.assertIsNone(resolution.pin)
        self.assertIn("GitHub could not be reached", resolution.note)

    def test_the_running_release_with_a_forged_statement_fails(self):
        def forged(version):
            raise image_pin.PinError("bad signature")

        with self.assertRaises(image_pin.PinError):
            self._resolve("1.11.0", running="1.11.0", pin_reader=forged)

    def test_a_switch_to_a_release_before_signing_says_why(self):
        with self.assertRaises(image_pin.PinError) as caught:
            self._resolve("1.10.4", allow_downgrade=True, pin_reader=lambda v: self.fail("fetched"))
        self.assertIn("predates signed releases", str(caught.exception))
        self.assertTrue(caught.exception.unavailable, "the escape may pin its registry digest")

    def test_update_pin_changes_nothing_when_there_is_nothing_to_update(self):
        with tempfile.TemporaryDirectory() as holder:
            project = Path(holder)
            (project / "compose.yml").write_text(BLOCK, encoding="utf-8")
            resolution, outcome = image_pin.update_pin(
                str(project), env={},
                resolver=functools.partial(image_pin.resolve, label_reader=lambda ref: "1.10.4"),
                running="1.11.0b1",
            )
            self.assertIsNone(image_pin.read_lock(project))
            self.assertEqual((project / "compose.yml").read_text(encoding="utf-8"), BLOCK)
            self.assertEqual(outcome["files"], [])


class AgentUpdateSmokeTests(unittest.TestCase):
    """scripts/smoke-test.sh's `agent update` on a candidate build: read-only fixture, no statement."""

    def test_a_candidate_newer_than_its_channel_updates_the_pair_without_pinning(self):
        from composer.launcher import DockerComposeLauncher

        holder = tempfile.mkdtemp()
        previous = os.getcwd()
        self.addCleanup(shutil.rmtree, holder, True)
        self.addCleanup(os.chmod, holder, 0o755)
        self.addCleanup(os.chdir, previous)
        os.chdir(holder)
        Path("agent-lifecycle-compose.yml").write_text(
            "name: smoke\nservices:\n  composer-agent:\n    image: busybox:1.36\n", encoding="utf-8")
        os.chmod(holder, 0o555)
        launcher = DockerComposeLauncher()
        with patch.object(launcher, "_resident_pair_scope", return_value=["composer-agent"]):
            launcher.configure_agent_update(["-f", "agent-lifecycle-compose.yml"])
        with patch("composer.registry.remote_image_version", return_value="1.10.4"), \
             patch("composer.version.read_composer_version", return_value="1.11.0b1"), \
             patch("composer.image_pin.verified_pin", side_effect=AssertionError("needs no statement")), \
             patch.dict(os.environ, {"COMPOSER_SELF_IMAGE": ""}), \
             patch("sys.stdout", new_callable=io.StringIO) as out:
            ok, detail = launcher.pin_resident_pair()
        self.assertTrue(ok, detail)
        self.assertIn("nothing to pin", out.getvalue())
