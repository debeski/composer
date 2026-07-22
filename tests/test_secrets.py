import os
import unittest
from pathlib import Path
from unittest.mock import patch

from composer.secrets_manager import SecretsMixin


class SecretsHarness(SecretsMixin):
    """Minimal launcher stand-in with a controllable required-var set."""

    def __init__(self, required, candidates, dev_mode=False):
        self._required = set(required)
        self._candidates = [Path(c) for c in candidates]
        self.dev_mode = dev_mode
        self.loaded_secrets = []
        self.secrets_source = None

    def required_compose_vars(self):
        return set(self._required)

    def plaintext_env_candidates(self):
        return list(self._candidates)


class ResolveSecretsTests(unittest.TestCase):
    def _write(self, path: Path, text: str) -> Path:
        path.write_text(text, encoding="utf-8")
        return path

    def test_unreadable_candidate_is_not_vacuous_success(self):
        # A compose that defaults every secret leaves `required` tiny (here
        # already satisfied by the process env). An unreadable secrets file must
        # fail loudly, never fall through to compose defaults.
        with patch.object(os, "environ", {"PWD": "/proj", "COMPOSER_VERSION": "x"}):
            harness = SecretsHarness(required={"PWD"}, candidates=[".secrets/.env"])
            with patch.object(
                harness, "parse_env_file", side_effect=PermissionError(13, "Permission denied")
            ), patch.object(harness, "mapped_host_uid", return_value=231072):
                ok, err = harness.resolve_secrets()
        self.assertFalse(ok)
        self.assertIn("could not be read", err)
        self.assertIn("host UID 231072", err)
        self.assertIn("setfacl -m u:231072:--x .secrets", err)
        self.assertIn("setfacl -m u:231072:r-- .secrets/.env", err)
        self.assertIn("Do not make the secrets file world-readable", err)
        self.assertEqual(harness.loaded_secrets, [])
        self.assertIsNone(harness.secrets_source)

    def test_mapped_host_uid_translates_effective_uid(self):
        harness = SecretsHarness(required=set(), candidates=[])
        uid_map = "         0     231072      65536\n"

        with patch.object(Path, "read_text", return_value=uid_map), patch.object(
            os, "geteuid", return_value=7
        ):
            mapped_uid = harness.mapped_host_uid()

        self.assertEqual(mapped_uid, 231079)

    def test_inherited_launcher_environment_avoids_project_file_read(self):
        inherited = {
            "COMPOSER_INHERITED_SECRET_KEYS": "POSTGRES_PASSWORD,OPTIONAL_EMPTY",
            "POSTGRES_PASSWORD": "s3cret",
            "OPTIONAL_EMPTY": "",
            "PWD": "/proj",
        }
        harness = SecretsHarness(required={"PWD"}, candidates=[".secrets/.env"])

        with patch.object(os, "environ", inherited), patch.object(
            harness, "parse_env_file"
        ) as parse_file:
            ok, err = harness.resolve_secrets()

        self.assertTrue(ok, err)
        self.assertEqual(harness.secrets_source, "inherited launcher environment")
        parse_file.assert_not_called()

    def test_inherited_launcher_environment_fails_when_declared_key_is_missing(self):
        inherited = {
            "COMPOSER_INHERITED_SECRET_KEYS": "POSTGRES_PASSWORD,DJANGO_SECRET_KEY",
            "POSTGRES_PASSWORD": "s3cret",
        }
        harness = SecretsHarness(required=set(), candidates=[])

        with patch.object(os, "environ", inherited):
            ok, err = harness.resolve_secrets()

        self.assertFalse(ok)
        self.assertIn("DJANGO_SECRET_KEY", err)

    def test_readable_but_empty_candidate_is_rejected(self):
        with patch.object(os, "environ", {"PWD": "/proj", "COMPOSER_VERSION": "x"}):
            harness = SecretsHarness(required={"PWD"}, candidates=[".env"])
            with patch.object(harness, "parse_env_file", return_value={}):
                ok, err = harness.resolve_secrets()
        self.assertFalse(ok)
        self.assertIn("no values found", err)
        self.assertEqual(harness.loaded_secrets, [])

    def test_readable_candidate_with_values_loads(self):
        with patch.object(os, "environ", {"PWD": "/proj", "COMPOSER_VERSION": "x"}):
            harness = SecretsHarness(required={"PWD"}, candidates=[".secrets/.env"])
            with patch.object(
                harness,
                "parse_env_file",
                return_value={"POSTGRES_PASSWORD": "s3cret"},
            ):
                ok, err = harness.resolve_secrets()
                loaded = os.environ.get("POSTGRES_PASSWORD")
        self.assertTrue(ok, err)
        self.assertEqual(loaded, "s3cret")
        self.assertEqual(harness.secrets_source, ".secrets/.env")

    def test_unreadable_first_candidate_falls_through_to_good_second(self):
        with patch.object(os, "environ", {"PWD": "/proj", "COMPOSER_VERSION": "x"}):
            harness = SecretsHarness(
                required={"PWD"}, candidates=[".env", ".secrets/.env"]
            )

            def fake_parse(path):
                if str(path) == ".env":
                    raise PermissionError(13, "Permission denied")
                return {"POSTGRES_PASSWORD": "s3cret"}

            with patch.object(harness, "parse_env_file", side_effect=fake_parse):
                ok, err = harness.resolve_secrets()
        self.assertTrue(ok, err)
        self.assertEqual(harness.secrets_source, ".secrets/.env")


if __name__ == "__main__":
    unittest.main()
