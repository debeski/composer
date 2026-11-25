"""The wrapper runs the deployer from `.composer-image` and refreshes it by pin.

The deployer holds the Docker socket, so it is the image a compromised registry
tag would most like to swap. These run the real `start.sh` against a fake
`docker` and check which image each step uses.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

DIGEST_OLD = "sha256:" + "1" * 64
DIGEST_NEW = "sha256:" + "2" * 64
LOCK_OLD = f"debeski/composer:v1.11.0b1@{DIGEST_OLD}"
LOCK_NEW = f"debeski/composer:v1.11.0b2@{DIGEST_NEW}"

# `image inspect --format …image-pin…` answers from $CAN_PIN; a bare presence
# check from $LOCAL_IMAGES (space-separated); `update self --pin` writes $NEW_LOCK.
FAKE_DOCKER = r"""#!/bin/sh
printf "%s\n" "$*" >> "$DOCKER_CALLS_FILE"
last=""
for arg in "$@"; do last="$arg"; done
case "$1" in
  image)
    case "$*" in
      *image-pin*)
        case " $CAN_PIN " in *" $last "*) echo 1 ;; *) echo "<no value>" ;; esac
        exit 0 ;;
    esac
    case " $LOCAL_IMAGES " in *" $last "*) echo sha256:id-$last; exit 0 ;; esac
    exit 1 ;;
  pull)
    exit "${PULL_EXIT:-0}" ;;
  run)
    case "$*" in
      *"update self --pin"*)
        if [ "${PIN_EXIT:-0}" != 0 ]; then exit "$PIN_EXIT"; fi
        printf "%s\n" "$NEW_LOCK" > "$PROJECT_DIR/.composer-image"
        exit 0 ;;
    esac
    exit 0 ;;
esac
"""


@unittest.skipIf(os.name == "nt", "Bash wrapper test")
class WrapperPinTests(unittest.TestCase):
    def _run(self, *argv, lock=None, local=(), can_pin=(), pin_exit=0, new_lock=LOCK_NEW, env_extra=None):
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            fake_bin = root / "bin"
            project.mkdir()
            fake_bin.mkdir()
            shutil.copy2(repo_root / "start.sh", project / "start.sh")
            if lock is not None:
                (project / ".composer-image").write_text(lock + "\n", encoding="utf-8")
            calls_path = root / "calls"
            docker = fake_bin / "docker"
            docker.write_text(FAKE_DOCKER, encoding="utf-8")
            docker.chmod(0o755)
            env = os.environ.copy()
            env.pop("COMPOSER_SELF_IMAGE", None)
            env.update({
                "PATH": f"{fake_bin}:{env.get('PATH', '')}",
                "DOCKER_CALLS_FILE": str(calls_path),
                "PROJECT_DIR": str(project),
                "LOCAL_IMAGES": " ".join(local),
                "CAN_PIN": " ".join(can_pin),
                "PIN_EXIT": str(pin_exit),
                "NEW_LOCK": new_lock,
            })
            env.update(env_extra or {})
            result = subprocess.run(["bash", str(project / "start.sh"), *argv], cwd=project, env=env,
                                    capture_output=True, text=True, check=False)
            calls = calls_path.read_text(encoding="utf-8").splitlines() if calls_path.exists() else []
            lock_after = (project / ".composer-image").read_text(encoding="utf-8").strip() \
                if (project / ".composer-image").exists() else None
            return result, calls, lock_after

    @staticmethod
    def _launches(calls):
        return [call for call in calls if call.startswith("run -i")]

    def test_commands_run_the_pinned_deployer(self):
        result, calls, _ = self._run("check", lock=LOCK_OLD, local=(LOCK_OLD,))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self._launches(calls)[0].endswith(f"{LOCK_OLD} check"), calls)
        self.assertFalse([c for c in calls if c.startswith("pull")])

    def test_an_explicit_image_beats_the_lock(self):
        _, calls, _ = self._run("check", lock=LOCK_OLD, local=("my/composer:x",),
                                env_extra={"COMPOSER_SELF_IMAGE": "my/composer:x"})
        self.assertTrue(self._launches(calls)[0].endswith("my/composer:x check"), calls)

    def test_a_malformed_lock_runs_the_channel_tag(self):
        _, calls, _ = self._run("check", lock="debeski/composer:latest", local=("debeski/composer:latest",))
        self.assertTrue(self._launches(calls)[0].endswith("debeski/composer:latest check"), calls)

    def test_update_self_verifies_with_the_pinned_deployer_then_pulls_the_new_digest(self):
        result, calls, lock = self._run("update", "self", lock=LOCK_OLD, local=(LOCK_OLD,), can_pin=(LOCK_OLD,))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(lock, LOCK_NEW)
        pin = next(i for i, c in enumerate(calls) if c.endswith(f"{LOCK_OLD} update self --pin"))
        pull = calls.index(f"pull {LOCK_NEW}")
        self.assertLess(pin, pull, "the digest is pulled only after it was verified")
        self.assertNotIn("pull debeski/composer:latest", calls, "a pinned project never pulls the moving tag")
        self.assertFalse(self._launches(calls))

    def test_the_first_pin_trusts_the_channel_tag_once(self):
        result, calls, lock = self._run("update", "self", can_pin=("debeski/composer:latest",))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("pull debeski/composer:latest", calls)
        self.assertTrue(any(c.endswith("debeski/composer:latest update self --pin") for c in calls), calls)
        self.assertEqual(lock, LOCK_NEW)
        self.assertIn(f"pull {LOCK_NEW}", calls)

    def test_a_deployer_from_before_pinning_stays_on_the_tag(self):
        result, calls, lock = self._run("update", "self")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("predates digest pinning", result.stdout)
        self.assertIsNone(lock)
        self.assertFalse([c for c in calls if "update self --pin" in c])

    def test_a_refused_pin_fails_update_self_and_keeps_the_old_pin(self):
        result, calls, lock = self._run("update", "self", lock=LOCK_OLD, local=(LOCK_OLD,), can_pin=(LOCK_OLD,),
                                        pin_exit=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(lock, LOCK_OLD)
        self.assertFalse([c for c in calls if c.startswith("pull")])

    def test_a_refused_pin_still_updates_the_stack_on_the_old_deployer(self):
        result, calls, _ = self._run("update", lock=LOCK_OLD, local=(LOCK_OLD,), can_pin=(LOCK_OLD,), pin_exit=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("could not refresh the Composer deployer", result.stderr)
        self.assertTrue(self._launches(calls)[-1].endswith(f"{LOCK_OLD} update"), calls)

    def test_an_unscoped_update_runs_the_stack_update_on_the_new_pin(self):
        result, calls, _ = self._run("update", lock=LOCK_OLD, local=(LOCK_OLD, LOCK_NEW), can_pin=(LOCK_OLD,))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self._launches(calls)[-1].endswith(f"{LOCK_NEW} update"), calls)

    def test_the_escape_hatch_reaches_composer(self):
        _, calls, _ = self._run("update", "self", lock=LOCK_OLD, local=(LOCK_OLD,), can_pin=(LOCK_OLD,))
        pin = next(c for c in calls if "update self --pin" in c)
        self.assertIn("-e COMPOSER_ALLOW_UNVERIFIED_COMPOSER", pin)


class WrapperPinParityTests(unittest.TestCase):
    """Both wrappers and image_pin accept exactly the same lock text."""

    def test_the_wrappers_use_image_pins_pattern(self):
        from composer import image_pin

        root = Path(__file__).resolve().parents[1]
        expected = r"^debeski/composer:v[0-9]+(\.[0-9]+){1,2}((a|b|rc)[0-9]+)?@sha256:[0-9a-f]{64}$"
        self.assertIn(f"composer_lock_pattern='{expected}'", (root / "start.sh").read_text(encoding="utf-8"))
        ps1 = (root / "start.ps1").read_text(encoding="utf-8")
        self.assertIn(f"$composerLockPattern = '{expected}'", ps1)
        self.assertIn("-cmatch $composerLockPattern", ps1, "hex must be lowercase, as in image_pin")
        import re

        for text in (LOCK_OLD, LOCK_NEW, "debeski/composer:v1.11.0rc1@" + DIGEST_NEW,
                     "debeski/composer:latest", "debeski/composer:v1.11.0.dev1@" + DIGEST_NEW,
                     "debeski/composer:v1.11.0@sha256:" + "A" * 64):
            self.assertEqual(bool(re.match(expected, text)), image_pin.parse_ref(text) is not None, text)

    def test_the_image_advertises_that_it_can_pin(self):
        root = Path(__file__).resolve().parents[1]
        self.assertIn('LABEL org.dlux.composer.image-pin="1"', (root / "Dockerfile").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
