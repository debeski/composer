"""The release workflow publishes what `image_pin` verifies, in the right order.

A deployment refuses any Composer release without a signed digest statement, so
a workflow that drifts from `image_pin` stops every update in the field.
"""

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from composer import image_pin

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))
JOBS = WORKFLOW["jobs"]


def _step(job, name_part):
    for step in JOBS[job]["steps"]:
        if name_part in str(step.get("name", "")):
            return step
    raise AssertionError(f"{job} has no step named like {name_part!r}")


def _needs(job):
    needs = JOBS[job].get("needs", [])
    return [needs] if isinstance(needs, str) else needs


class ReleaseOrderTests(unittest.TestCase):
    def test_build_push_pushes_only_the_immutable_version_tag(self):
        push = _step("build-push", "Build and push")
        self.assertEqual(push["with"]["tags"].strip(), "${{ env.IMAGE }}:${{ github.ref_name }}")
        text = yaml.safe_dump(JOBS["build-push"])
        self.assertNotIn(":latest", text)
        self.assertNotIn(":beta", text)
        self.assertEqual(JOBS["build-push"]["outputs"]["digest"], "${{ steps.push.outputs.digest }}")

    def test_aliases_move_last_and_by_digest(self):
        self.assertIn("github-release", _needs("aliases"))
        self.assertIn("sign", _needs("github-release"))
        move = _step("aliases", "Move the channel aliases")
        self.assertIn("needs.build-push.outputs.digest", move["env"]["SOURCE"])
        self.assertIn("imagetools create", move["run"])

    def test_only_the_signing_job_can_mint_an_oidc_token(self):
        self.assertEqual(WORKFLOW["permissions"], {"contents": "read"})
        for name, job in JOBS.items():
            granted = (job.get("permissions") or {}).get("id-token")
            self.assertEqual(granted, "write" if name == "sign" else None, name)

    def test_the_release_attaches_the_statement_and_bundle(self):
        create = _step("github-release", "Create GitHub Release")
        files = create["with"]["files"].split()
        self.assertEqual(sorted(files), sorted([image_pin.STATEMENT_NAME, image_pin.BUNDLE_NAME]))
        self.assertTrue(create["with"]["fail_on_unmatched_files"])


class ReleaseStatementTests(unittest.TestCase):
    def _write(self, version, prerelease, digest="sha256:" + "c" * 64):
        script = re.search(r"python3 - <<'PY'\n(.*?)\n\s*PY", _step("sign", "Write the digest statement")["run"],
                           re.S).group(1)
        with tempfile.TemporaryDirectory() as holder:
            env = {**os.environ, "VERSION": version, "TAG": f"v{version}", "DIGEST": digest,
                   "PRERELEASE": "true" if prerelease else "false"}
            proc = subprocess.run([sys.executable, "-c", script], cwd=holder, env=env,
                                  capture_output=True, text=True, check=False)
            path = Path(holder) / image_pin.STATEMENT_NAME
            return proc, (path.read_bytes() if path.exists() else b"")

    def test_the_workflows_statement_is_one_a_deployment_accepts(self):
        for version, prerelease in (("1.11.0b1", True), ("1.11.0", False)):
            with self.subTest(version=version):
                proc, raw = self._write(version, prerelease)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(image_pin.check_statement(raw, version), "sha256:" + "c" * 64)

    def test_a_missing_digest_fails_the_release(self):
        proc, _raw = self._write("1.11.0", False, digest="")
        self.assertNotEqual(proc.returncode, 0)

    def test_the_workflow_verifies_the_identity_deployments_check(self):
        verify = _step("sign", "Verify the signature")["run"]
        rendered = verify.replace("${{ github.repository }}", image_pin.GITHUB_REPOSITORY) \
            .replace("${{ github.ref }}", "refs/tags/v1.11.0")
        self.assertIn(image_pin.signer_identity("1.11.0"), rendered)
        self.assertIn(image_pin.OIDC_ISSUER, rendered)

    def test_the_signed_files_are_the_names_deployments_download(self):
        upload = next(step for step in JOBS["sign"]["steps"] if "upload-artifact" in str(step.get("uses")))
        self.assertEqual(sorted(upload["with"]["path"].split()),
                         sorted([image_pin.STATEMENT_NAME, image_pin.BUNDLE_NAME]))
        self.assertIn(f"--bundle {image_pin.BUNDLE_NAME} {image_pin.STATEMENT_NAME}",
                      _step("sign", "Sign the statement")["run"])


if __name__ == "__main__":
    unittest.main()
