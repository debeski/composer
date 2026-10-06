import unittest

from composer.dlux_package_cli import availability_summary, installed_release


def _payload(version, channel="stable", inline_safe=True):
    return {"available": True, "version": version, "channel": channel, "inline_safe": inline_safe}


class AvailabilitySummaryTests(unittest.TestCase):
    def test_an_older_stable_is_not_announced_to_a_deployment_on_a_beta(self):
        # Seen live: a stack on 1.8.14b1 was told "DjangoLux 1.8.13 available".
        line = availability_summary(_payload("1.8.13"), "1.8.14b1")
        self.assertTrue(line.startswith("Up to date"))
        self.assertIn("1.8.14b1 is newer than 1.8.13", line)
        self.assertNotIn("available", line)

    def test_the_installed_release_is_up_to_date(self):
        line = availability_summary(_payload("1.9.0b1", channel="beta"), "1.9.0b1")
        self.assertEqual(line, "Up to date: DjangoLux 1.9.0b1 is the newest release on the beta channel")

    def test_a_newer_release_is_announced(self):
        line = availability_summary(_payload("1.9.0b1", channel="beta"), "1.8.14b2")
        self.assertEqual(line, "DjangoLux 1.9.0b1 available (inline-safe, beta channel)")

    def test_an_image_rebuild_release_says_so(self):
        line = availability_summary(_payload("2.0.0", inline_safe=False), "1.9.0")
        self.assertIn("requires an image rebuild", line)

    def test_an_unknown_installed_version_keeps_the_plain_announcement(self):
        self.assertEqual(
            availability_summary(_payload("1.8.13"), ""),
            "DjangoLux 1.8.13 available (inline-safe, stable channel)",
        )


    def test_a_recorded_image_version_is_never_called_up_to_date(self):
        # Seen on testbed-dlux: active.json kept source=image, version=1.11.0b1
        # while the image ran 1.9.4, and check said "Up to date".
        line = availability_summary(_payload("1.10.2b1", channel="beta"), "1.11.0b1", verified=False)
        self.assertNotIn("Up to date", line)
        self.assertIn("recorded as 1.11.0b1", line)
        self.assertIn("cannot verify", line)

    def test_a_newer_release_is_announced_whatever_the_source(self):
        line = availability_summary(_payload("1.11.0b2", channel="beta"), "1.9.4", verified=False)
        self.assertEqual(line, "DjangoLux 1.11.0b2 available (inline-safe, beta channel)")

    def test_only_a_volume_release_is_verified(self):
        self.assertEqual(installed_release({"source": "volume", "version": "1.11.0b2"}), ("1.11.0b2", True))
        self.assertEqual(installed_release({"source": "image", "version": "1.11.0b1"}), ("1.11.0b1", False))
        self.assertEqual(installed_release({}), ("", False))


if __name__ == "__main__":
    unittest.main()
