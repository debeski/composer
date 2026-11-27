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
import unittest.mock
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

    def test_a_release_before_pinning_never_reaches_the_lock_or_the_block(self):
        for apply in (False, True):
            with self.subTest(apply=apply), self.assertRaises(agent_installer.AgentInstallError) as caught:
                agent_installer.pin_composer_image(
                    str(self.project), pin=image_pin.Pin("1.10.4", DIGEST_A, verified=False), apply=apply,
                    command_runner=lambda *a, **k: self.fail("validated"),
                )
            self.assertIn("cannot run a pinned stack", str(caught.exception))
        with self.assertRaises(agent_installer.AgentInstallError):
            agent_installer.switch_composer_channel(
                str(self.project), channel="stable", apply=True, pin=image_pin.Pin("1.10.4", DIGEST_A),
                command_runner=lambda *a, **k: self.fail("validated"),
            )
        self.assertIsNone(image_pin.read_lock(self.project))
        self.assertEqual(channel_config.read_channel(self.project), "stable")
        self.assertEqual((self.project / "compose.yml").read_text(encoding="utf-8"), BLOCK)

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
        self.running = {}
        # No Docker: what runs is given per test.
        for name, value in (("_resident_running_images", None), ("compose_config_json", {}), ("_container_states", []),
                            ("_pin_image_version", "")):
            stub = (patch.object(self.launcher, name, side_effect=lambda: dict(self.running), create=True)
                    if value is None else patch.object(self.launcher, name, return_value=value))
            stub.start()
            self.addCleanup(stub.stop)

    def _finding(self):
        return self.launcher._check_composer_channel()

    def _pin_block(self, pin):
        with patch("composer.agent_installer.shutil.which", return_value="/usr/bin/docker"):
            agent_installer.pin_composer_image(str(self.project), pin=pin, apply=True, command_runner=_compose_ok)

    def test_a_pair_still_running_the_old_image_warns(self):
        # O4: the block and the lock named the new pin, `check` said OK, and the pair ran the old image.
        pin = image_pin.Pin("1.11.0", DIGEST_A)
        self._pin_block(pin)
        self.running = {"composer-agent": pin.ref, "composer-executor": "debeski/composer:latest"}
        result = self._finding()
        self.assertEqual(result["level"], "warn", result)
        self.assertIn("composer-executor still runs debeski/composer:latest", result["message"])
        self.assertIn("./start.sh", result["fix"])
        self.running = {"composer-agent": pin.ref, "composer-executor": pin.ref}
        self.assertEqual(self._finding()["level"], "ok")

    def test_a_lock_from_before_pinning_warns_with_the_way_out(self):
        pin = image_pin.Pin("1.10.4", DIGEST_A, verified=False)
        image_pin.write_lock(pin, self.project)
        result = self._finding()
        self.assertEqual(result["level"], "warn", result)
        self.assertIn("predates digest pinning", result["message"])
        self.assertIn("COMPOSER_SELF_IMAGE=debeski/composer:beta ./start.sh check --beta -y", result["fix"])
        fixed = self.launcher._fix_composer_pin(SimpleNamespace(file=None, yes=True))
        self.assertEqual([f["level"] for f in fixed], ["warn"], fixed)
        self.assertEqual((self.project / "compose.yml").read_text(encoding="utf-8"), BLOCK)

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

    def test_a_lock_whose_tag_and_digest_disagree_is_not_adopted(self):
        # R2-025: v1.11.0b2@<b1 digest> was adopted, and both halves then ran 1.11.0b1.
        image_pin.write_lock(image_pin.Pin("1.11.0b2", DIGEST_A), self.project)
        with patch.object(self.launcher, "_pin_image_version", return_value="1.11.0b1"), \
                patch("composer.agent_installer.pin_composer_image", side_effect=AssertionError("pinned")):
            fixed = self.launcher._fix_composer_pin(SimpleNamespace(file=None, yes=True))
            self.running = {"composer-agent": image_pin.read_lock(self.project).ref}
            finding = self._finding()
        self.assertEqual([f["level"] for f in fixed], ["fail"], fixed)
        self.assertIn("names Composer 1.11.0b2 in its tag while its digest is Composer 1.11.0b1", fixed[0]["message"])
        self.assertEqual((self.project / "compose.yml").read_text(encoding="utf-8"), BLOCK)
        self.assertEqual(finding["level"], "fail", finding)

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

    def test_a_switch_to_a_release_before_signing_refuses_with_the_way_out(self):
        # F2 + O6: stable advertised 1.10.4; the unverified escape pinned it and trapped the stack.
        with patch("composer.image_pin.channel_version", return_value="1.10.4"), \
             patch("composer.image_pin.verified_pin", side_effect=AssertionError("fetched")), \
             patch("composer.registry.tag_digest", side_effect=AssertionError("pinned the registry digest")):
            outcome = self.launcher._switch_channel(
                SimpleNamespace(file=None, yes=True, allow_unverified_composer=True), "stable")
        self.assertEqual(outcome[0]["level"], "fail", outcome)
        self.assertIn("cannot run a pinned stack", outcome[0]["message"])
        self.assertIn("Stay on the beta channel", outcome[0]["fix"])
        self.assertNotIn("--allow-unverified-composer to pin", outcome[0]["fix"])
        self.assertIsNone(image_pin.read_lock(self.project))
        self.assertEqual((self.project / "compose.yml").read_text(encoding="utf-8"), BLOCK)

    def test_a_channel_switch_recreates_the_pair_on_the_new_pin(self):
        # O4: `check --beta` / `--stable` retagged the block and wrote the lock, and left the pair running.
        def model():
            images = agent_installer.composer_images_in_block(
                (self.project / "compose.yml").read_text(encoding="utf-8"))
            return {"name": "demo", "services": {
                "composer-agent": {"image": images[0]}, "composer-executor": {"image": images[1]},
                "web": {"image": "demo:latest"},
            }}

        rows = [{"service": name, "name": f"demo-{name}-1", "id": f"id-{name}", "state": "running",
                 "health": "", "restarts": 0, "started_at": ""} for name in ("composer-agent", "composer-executor", "web")]
        recreate = unittest.mock.Mock(return_value=(True, "", ""))
        resolution = image_pin.Resolution(pin=image_pin.Pin("1.11.0b2", DIGEST_A), current=None, changed=True)
        with patch("composer.image_pin.resolve", return_value=resolution), \
             patch("composer.agent_installer.shutil.which", return_value="/usr/bin/docker"), \
             patch("composer.agent_installer.switch_composer_channel",
                   functools.partial(agent_installer.switch_composer_channel, command_runner=_compose_ok)), \
             patch.object(self.launcher, "compose_config_json", side_effect=model), \
             patch.object(self.launcher, "_container_states", return_value=rows), \
             patch.object(self.launcher, "resolve_secrets", return_value=(True, "")), \
             patch.object(self.launcher, "recreate_containers", recreate), \
             patch.object(self.launcher, "_wait_healthy", return_value=(True, "")), \
             patch("sys.stdout", new_callable=io.StringIO):
            outcome = self.launcher._switch_channel(
                SimpleNamespace(file=None, yes=True, no_recreate=False, resident_apply=False), "beta")
        self.assertEqual(outcome[0]["level"], "ok", outcome)
        self.assertEqual([call.args[0] for call in recreate.call_args_list], [["composer-executor", "composer-agent"]])
        self.assertIn("fix:recreate-resident", [entry["name"] for entry in outcome])

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
            ok, moved, detail = launcher.pin_resident_pair()
        self.assertFalse(ok)
        self.assertFalse(moved)
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
        self.assertFalse(caught.exception.unavailable, "no flag may pin it")
        self.assertIn("Stay on the beta channel", caught.exception.way_out)

    def test_a_release_before_signing_is_never_pinned_even_unverified(self):
        # F2 (1.11.0b1 beta channel, H6): `check --stable --allow-unverified-composer`
        # pinned 1.10.4, which cannot read a pinned block or move the pin again.
        with self.assertRaises(image_pin.PinError) as caught:
            self._resolve("1.10.4", allow_downgrade=True, allow_unverified=True,
                          pin_reader=lambda v: self.fail("fetched"),
                          digest_reader=lambda ref: self.fail("pinned the registry digest"))
        self.assertIn("--allow-unverified-composer included", str(caught.exception))
        self.assertIn("check --stable", str(caught.exception))

    def test_moving_away_from_a_lock_before_signing_is_an_ordinary_update(self):
        resolution, fetched = self._resolve("1.11.0b2", current=image_pin.Pin("1.10.4", DIGEST_B),
                                            running="1.10.4")
        self.assertTrue(resolution.changed)
        self.assertEqual(fetched, ["1.11.0b2"])

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

    def test_a_candidate_newer_than_its_channel_leaves_the_pair_alone(self):
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
            ok, moved, detail = launcher.pin_resident_pair()
        self.assertTrue(ok, detail)
        self.assertFalse(moved, "nothing to pin: the pair is neither pulled nor recreated")
        self.assertIn("nothing to pin", out.getvalue())


class ResidentPairMovesOnlyThroughItsPinTests(unittest.TestCase):
    """The 1.11.0b1 live proof (N-2): the pair went 1.11.0b1 -> 1.10.4 on `agent update` and `./start.sh update`.

    The resolution said "nothing to pin", then the run pulled the moving
    `debeski/composer:beta` tag and recreated the pair on it anyway.
    """

    PAIR = ["composer-executor", "composer-agent"]
    SERVICES = ["web", "worker", *PAIR]

    def _model(self, resident_image):
        return {"name": "demo", "services": {
            "web": {"image": "demo-app:2.2"}, "worker": {"image": "demo-app:2.2"},
            "composer-executor": {"image": resident_image}, "composer-agent": {"image": resident_image},
        }}

    def _run(self, argv, *, resident_image="debeski/composer:beta", resolution=None, env=None, effects=None):
        from composer.launcher import DockerComposeLauncher

        launcher = DockerComposeLauncher()
        compose_calls = []

        def compose(args, **kwargs):
            compose_calls.append(list(args))
            return True, "", ""

        resolution = resolution or image_pin.Resolution(
            pin=None, current=None, changed=False,
            note="This Composer (1.11.0b1) is newer than the beta channel's newest release (1.10.4); nothing to pin.",
        )
        from contextlib import ExitStack

        steps = {
            "_resident_pair_scope": list(self.PAIR),
            "extract_config": None,
            "_defined_services": (True, list(self.SERVICES), ""),
            "sync_runtime_compose_override": True,
            "update_service_states": True,
            "compose_config_json": self._model(resident_image),
            "resolve_secrets": (True, ""),
            "render": None,
            "preflight_version_gate": (True, ""),
            "downgrade_guard": (True, ""),
            "prepare_app_data": (True, ""),
            "record_deployed_release": None,
            "ensure_edge_proxy": ([], None),
            "monitor_health": (True, ""),
            "run_post_start_hooks": (True, ""),
            "cleanup": None,
        }
        with ExitStack() as stack:
            stack.enter_context(patch("sys.argv", ["composer", *argv]))
            stack.enter_context(patch.dict(os.environ, {"COMPOSER_SELF_IMAGE": "", "COMPOSER_EXCLUDE_SERVICES": "",
                                                        **(env or {})}))
            stack.enter_context(patch("composer.launcher.install_hangup_guard"))
            stack.enter_context(patch("composer.image_pin.update_pin",
                                      return_value=(resolution, {"files": [], "lock_changed": False})))
            for name, value in steps.items():
                stack.enter_context(patch.object(launcher, name, return_value=value))
            for name, effect in (effects or {}).items():
                stack.enter_context(patch.object(launcher, name, side_effect=effect))
            stack.enter_context(patch.object(launcher, "run_docker_compose_streaming", side_effect=compose))
            stack.enter_context(patch.object(launcher, "run_command_streaming",
                                             side_effect=AssertionError("no docker pull")))
            out = stack.enter_context(patch("sys.stdout", new_callable=io.StringIO))
            launcher.run()
        return compose_calls, out.getvalue(), launcher

    def _start(self, *, tag_version, running_version="1.11.0b1", tag_id="sha256:tag", pair_running=True, env=None):
        """A plain `./start.sh` after the wrapper re-pointed the local `debeski/composer:beta`."""
        from composer.version import RELEASE_MANIFEST_LABEL

        versions = {"sha256:running": running_version, tag_id: tag_version}
        commands = []

        def run_command(argv, **kwargs):
            commands.append(list(argv))
            if argv[:2] == ["docker", "inspect"]:
                return True, "sha256:running\n", ""
            if argv[:3] == ["docker", "image", "inspect"]:
                return (True, f"{tag_id}\n", "") if tag_id else (False, "", "No such image")
            return False, "", "unexpected"

        rows = [{"Service": name, "ID": f"c-{name}", "State": "running"} for name in self.PAIR] if pair_running else []
        effects = {
            "run_command": run_command,
            "get_compose_ps_entries": lambda include_all=False: (True, rows, ""),
            "image_labels": lambda image: {RELEASE_MANIFEST_LABEL: json.dumps({"version": versions.get(image, "")})},
        }
        calls, out, _ = self._run([], env=env, effects=effects)
        return calls, out, commands

    def test_agent_update_with_nothing_to_pin_pulls_and_recreates_nothing(self):
        calls, out, _ = self._run(["agent", "update"])
        self.assertEqual(calls, [], "no compose pull or up of debeski/composer:*")
        self.assertIn("nothing to pin", out)
        self.assertIn("Nothing to update", out)

    def test_agent_update_with_an_unreadable_channel_pulls_and_recreates_nothing(self):
        kept = image_pin.Resolution(pin=None, current=None, changed=False,
                                    note="The published version could not be read. The Composer pin is unchanged.")
        calls, _, _ = self._run(["agent", "update"], resolution=kept)
        self.assertEqual(calls, [])

    def test_agent_update_with_a_new_pin_pulls_and_recreates_the_pair(self):
        moved = image_pin.Resolution(pin=image_pin.Pin("1.11.1", DIGEST_A), current=None, changed=True)
        calls, _, _ = self._run(["agent", "update"], resolution=moved)
        self.assertEqual(calls[0], ["pull", *self.PAIR])
        self.assertEqual(calls[1], ["up", "-d", *self.PAIR])

    def test_a_plain_update_leaves_a_pair_on_its_channel_tag_alone(self):
        calls, out, launcher = self._run(["update"])
        self.assertEqual(calls[0], ["pull", "web", "worker"])
        self.assertEqual(calls[1], ["up", "-d", "web", "worker"])
        self.assertTrue(all("composer-agent" not in call and "composer-executor" not in call for call in calls))
        self.assertIn("moves only through './start.sh agent update'", out)

    def test_a_pull_leaves_a_pair_on_its_channel_tag_alone(self):
        calls, _, _ = self._run(["pull"])
        self.assertEqual(calls, [["pull", "web", "worker"]])

    def test_a_plain_update_follows_a_digest_pin(self):
        calls, _, _ = self._run(["update"], resident_image=REF_10)
        self.assertEqual(calls[0], ["pull"])
        self.assertEqual(calls[1], ["up", "-d"])

    def test_an_update_naming_only_the_pair_on_its_tag_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            self._run(["update", "composer-agent"])
        self.assertEqual(caught.exception.code, 2)

    def test_an_update_naming_the_pair_and_others_keeps_the_others(self):
        calls, _, _ = self._run(["update", "web", "composer-agent"])
        self.assertEqual(calls[0], ["pull", "web"])
        self.assertEqual(calls[1], ["up", "-d", "web"])

    def test_a_start_leaves_the_pair_off_a_tag_that_went_backwards(self):
        calls, out, _ = self._start(tag_version="1.10.4")
        self.assertEqual(calls, [["up", "-d", "web", "worker"]])
        self.assertIn("Composer 1.10.4, older than the running 1.11.0b1", out)
        self.assertIn("./start.sh agent update", out)

    def test_a_start_recreates_the_pair_on_a_newer_tag(self):
        calls, _, _ = self._start(tag_version="1.11.0")
        self.assertEqual(calls, [["up", "-d"]])

    def test_a_start_with_the_pair_stopped_behaves_as_before(self):
        calls, _, _ = self._start(tag_version="1.10.4", pair_running=False)
        self.assertEqual(calls, [["up", "-d"]])

    def test_a_start_with_an_explicit_composer_image_behaves_as_before(self):
        calls, _, commands = self._start(tag_version="1.10.4", env={"COMPOSER_SELF_IMAGE": "mirror/composer:1.10.4"})
        self.assertEqual(calls, [["up", "-d"]])
        self.assertEqual(commands, [])

    def test_a_start_on_the_image_the_pair_already_runs_reads_no_versions(self):
        calls, _, commands = self._start(tag_version="1.10.4", tag_id="sha256:running")
        self.assertEqual(calls, [["up", "-d"]])
        self.assertFalse(any(argv[:2] == ["docker", "run"] for argv in commands))

    def test_a_start_leaves_the_pair_off_a_tag_it_cannot_read(self):
        calls, out, _ = self._start(tag_version="", tag_id="")
        self.assertEqual(calls, [["up", "-d", "web", "worker"]])
        self.assertIn("cannot be read", out)
