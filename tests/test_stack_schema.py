"""The secrets file's stack schema stamp, which the schema migrations restamp.

`check` reads a stamp from the plaintext secrets file, so a migration that
restamps the other stack files must restamp it too. It holds the deployment's
secrets: only the stamp may change, and none of it may reach a diff or a backup.
"""

import os
import tempfile
import unittest
from pathlib import Path

from composer import stack_schema

SECRET = "test-secret-value-9f3c"
STAMPED = (
    b"# dlux stack schema 4\r\n"
    b"DJANGO_SECRET_KEY=" + SECRET.encode() + b"\r\n"
    b"POSTGRES_PASSWORD=latin-1-\xe9\r\n"
)


class _Project(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name).resolve()

    def secrets(self, data=STAMPED, name=".secrets/.env"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o600)
        return path


class SecretsFileTests(_Project):
    def test_it_is_the_first_candidate_composer_loads(self):
        self.assertIsNone(stack_schema.secrets_file(self.root))
        hidden = self.secrets()
        self.assertEqual(stack_schema.secrets_file(self.root), hidden)
        plain = self.secrets(name=".env")
        self.assertEqual(stack_schema.secrets_file(self.root), plain)

    def test_a_link_out_of_the_project_is_left_alone(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            target = Path(elsewhere) / ".env"
            target.write_bytes(STAMPED)
            (self.root / ".env").symlink_to(target)
            self.assertIsNone(stack_schema.secrets_file(self.root))
            updates = {}
            stack_schema.restamp_secrets(self.root, updates, 4, 5)
            self.assertEqual(updates, {})


class RestampSecretsTests(_Project):
    def test_a_stamped_file_is_restamped_byte_for_byte(self):
        path = self.secrets()
        updates = {}
        stack_schema.restamp_secrets(self.root, updates, 4, 5)
        self.assertEqual(list(updates), [path])
        self.assertEqual(
            updates[path].encode("utf-8", "surrogateescape"),
            STAMPED.replace(b"schema 4", b"schema 5", 1),
        )

    def test_an_unstamped_or_other_schema_file_is_left_alone(self):
        for data in (STAMPED.replace(b"# dlux stack schema 4\r\n", b""), STAMPED.replace(b"4", b"3", 1)):
            self.secrets(data)
            updates = {}
            stack_schema.restamp_secrets(self.root, updates, 4, 5)
            self.assertEqual(updates, {}, data[:24])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads any file")
    def test_an_unreadable_file_is_left_alone(self):
        path = self.secrets()
        path.chmod(0)
        self.addCleanup(path.chmod, 0o600)
        updates = {}
        stack_schema.restamp_secrets(self.root, updates, 4, 5)
        self.assertEqual(updates, {})


class WriteUpdatesTests(_Project):
    def test_only_the_stamp_changes_in_place_and_nothing_is_copied(self):
        path = self.secrets()
        compose = self.root / "compose.yml"
        compose.write_text('x-environment: &env\n  DLUX_STACK_SCHEMA: "4"\n', encoding="utf-8")
        before = path.stat()
        updates = {compose.resolve(): stack_schema.restamp(compose.read_text(encoding="utf-8"), 5)}
        stack_schema.restamp_secrets(self.root, updates, 4, 5)
        backup = self.root / ".xclude" / "composer-check" / "test"

        stack_schema.write_updates(self.root, updates, backup)

        self.assertEqual(path.read_bytes(), STAMPED.replace(b"schema 4", b"schema 5", 1))
        after = path.stat()
        self.assertEqual((after.st_ino, after.st_mode, after.st_uid), (before.st_ino, before.st_mode, before.st_uid))
        self.assertTrue((backup / "compose.yml").is_file())
        self.assertFalse((backup / ".secrets").exists(), "the secrets are never copied to the backup")
        self.assertEqual(stack_schema.collect_stamps(str(self.root), "compose.yml", [".secrets/.env"]),
                         {"compose.yml": 5, ".secrets/.env": 5})

    def test_the_preview_names_the_file_but_shows_none_of_it(self):
        self.secrets()
        compose = self.root / "compose.yml"
        compose.write_text('x-environment: &env\n  DLUX_STACK_SCHEMA: "4"\n', encoding="utf-8")
        updates = {compose.resolve(): stack_schema.restamp(compose.read_text(encoding="utf-8"), 5)}
        stack_schema.restamp_secrets(self.root, updates, 4, 5)

        diff = stack_schema.preview_diff(self.root, updates)
        note = stack_schema.secrets_note(self.root, updates, 5)

        self.assertIn('+  DLUX_STACK_SCHEMA: "5"', diff)
        self.assertNotIn(".env", diff)
        self.assertIn(".secrets/.env is restamped 5", note)
        for text in (diff, note):
            self.assertNotIn(SECRET, text)
            self.assertNotIn("latin-1-", text)
        self.assertEqual(stack_schema.secrets_note(self.root, {}, 5), "")


if __name__ == "__main__":
    unittest.main()
