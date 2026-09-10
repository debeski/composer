"""Channel behaviour: version ordering, eligibility, policy reading, switching.

These are the rules that decide which release a deployment installs, so each
test states the rule it is defending rather than just exercising the function.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from composer import channel_config, dlux_channel
from composer.agent_installer import (
    AgentInstallError,
    COMPOSER_AGENT_END,
    COMPOSER_AGENT_START,
    composer_images_in_block,
    retag_composer_image,
)
from composer.dlux_release_source import (
    ReleaseCandidate,
    ReleaseSourceError,
    normalize_manifest,
    select_candidate,
)
from composer.dlux_runtime import version_sort_key
from composer.release_tag import (
    ReleaseTagError,
    changelog_section,
    classify_tag,
    published_image_tags,
    should_advance_beta,
    validate_beta_first,
)
from composer.versions import at_least, try_parse


def _candidates(*versions):
    return [
        ReleaseCandidate(
            version=v,
            filename=f"django_lux-{v}-py3-none-any.whl",
            url=f"https://files.pythonhosted.org/x/django_lux-{v}-py3-none-any.whl",
            sha256="0" * 64,
        )
        for v in versions
    ]


class VersionOrderingTests(unittest.TestCase):
    def test_beta_ten_is_newer_than_beta_two(self):
        # The regex this replaced scored every non-numeric part as -1, so b2 and
        # b10 tied and the winner depended on input order.
        self.assertGreater(try_parse("1.9.0b10"), try_parse("1.9.0b2"))

    def test_the_prerelease_ladder_climbs_to_final(self):
        ladder = ["1.9.0b1", "1.9.0b2", "1.9.0rc1", "1.9.0"]
        parsed = [try_parse(v) for v in ladder]
        self.assertEqual(parsed, sorted(parsed), "b < rc < final must hold in that order")

    def test_a_beta_does_not_satisfy_its_own_floor(self):
        # A manifest needing a fix that shipped in 1.3.14 must not accept the
        # beta of 1.3.14, which by definition predates the final.
        self.assertFalse(at_least("1.3.14b1", ">=1.3.14"))
        self.assertTrue(at_least("1.3.14", ">=1.3.14"))
        self.assertTrue(at_least("1.3.15", ">=1.3.14"))

    def test_two_digit_patch_outranks_one_digit(self):
        self.assertTrue(at_least("1.8.10", ">=1.8.9"))

    def test_unparseable_input_never_satisfies_a_floor(self):
        self.assertFalse(at_least("", ">=1.0.0"))
        self.assertFalse(at_least("nightly", ">=1.0.0"))

    def test_staged_release_ordering_separates_a_beta_from_its_final(self):
        # The rollback target search wants releases strictly below the active
        # one. Ties make a beta invisible to the rollback that should find it.
        self.assertLess(version_sort_key("1.9.0b2"), version_sort_key("1.9.0"))
        self.assertLess(version_sort_key("1.9.0b2"), version_sort_key("1.9.0rc1"))
        self.assertLess(version_sort_key("garbage"), version_sort_key("1.0.0"))


class CandidateSelectionTests(unittest.TestCase):
    def test_stable_never_selects_a_prerelease(self):
        chosen = select_candidate(_candidates("1.8.14", "1.9.0b1"), channel="stable")
        self.assertEqual(chosen.version, "1.8.14")

    def test_beta_selects_the_prerelease_when_it_is_newest(self):
        chosen = select_candidate(_candidates("1.8.14", "1.9.0b1"), channel="beta")
        self.assertEqual(chosen.version, "1.9.0b1")

    def test_beta_still_prefers_a_newer_final(self):
        # Beta means "prereleases are also eligible", not "prefer prereleases".
        chosen = select_candidate(_candidates("1.9.0b1", "1.9.0"), channel="beta")
        self.assertEqual(chosen.version, "1.9.0")

    def test_stable_refuses_rather_than_falling_back_to_a_beta(self):
        # The old code took `stable or all_candidates`, so a project with only
        # prereleases published handed one to a stable deployment.
        with self.assertRaises(ReleaseSourceError) as caught:
            select_candidate(_candidates("1.9.0b1", "1.9.0b2"), channel="stable")
        self.assertIn("stable channel", str(caught.exception))

    def test_an_explicit_pin_is_honoured_on_either_channel(self):
        chosen = select_candidate(_candidates("1.8.14", "1.9.0b1"), "1.9.0b1", channel="stable")
        self.assertEqual(chosen.version, "1.9.0b1")

    def test_development_releases_are_never_eligible(self):
        with self.assertRaises(ReleaseSourceError):
            select_candidate(_candidates("1.9.0.dev1"), channel="beta")


class ManifestRequirementTests(unittest.TestCase):
    """`requires` fails closed, so every key DjangoLux publishes must be known."""

    def _manifest(self, **requires):
        return {
            "schema_version": 2,
            "version": "1.8.14",
            "requires": {"updater_schema": ">=1", **requires},
            "migrations": {"effect": "none", "rollback_compatible": True},
            "install": {"inline": "allowed"},
        }

    def test_a_migration_baseline_requirement_is_understood(self):
        # DjangoLux 1.8.14 publishes this key. Until Composer recognised it,
        # every Composer refused that manifest outright with "unsupported
        # requirements", so no deployment could install the release at all.
        normalized = normalize_manifest(self._manifest(migration_baseline=">=1.8.9"), "1.8.14")
        self.assertTrue(normalized["inline_safe"])

    def test_an_unknown_requirement_still_refuses_the_release(self):
        # The fail-closed rule is the point of the allow-list; widening it for
        # one key must not have turned it into a pass-through.
        with self.assertRaises(ReleaseSourceError) as caught:
            normalize_manifest(self._manifest(quantum_alignment=">=1"), "1.8.14")
        self.assertIn("quantum_alignment", str(caught.exception))

    def test_a_composer_beta_does_not_satisfy_a_stable_service_floor(self):
        from unittest import mock

        with mock.patch("composer.dlux_release_source.read_composer_version", return_value="1.3.14b1"):
            with self.assertRaises(ReleaseSourceError) as caught:
                normalize_manifest(
                    self._manifest(services={"composer": ">=1.3.14"}), "1.8.14",
                )
        self.assertIn("requires Composer", str(caught.exception))


class PolicyReadingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _write(self, payload):
        (self.state / dlux_channel.POLICY_FILENAME).write_text(
            payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
        )

    def test_a_missing_policy_is_stable_and_not_an_error(self):
        self.assertEqual(dlux_channel.read_policy(self.state), ("stable", ""))

    def test_a_published_beta_policy_is_read(self):
        self._write({"schema_version": 1, "channel": "beta"})
        self.assertEqual(dlux_channel.read_policy(self.state), ("beta", ""))

    def test_corrupt_policy_reads_stable_and_reports(self):
        # Corrupting a file must never be a way to turn beta on.
        self._write("{not json")
        channel, error = dlux_channel.read_policy(self.state)
        self.assertEqual(channel, "stable")
        self.assertTrue(error)

    def test_a_future_schema_stays_on_stable(self):
        self._write({"schema_version": 99, "channel": "beta"})
        channel, error = dlux_channel.read_policy(self.state)
        self.assertEqual(channel, "stable")
        self.assertIn("schema 99", error)

    def test_an_unknown_channel_name_is_not_beta(self):
        self._write({"schema_version": 1, "channel": "nightly"})
        self.assertEqual(dlux_channel.read_policy(self.state)[0], "stable")

    def test_a_request_is_pending_until_its_token_is_acknowledged(self):
        request = dlux_channel.request_channel(self.state, "beta")
        self.assertEqual(dlux_channel.describe(self.state)["pending"], "beta")
        (self.state / dlux_channel.ACK_FILENAME).write_text(
            json.dumps({"token": request["token"], "channel": "beta", "applied": True}),
            encoding="utf-8",
        )
        self.assertEqual(dlux_channel.describe(self.state)["pending"], "")

    def test_a_refused_request_is_reported_as_failed(self):
        request = dlux_channel.request_channel(self.state, "beta")
        (self.state / dlux_channel.ACK_FILENAME).write_text(
            json.dumps({
                "token": request["token"], "channel": "beta",
                "applied": False, "error": "volume read-only",
            }),
            encoding="utf-8",
        )
        status = dlux_channel.describe(self.state)
        self.assertEqual(status["pending"], "")
        self.assertIn("read-only", status["failed"])


class ComposerChannelConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_a_project_that_never_chose_runs_stable(self):
        self.assertEqual(channel_config.read_channel(self.project), "stable")
        self.assertEqual(
            channel_config.resolve_self_image(self.project, env={}),
            "debeski/composer:latest",
        )

    def test_the_persisted_channel_selects_the_image(self):
        channel_config.write_channel("beta", self.project)
        self.assertEqual(
            channel_config.resolve_self_image(self.project, env={}),
            "debeski/composer:beta",
        )

    def test_an_explicit_pin_beats_the_channel(self):
        # Documented precedence: an operator who pinned an image gets it.
        channel_config.write_channel("beta", self.project)
        env = {"COMPOSER_SELF_IMAGE": "debeski/composer:v1.3.9"}
        self.assertEqual(
            channel_config.resolve_self_image(self.project, env=env),
            "debeski/composer:v1.3.9",
        )
        self.assertTrue(channel_config.describe(self.project, env=env)["pinned"])

    def test_a_corrupt_channel_file_reads_stable(self):
        channel_config.channel_file(self.project).write_text("beta\x00nonsense", encoding="utf-8")
        self.assertEqual(channel_config.read_channel(self.project), "stable")

    def test_an_unknown_channel_is_refused_on_write(self):
        with self.assertRaises(channel_config.ChannelConfigError):
            channel_config.write_channel("nightly", self.project)

    def test_the_wrappers_resolve_the_same_image_this_module_does(self):
        # The wrapper reads the channel file itself, before any Python runs.
        # If these two drift, the deployer and the agent run different images.
        root = Path(__file__).resolve().parents[1]
        for name in ("start.sh", "start.ps1"):
            text = (root / name).read_text(encoding="utf-8")
            self.assertIn(channel_config.CHANNEL_FILENAME, text, f"{name} must read the channel file")
            self.assertIn("debeski/composer:beta", text, f"{name} must know the beta image")
            self.assertIn("COMPOSER_SELF_IMAGE", text, f"{name} must honour the pin")


class ComposeRetagTests(unittest.TestCase):
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
    image: debeski/composer:latest
"""

    def test_every_composer_service_in_the_block_moves(self):
        updated = retag_composer_image(self.BLOCK, "debeski/composer:beta")
        self.assertEqual(
            composer_images_in_block(updated),
            ["debeski/composer:beta", "debeski/composer:beta"],
        )

    def test_nothing_outside_the_generated_block_is_touched(self):
        # `web` here deliberately names the same image. A switch is scoped to
        # Composer's own services; rewriting the whole file would be a different,
        # much larger promise than the operator made.
        updated = retag_composer_image(self.BLOCK, "debeski/composer:beta")
        self.assertIn("  web:\n    image: debeski/composer:latest\n", updated)

    def test_the_third_party_proxy_image_is_left_alone(self):
        updated = retag_composer_image(self.BLOCK, "debeski/composer:beta")
        self.assertIn("image: tecnativa/docker-socket-proxy:latest", updated)

    def test_a_file_with_no_generated_block_is_refused(self):
        with self.assertRaises(AgentInstallError):
            retag_composer_image("name: demo\nservices:\n  web:\n    image: x\n", "debeski/composer:beta")


class ReleaseTagTests(unittest.TestCase):
    def test_a_plain_version_is_stable_and_takes_latest(self):
        decision = classify_tag("v1.3.14", version_file_value="1.3.14")
        self.assertEqual(decision["channel"], "stable")
        self.assertFalse(decision["prerelease"])
        self.assertTrue(decision["make_latest"])
        self.assertEqual(decision["alias"], "latest")

    def test_a_beta_tag_never_claims_latest(self):
        decision = classify_tag("v1.4.0b1", version_file_value="1.4.0b1")
        self.assertEqual(decision["channel"], "beta")
        self.assertTrue(decision["prerelease"])
        self.assertFalse(decision["make_latest"])
        self.assertEqual(decision["alias"], "beta")

    def test_release_candidates_publish_through_the_beta_channel(self):
        self.assertEqual(classify_tag("v1.4.0rc1")["channel"], "beta")

    def test_non_canonical_spellings_are_refused(self):
        for tag in ("v1.4.0-beta1", "v1.4.0.b1", "V1.4.0"):
            with self.assertRaises(ReleaseTagError, msg=tag):
                classify_tag(tag)

    def test_development_local_and_post_releases_are_refused(self):
        for tag in ("v1.4.0.dev1", "v1.4.0+local", "v1.4.0.post1"):
            with self.assertRaises(ReleaseTagError, msg=tag):
                classify_tag(tag)

    def test_a_tag_that_disagrees_with_the_version_file_is_refused(self):
        with self.assertRaises(ReleaseTagError):
            classify_tag("v1.4.0", version_file_value="1.3.14")

    def test_a_stable_release_adopts_the_beta_alias_when_it_is_newer(self):
        self.assertTrue(should_advance_beta("1.4.0", "1.4.0b3"))

    def test_the_beta_alias_never_moves_backwards(self):
        # Stable 1.4.1 must not drag a 1.5.0b1 tester back a minor version.
        self.assertFalse(should_advance_beta("1.4.1", "1.5.0b1"))

    def test_a_beta_alias_that_does_not_exist_is_free_to_claim(self):
        self.assertTrue(should_advance_beta("1.4.0", "", beta_exists=False))

    def test_an_unreadable_beta_alias_is_never_overwritten(self):
        # The two "no version" cases want opposite answers. An alias that
        # EXISTS but whose version could not be read must not be clobbered:
        # "I could not check" is not evidence that overwriting is safe, and a
        # silently downgraded beta line is worse than a stalled one.
        #
        # This was fail-open, and the registry read feeding it was broken in a
        # way that always produced this case — `.Image` is nil on a multi-arch
        # manifest list, so the un-indexed template errored and `|| true` made
        # it look like "no beta published".
        self.assertFalse(should_advance_beta("1.4.0", "", beta_exists=True))
        self.assertFalse(should_advance_beta("1.4.0", "not-a-version", beta_exists=True))

    def test_the_cli_defaults_to_the_cautious_reading(self):
        # Nothing passed means "assume it exists", so a caller that forgets the
        # flag does not silently get the dangerous branch.
        import subprocess
        import sys

        # Derived from VERSION, not hardcoded: this shells out to the real CLI,
        # which validates the tag against that file, so a pinned literal would
        # fail on every version bump for a reason unrelated to what is tested.
        root = Path(__file__).resolve().parents[1]
        version = (root / "VERSION").read_text(encoding="utf-8").strip()
        completed = subprocess.run(
            [sys.executable, "-m", "composer.release_tag", f"v{version}"],
            capture_output=True, text=True, cwd=str(root),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        if try_parse(version).is_prerelease:
            self.assertIn('"advance_beta": true', completed.stdout, "a prerelease always takes :beta")
        else:
            # No --beta-exists passed, so the cautious default applies and a
            # stable release must NOT claim an alias it could not compare.
            self.assertIn('"advance_beta": false', completed.stdout)


class ChangelogSectionTests(unittest.TestCase):
    SAMPLE = (
        "# Changelog\n\n"
        "## v1.3.14\n\nfinal notes\n\n"
        "## v1.3.14b1\n\nbeta notes\n\n"
        "## v1.3.13\n\nolder\n"
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "CHANGELOG.md"
        self.path.write_text(self.SAMPLE, encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)

    def test_the_stable_section_is_not_the_beta_section(self):
        # A prefix match on "## v1.3.14" also matches "## v1.3.14b1", so the
        # stable release would have shipped its beta's notes.
        self.assertEqual(changelog_section("1.3.14", self.path), "final notes")
        self.assertEqual(changelog_section("1.3.14b1", self.path), "beta notes")

    def test_a_missing_section_is_empty_rather_than_the_next_one(self):
        self.assertEqual(changelog_section("9.9.9", self.path), "")



class BetaFirstGateTests(unittest.TestCase):
    """A new minor or major must never first appear as stable (plan §1)."""

    @staticmethod
    def _published(*tags):
        return lambda betas: [tag for tag in betas if tag in tags]

    def test_a_new_minor_with_no_beta_tag_is_refused(self):
        errors = validate_beta_first("1.4.0", tags=["v1.3.14", "v1.3.14b2"], fetch_published=self._published())
        self.assertIn("must be published as a beta first", errors[0])

    def test_a_published_beta_admits_the_stable_release(self):
        self.assertEqual(validate_beta_first(
            "1.4.0", tags=["v1.3.14", "v1.4.0b1"], fetch_published=self._published("v1.4.0b1"),
        ), [])

    def test_a_release_candidate_counts_as_the_beta(self):
        self.assertEqual(validate_beta_first(
            "1.4.0", tags=["v1.4.0rc1"], fetch_published=self._published("v1.4.0rc1"),
        ), [])

    def test_a_beta_that_was_never_pushed_does_not_count(self):
        errors = validate_beta_first("1.4.0", tags=["v1.4.0b1"], fetch_published=self._published())
        self.assertIn("Docker Hub serves none of them", errors[0])

    def test_a_beta_of_another_release_does_not_count(self):
        errors = validate_beta_first(
            "1.4.0", tags=["v1.3.14b1", "v1.5.0b1"],
            fetch_published=self._published("v1.3.14b1", "v1.5.0b1"),
        )
        self.assertIn("must be published as a beta first", errors[0])

    def test_a_registry_error_refuses(self):
        def broken(_betas):
            raise OSError("docker not found")

        self.assertIn("Could not confirm", validate_beta_first("1.4.0", tags=["v1.4.0b1"], fetch_published=broken)[0])

    def test_patches_and_prereleases_consult_nothing(self):
        def must_not_be_called(_betas):
            raise AssertionError("the gate consulted the registry for a release it does not cover")

        for version in ("1.3.15", "1.4.1", "1.4.0b1", "1.4.0rc2"):
            with self.subTest(version=version):
                self.assertEqual(validate_beta_first(version, tags=None, fetch_published=must_not_be_called), [])

    def test_the_registry_is_asked_about_each_beta_tag(self):
        asked = []

        def runner(command, **_kwargs):
            asked.append(command[-1])
            code = 0 if command[-1].endswith(":v1.4.0b2") else 1
            return subprocess.CompletedProcess(command, code, "", "")

        self.assertEqual(published_image_tags(["v1.4.0b1", "v1.4.0b2"], runner=runner), ["v1.4.0b2"])
        self.assertEqual(asked, ["debeski/composer:v1.4.0b1", "debeski/composer:v1.4.0b2"])


if __name__ == "__main__":
    unittest.main()
