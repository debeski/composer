import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import Mock

from composer.agent_installer import (
    _transform_post_start_to_label,
    enable_post_start_label,
)
from composer.constants import DEFAULT_MIGRATOR_COMMAND, SERVICE_HEALTHY
from composer.post_start_hooks import PostStartHooksMixin

MIGRATOR = "python -m dlux.updater.supervisor --no-watch -- python manage.py migrator"

# The generated shape: the migrator declared as a label composer reads, with no
# native post_start hook for Compose to run a second, unflagged copy of.
LABELLED_COMPOSE = """name: demo

services:
  db:
    image: postgres:17
  web:
    image: ${WEB_IMAGE:-demo:latest}
    labels:
      org.dlux.restart: "safe"
      org.dlux.post-start: "%s"
    entrypoint: ["/app/entrypoint.sh"]
    volumes:
      - static:/app/staticfiles:rw
volumes:
  static:
""" % MIGRATOR

# Pre-label deployments: Compose runs this hook itself.
LEGACY_COMPOSE = """name: demo

services:
  db:
    image: postgres:17
  web:
    image: ${WEB_IMAGE:-demo:latest}
    restart: always
    labels:
      org.dlux.restart: "safe"
    entrypoint: ["/app/entrypoint.sh"]
    post_start:
      # keep collectstatic on the runtime-active release
      - command: %s
    volumes:
      - static:/app/staticfiles:rw
  celery:
    image: ${WEB_IMAGE:-demo:latest}
volumes:
  static:
""" % MIGRATOR


class FakeLauncher(PostStartHooksMixin):
    """Minimal host for the mixin: only the attributes the hooks read."""

    def __init__(self, config=None, compose_files=(), **flags):
        self._config = config
        self.active_compose_files = list(compose_files)
        self.service_state = {"web": SERVICE_HEALTHY, "celery": SERVICE_HEALTHY}
        self.skip_post_start = flags.get("skip_post_start", False)
        self.no_migrate = flags.get("no_migrate", False)
        self.force_makemigrations = flags.get("force_makemigrations", False)
        self.target_app = flags.get("target_app", None)
        self.status = []
        self.executed = []

    def compose_config_json(self):
        return self._config

    def emit_status(self, label, message):
        self.status.append((label, message))

    def build_failure_detail(self, out, err):
        return f"{out}{err}"

    def run_docker_compose(self, args, **kwargs):
        self.executed.append(args)
        return True, "", ""

    def run_docker_compose_streaming(self, args, **kwargs):
        self.executed.append(args)
        return True, "", ""

    def finish_progress_line(self):
        pass


def _config(labels):
    return {"services": {"web": {"image": "demo:latest", "labels": labels}}}


class PostStartDiscoveryTests(unittest.TestCase):
    def test_label_is_the_canonical_source(self):
        launcher = FakeLauncher(_config({"org.dlux.post-start": MIGRATOR}))
        commands, legacy = launcher.parse_post_start_commands()
        self.assertEqual(commands, [("web", MIGRATOR)])
        self.assertFalse(legacy)

    def test_list_form_labels_are_understood(self):
        launcher = FakeLauncher(_config([f"org.dlux.post-start={MIGRATOR}", "other=1"]))
        commands, _ = launcher.parse_post_start_commands()
        self.assertEqual(commands, [("web", MIGRATOR)])

    def test_services_without_the_label_contribute_nothing(self):
        launcher = FakeLauncher(_config({"org.dlux.restart": "safe"}))
        self.assertEqual(launcher.parse_post_start_commands(), ([], False))

    def test_existing_dlux_updater_stack_gets_compatibility_migrator(self):
        config = {
            "services": {
                "web": {"image": "demo:latest"},
                "dlux-updater": {
                    "image": "demo:latest",
                    "command": [
                        "python", "-m", "tools.dlux_runtime_supervisor", "--no-watch", "--",
                        "python", "manage.py", "migrator",
                    ],
                },
            }
        }
        launcher = FakeLauncher(config)
        commands, legacy = launcher.parse_post_start_commands()
        self.assertEqual(
            commands,
            [(
                "web",
                "python -m tools.dlux_runtime_supervisor --no-watch -- python manage.py migrator",
            )],
        )
        self.assertFalse(legacy)
        launcher.run_post_start_hooks()
        self.assertTrue(any("compatibility migrator" in msg for _, msg in launcher.status))

    def test_legacy_post_start_block_is_a_flagged_fallback(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "compose.yml"
            path.write_text(LEGACY_COMPOSE)
            launcher = FakeLauncher(None, compose_files=[str(path)])
            commands, legacy = launcher.parse_post_start_commands()
        self.assertEqual(commands, [("web", MIGRATOR)])
        self.assertTrue(legacy)

    def test_an_override_repeating_the_block_does_not_queue_it_twice(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / "compose.yml"
            base.write_text(LEGACY_COMPOSE)
            override = Path(tmp) / "compose.dev.yml"
            override.write_text(LEGACY_COMPOSE)
            launcher = FakeLauncher(None, compose_files=[str(base), str(override)])
            commands, _ = launcher.parse_post_start_commands()
        self.assertEqual(commands, [("web", MIGRATOR)])


class MigratorFlagTests(unittest.TestCase):
    def test_bare_run_passes_no_flags(self):
        self.assertEqual(FakeLauncher(None).migrator_flags(), [])

    def test_mm_forces_makemigrations(self):
        launcher = FakeLauncher(None, force_makemigrations=True)
        self.assertEqual(launcher.migrator_flags(), ["-mm"])

    def test_nm_skips_migrations(self):
        launcher = FakeLauncher(None, no_migrate=True)
        self.assertEqual(launcher.migrator_flags(), ["-nm"])

    def test_target_app_is_forwarded_alongside_mm(self):
        launcher = FakeLauncher(None, force_makemigrations=True, target_app="shop")
        self.assertEqual(launcher.migrator_flags(), ["-a", "shop", "-mm"])


class RunPostStartHooksTests(unittest.TestCase):
    def test_flags_reach_the_migrator_through_the_supervisor(self):
        launcher = FakeLauncher(
            _config({"org.dlux.post-start": MIGRATOR}), force_makemigrations=True
        )
        ok, detail = launcher.run_post_start_hooks()
        self.assertTrue(ok, detail)
        # argparse.REMAINDER in the supervisor forwards everything after `--`.
        self.assertEqual(
            launcher.executed,
            [[
                "exec", "-T", "web",
                "python", "-m", "dlux.updater.supervisor", "--no-watch", "--",
                "python", "manage.py", "migrator", "-mm",
            ]],
        )

    def test_nm_still_runs_the_hook_so_static_is_collected(self):
        launcher = FakeLauncher(_config({"org.dlux.post-start": MIGRATOR}), no_migrate=True)
        ok, _ = launcher.run_post_start_hooks()
        self.assertTrue(ok)
        self.assertEqual(len(launcher.executed), 1)
        self.assertIn("-nm", launcher.executed[0])

    def test_skip_post_start_suppresses_the_hook_entirely(self):
        launcher = FakeLauncher(
            _config({"org.dlux.post-start": MIGRATOR}), skip_post_start=True
        )
        ok, _ = launcher.run_post_start_hooks()
        self.assertTrue(ok)
        self.assertEqual(launcher.executed, [])

    def test_non_migrator_commands_get_no_flags(self):
        launcher = FakeLauncher(
            _config({"org.dlux.post-start": "python manage.py warm_cache"}),
            force_makemigrations=True,
        )
        launcher.run_post_start_hooks()
        self.assertEqual(launcher.executed, [["exec", "-T", "web", "python", "manage.py", "warm_cache"]])

    def test_unhealthy_hook_service_is_a_failure_not_a_silent_skip(self):
        launcher = FakeLauncher(_config({"org.dlux.post-start": MIGRATOR}))
        launcher.service_state["web"] = "failed"
        ok, detail = launcher.run_post_start_hooks()
        self.assertFalse(ok)
        self.assertIn("not healthy", detail)
        self.assertEqual(launcher.executed, [])

    def test_a_legacy_block_is_run_but_announced(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "compose.yml"
            path.write_text(LEGACY_COMPOSE)
            launcher = FakeLauncher(None, compose_files=[str(path)])
            launcher.run_post_start_hooks()
        self.assertEqual(len(launcher.executed), 1)
        self.assertTrue(any(label == "Note" for label, _ in launcher.status))


class PostStartLabelMigrationTests(unittest.TestCase):
    def test_hook_becomes_a_label_and_the_body_survives(self):
        updated = _transform_post_start_to_label(LEGACY_COMPOSE, "demo")
        self.assertNotIn("post_start:", updated)
        self.assertIn(f'org.dlux.post-start: "{MIGRATOR}"', updated)
        # The regex must not run past the hook and eat the rest of the service.
        self.assertIn("      - static:/app/staticfiles:rw", updated)
        self.assertIn('org.dlux.restart: "safe"', updated)
        self.assertIn("  celery:", updated)

    def test_already_labelled_compose_is_left_alone(self):
        self.assertEqual(
            _transform_post_start_to_label(LABELLED_COMPOSE, "demo"), LABELLED_COMPOSE
        )

    def test_existing_dlux_updater_without_any_hook_gets_default_label(self):
        missing = LEGACY_COMPOSE.replace(
            "    post_start:\n      # keep collectstatic on the runtime-active release\n"
            f"      - command: {MIGRATOR}\n",
            "",
        ).replace("  db:\n", "  dlux-updater:\n    image: demo:latest\n  db:\n", 1)
        updated = _transform_post_start_to_label(missing, "demo")
        self.assertIn(f'org.dlux.post-start: "{DEFAULT_MIGRATOR_COMMAND}"', updated)
        self.assertEqual(_transform_post_start_to_label(updated, "demo"), updated)

    def test_missing_hook_preserves_legacy_supervisor_module(self):
        missing = LEGACY_COMPOSE.replace(
            "    post_start:\n      # keep collectstatic on the runtime-active release\n"
            f"      - command: {MIGRATOR}\n",
            "",
        ).replace(
            "  db:\n",
            "  dlux-updater:\n"
            "    image: demo:latest\n"
            "    command: [\"python\", \"-m\", \"tools.dlux_runtime_supervisor\", \"--no-watch\", \"--\", \"python\", \"manage.py\", \"migrator\"]\n"
            "  db:\n",
            1,
        )
        updated = _transform_post_start_to_label(missing, "demo")
        self.assertIn("python -m tools.dlux_runtime_supervisor --no-watch", updated)

    def test_transform_is_idempotent(self):
        once = _transform_post_start_to_label(LEGACY_COMPOSE, "demo")
        self.assertEqual(_transform_post_start_to_label(once, "demo"), once)

    def test_a_multi_command_hook_is_refused(self):
        crowded = LEGACY_COMPOSE.replace(
            f"      - command: {MIGRATOR}\n",
            f"      - command: {MIGRATOR}\n      - command: python manage.py warm_cache\n",
        )
        self.assertEqual(_transform_post_start_to_label(crowded, "demo"), crowded)

    def test_dry_run_reports_the_file_without_writing(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "compose.yml"
            path.write_text(LEGACY_COMPOSE)
            result = enable_post_start_label(tmp, apply=False)
            self.assertEqual(result["files"], ["compose.yml"])
            self.assertFalse(result["applied"])
            self.assertEqual(path.read_text(), LEGACY_COMPOSE)

    def test_apply_validates_backs_up_and_writes(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "compose.yml"
            path.write_text(LEGACY_COMPOSE)
            runner = Mock(return_value=Mock(returncode=0, stdout="", stderr=""))
            result = enable_post_start_label(tmp, apply=True, command_runner=runner)
            self.assertTrue(result["applied"])
            self.assertTrue(result["backup_root"])
            self.assertNotIn("post_start:", path.read_text())
            self.assertIn("org.dlux.post-start:", path.read_text())
            backup = Path(result["backup_root"]) / "compose.yml"
            self.assertEqual(backup.read_text(), LEGACY_COMPOSE)

    def test_apply_refuses_when_compose_config_rejects_the_candidate(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "compose.yml"
            path.write_text(LEGACY_COMPOSE)
            runner = Mock(
                side_effect=[
                    Mock(returncode=0, stdout="", stderr=""),
                    Mock(returncode=1, stdout="", stderr="bad yaml"),
                ]
            )
            with self.assertRaises(Exception):
                enable_post_start_label(tmp, apply=True, command_runner=runner)
            self.assertEqual(path.read_text(), LEGACY_COMPOSE)


if __name__ == "__main__":
    unittest.main()
