import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from composer import session
from composer.launcher import DockerComposeLauncher
from composer.progress import PullProgress, format_bar, format_size


DOCKER_PULL = """latest: Pulling from debeski/composer
9824c27679d3: Pulling fs layer
31e352740f53: Pulling fs layer
0f8b424aa0b9: Already exists
9824c27679d3: Downloading [====>                    ]  5MB/20MB
9824c27679d3: Downloading [==================>      ]  15MB/20MB
9824c27679d3: Verifying Checksum
9824c27679d3: Download complete
31e352740f53: Downloading [==========>              ]  4MB/8MB
31e352740f53: Download complete
9824c27679d3: Extracting [==============>          ]  12MB/20MB
9824c27679d3: Pull complete
31e352740f53: Pull complete
Digest: sha256:7f8b9c
Status: Downloaded newer image for debeski/composer:latest
""".splitlines()

COMPOSE_PULL = """ web Pulling
 9824c27679d3 Pulling fs layer
 9824c27679d3 Downloading [=====>                   ]  2.5MB/25MB
 9824c27679d3 Download complete
 9824c27679d3 Pull complete
 web Pulled
""".splitlines()

COMPOSE_UP_TAIL = [
    " Container demo-db-1  Created",
    " Container demo-web-1  Waiting",
    " Container demo-web-1  Healthy",
]


class FormattingTests(unittest.TestCase):
    def test_bar_fills_proportionally_and_clamps(self):
        self.assertEqual(format_bar(0.0, width=10), "░" * 10)
        self.assertEqual(format_bar(0.5, width=10), "█" * 5 + "░" * 5)
        self.assertEqual(format_bar(1.0, width=10), "█" * 10)
        self.assertEqual(format_bar(2.5, width=10), "█" * 10)
        self.assertEqual(format_bar(-1.0, width=10), "░" * 10)

    def test_sizes_are_human_readable(self):
        self.assertEqual(format_size(512), "512B")
        self.assertEqual(format_size(2500), "2.5kB")
        self.assertEqual(format_size(24_300_000), "24.3MB")
        self.assertEqual(format_size(1_500_000_000), "1.5GB")


class DockerPullParsingTests(unittest.TestCase):
    def test_layers_and_percentage_advance_through_a_pull(self):
        progress = PullProgress()

        progress.feed("latest: Pulling from debeski/composer")
        self.assertTrue(progress.active)
        self.assertEqual(progress.image, "debeski/composer")
        self.assertEqual(progress.fraction, 0.0)

        for line in DOCKER_PULL[1:4]:
            progress.feed(line)
        self.assertEqual(len(progress.layers), 3)
        self.assertEqual(progress.completed_layers, 1)  # "Already exists"

        seen = []
        for line in DOCKER_PULL[4:]:
            progress.feed(line)
            seen.append(progress.fraction)

        self.assertEqual(seen, sorted(seen), "progress must never go backwards")
        self.assertEqual(progress.fraction, 1.0)
        self.assertEqual(progress.completed_layers, 3)

    def test_byte_counts_come_from_downloading_lines(self):
        progress = PullProgress()
        progress.feed("9824c27679d3: Pulling fs layer")
        progress.feed("9824c27679d3: Downloading [====>    ]  5MB/20MB")
        self.assertEqual(progress.byte_summary(), "5.0MB/20.0MB")
        # Half the layer's weight is the download, so 5/20 lands at 12.5%.
        self.assertAlmostEqual(progress.fraction, 0.125)

        progress.feed("9824c27679d3: Download complete")
        self.assertEqual(progress.byte_summary(), "20.0MB/20.0MB")
        self.assertAlmostEqual(progress.fraction, 0.5)

    def test_up_to_date_pull_completes_without_layers(self):
        progress = PullProgress()
        progress.feed("Status: Image is up to date for debeski/composer:latest")
        self.assertEqual(progress.fraction, 1.0)
        self.assertIn("100%", progress.bar())

    def test_bar_and_summary_describe_the_same_state(self):
        progress = PullProgress()
        for line in DOCKER_PULL[:7]:
            progress.feed(line)
        self.assertIn("layers", progress.summary())
        self.assertIn(f"{int(progress.fraction * 100)}%", progress.summary())
        self.assertIn("█", progress.bar())


class ComposePullParsingTests(unittest.TestCase):
    def test_compose_service_and_layer_lines(self):
        progress = PullProgress()
        for line in COMPOSE_PULL:
            progress.feed(line)
        self.assertEqual(progress.services, {"web": True})
        self.assertEqual(progress.completed_layers, 1)
        self.assertEqual(progress.fraction, 1.0)

    def test_service_only_output_still_reaches_full(self):
        """An already-current image pulls no layers at all."""
        progress = PullProgress()
        progress.feed(" web Pulling")
        self.assertEqual(progress.fraction, 0.0)
        progress.feed(" web Pulled")
        self.assertEqual(progress.fraction, 1.0)
        self.assertIn("1/1 images", progress.summary())

    def test_container_lifecycle_lines_are_not_pull_output(self):
        progress = PullProgress()
        for line in COMPOSE_UP_TAIL:
            self.assertFalse(progress.feed(line), line)
        self.assertFalse(progress.active)

    def test_blank_and_unrelated_lines_are_ignored(self):
        progress = PullProgress()
        for line in ("", "   ", "error during connect", "#4 [2/6] RUN apt-get"):
            self.assertFalse(progress.feed(line), line)


TWO_SERVICE_PULL = """ postgres Pulling
 web Pulling
 0f8b424aa0b9 Already exists
 3c1f2e8a7b19 Already exists
 9824c27679d3 Pulling fs layer
 31e352740f53 Pulling fs layer
 postgres Pulled
 9824c27679d3 Downloading [====>      ]  8MB/48MB
 9824c27679d3 Download complete
 31e352740f53 Download complete
 9824c27679d3 Pull complete
 31e352740f53 Pull complete
 web Pulled""".splitlines()


class PullScopeTests(unittest.TestCase):
    """The bar must never name an image other than the one being pulled.

    Compose interleaves both services' layers and never attributes a layer to a
    service, so showing the last service it mentioned pointed at the wrong
    image ("postgres" while web's layers downloaded).
    """

    def test_scope_narrows_to_the_image_still_pulling(self):
        progress = PullProgress()
        for line in TWO_SERVICE_PULL[:2]:
            progress.feed(line)
        self.assertEqual(progress.scope(), "postgres, web")

        for line in TWO_SERVICE_PULL[2:7]:  # through "postgres Pulled"
            progress.feed(line)
        self.assertEqual(progress.scope(), "web")
        self.assertIn("web ", progress.bar())
        self.assertNotIn("postgres", progress.bar())

    def test_scope_falls_back_to_the_pulled_repository(self):
        progress = PullProgress()
        progress.feed("latest: Pulling from debeski/composer")
        self.assertEqual(progress.scope(), "debeski/composer")

    def test_many_services_are_abbreviated(self):
        progress = PullProgress()
        for name in ("web", "postgres", "redis", "worker"):
            progress.feed(f" {name} Pulling")
        self.assertEqual(progress.scope(), "web, postgres +2")

    def test_reused_layers_are_reported_as_cached(self):
        progress = PullProgress()
        for line in TWO_SERVICE_PULL:
            progress.feed(line)
        self.assertEqual(len(progress.cached), 2)
        self.assertIn("4/4 layers (2 cached)", progress.bar())

    def test_progress_never_goes_backwards_or_peaks_early(self):
        progress = PullProgress()
        seen = []
        for line in TWO_SERVICE_PULL:
            progress.feed(line)
            seen.append(progress.fraction)

        self.assertEqual(seen, sorted(seen))
        # Cached layers alone must not read as a finished pull.
        self.assertLess(max(seen[:6]), 1.0)
        self.assertEqual(seen[-1], 1.0)

    def test_completion_needs_every_service_to_report(self):
        progress = PullProgress()
        progress.feed(" web Pulling")
        progress.feed(" postgres Pulling")
        progress.feed(" postgres Pulled")
        self.assertFalse(progress.complete)
        self.assertLess(progress.fraction, 1.0)
        progress.feed(" web Pulled")
        self.assertTrue(progress.complete)
        self.assertEqual(progress.fraction, 1.0)


class EmitPullProgressTests(unittest.TestCase):
    def setUp(self):
        self.launcher = DockerComposeLauncher()

    def tearDown(self):
        session._reset_for_tests()

    def _emit(self, lines):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            for line in lines:
                self.launcher.emit_pull_progress("Pull", line)
        return buffer.getvalue()

    def test_pull_lines_draw_the_bar_in_place(self):
        output = self._emit(DOCKER_PULL)
        self.assertIn("█", output)
        self.assertIn("[Pull]", output)
        self.assertIn("100%", output)
        self.assertIn("\r\033[2K", output)  # one repainted line, not a scroll
        self.assertNotIn("\n", output)

    def test_non_pull_lines_keep_the_plain_status_line(self):
        output = self._emit([" Container demo-web-1  Healthy"])
        self.assertIn("Container demo-web-1", output)
        self.assertNotIn("█", output)

    def test_detached_run_logs_coarse_summaries_without_cursor_control(self):
        session._detached = True
        output = self._emit(DOCKER_PULL)
        self.assertNotIn("█", output)
        self.assertNotIn("\033[2K", output)
        self.assertIn("100%", output)
        self.assertIn("layers", output)

    def test_console_log_records_the_summary(self):
        with patch.object(DockerComposeLauncher, "append_console") as console:
            self._emit(DOCKER_PULL)
        logged = [call.args[0] for call in console.call_args_list]
        self.assertTrue(logged)
        self.assertTrue(all(entry.startswith("[Pull] ") for entry in logged))
        self.assertIn("[Pull] debeski/composer · 100% · 3/3 layers (1 cached)", logged)

    def test_repeated_state_is_not_redrawn(self):
        output = self._emit(["9824c27679d3: Pulling fs layer"] * 5)
        self.assertEqual(output.count("[Pull]"), 1)

    def test_progress_line_is_closed_before_later_output(self):
        self.launcher.last_progress_text = "50% · 1/2 layers"
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.launcher.finish_progress_line()
        self.assertEqual(buffer.getvalue(), "\n")
        self.assertEqual(self.launcher.last_progress_text, "")


if __name__ == "__main__":
    unittest.main()
