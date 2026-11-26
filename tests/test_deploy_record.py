"""WEB_IMAGE: the deploy record names the exact image `up` used.

Composer has no trust root for a project's own image, so an update still means
"pull the tag". What it can do is remember the digest and image ID it deployed,
for an exact rollback target, the audit trail, and a note when the tag moves.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from composer import downgrade_guard
from composer.launcher import DockerComposeLauncher

from tests.test_deploy_safety import CONFIG, NEWER

DIGEST = "sha256:" + "d" * 64
OTHER = "sha256:" + "e" * 64


class RepoDigestTests(unittest.TestCase):
    def test_the_entry_for_the_stacks_repository_is_taken(self):
        digests = [f"registry.example/other@{OTHER}", f"docker.io/debeski/decrees@{DIGEST}"]
        self.assertEqual(downgrade_guard.repo_digest_for("debeski/decrees:v2.2.2", digests),
                         f"docker.io/debeski/decrees@{DIGEST}")

    def test_official_images_and_registries_with_ports_match(self):
        self.assertEqual(downgrade_guard.repo_digest_for("nginx:1.27", [f"docker.io/library/nginx@{DIGEST}"]),
                         f"docker.io/library/nginx@{DIGEST}")
        self.assertEqual(
            downgrade_guard.repo_digest_for("gitlab.office:5050/apps/decrees:2.2",
                                            [f"gitlab.office:5050/apps/decrees@{DIGEST}"]),
            f"gitlab.office:5050/apps/decrees@{DIGEST}")

    def test_another_repository_or_a_local_build_gives_nothing(self):
        self.assertEqual(downgrade_guard.repo_digest_for("debeski/decrees:v2", [f"debeski/other@{DIGEST}"]), "")
        self.assertEqual(downgrade_guard.repo_digest_for("decrees:latest", []), "")


class DeployRecordIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, cwd)
        self.launcher = DockerComposeLauncher()
        self.launcher.services = ["web", "worker", "caddy"]
        self.launcher.compose_config_json = lambda: CONFIG
        self.launcher.get_compose_ps_entries = lambda include_all=False: (True, [], "")

        def run_command(argv, timeout=None, env=None):
            if argv[:3] == ["docker", "image", "inspect"] and "{{json .Id}}" in argv[-1]:
                return True, f'"sha256:imageid" ["docker.io/debeski/decrees@{DIGEST}"]', ""
            if argv[:3] == ["docker", "image", "inspect"]:
                return True, json.dumps(NEWER), ""
            return False, "", "unexpected"

        self.launcher.run_command = run_command

    def test_the_record_names_the_deployed_digest_and_image_id(self):
        self.assertEqual(self.launcher.downgrade_guard(), (True, ""))
        self.launcher.record_deployed_release()
        deployed = downgrade_guard.read_deployed_image()
        self.assertEqual(deployed["repo_digest"], f"docker.io/debeski/decrees@{DIGEST}")
        self.assertEqual(deployed["image_id"], "sha256:imageid")
        self.assertEqual(downgrade_guard.read_record(), {"app": "2.2.2", "dlux": "1.16.6"},
                         "the version floor is unchanged by the identity fields")

    def test_check_notes_a_tag_that_moved_since_the_deploy(self):
        self.launcher.downgrade_guard()
        self.launcher.record_deployed_release()
        image = downgrade_guard.read_deployed_image()["image"]
        self.assertEqual(self.launcher._tag_moved_note(image, DIGEST), "")
        note = self.launcher._tag_moved_note(image, OTHER)
        self.assertIn("another image than the one deployed here", note)
        self.assertIn(DIGEST, note)
        self.assertEqual(self.launcher._tag_moved_note("debeski/somethingelse:1", OTHER), "")


if __name__ == "__main__":
    unittest.main()
