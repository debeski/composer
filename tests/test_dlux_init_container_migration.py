"""Retiring dlux-updater into Compose init containers on an existing stack.

DjangoLux 1.8.0 hands update execution to Composer, which leaves that service
with only boot work. These tests pin the compose transform `check --fix` applies
to a deployed pre-1.8.0 project, including the refusals — a stack this cannot
recognize must be left alone, not guessed at.
"""

import re
import unittest

import yaml

from composer.agent_installer import AgentInstallError, _transform_to_init_containers

DEPLOYED = """name: demoproj

services:
  db:
    image: postgres:17
    networks:
      - internal

  web:
    image: ${WEB_IMAGE:-demoproj:latest}
    labels:
      org.dlux.restart: "safe"
      org.dlux.post-start: "python -m dlux.updater.supervisor --no-watch -- python manage.py migrator"
    volumes:
      - static:/app/staticfiles:rw
      - dlux_runtime:/opt/dlux-runtime:ro
    depends_on:
      db:
        condition: service_healthy
      dlux-updater:
        condition: service_healthy
    networks:
      - internal

  # DjangoLux updater start
  dlux-updater:
    image: ${WEB_IMAGE:-demoproj:latest}
    restart: always
    labels:
      org.dlux.restart: "protected"
    command: ["python", "-m", "dlux.updater.supervisor", "--no-watch", "--", "bash", "-c", "python manage.py dlux_reconcile; python manage.py migrator && exec python manage.py dlux_update_worker"]
    volumes:
      - dlux_runtime:/opt/dlux-runtime:rw
    networks:
      - egress
      - internal
  # DjangoLux updater end

  celery:
    image: ${WEB_IMAGE:-demoproj:latest}
    command: ["python", "-m", "dlux.updater.supervisor", "--", "python", "-m", "celery", "-A", "config", "worker", "-B"]
    entrypoint: ["/app/entrypoint.sh"]
    volumes:
      - static:/app/staticfiles:ro
      - dlux_runtime:/opt/dlux-runtime:ro
    depends_on:
      db:
        condition: service_healthy
      dlux-updater:
        condition: service_healthy
    networks:
      - internal

volumes:
  dlux_runtime:
  static:

networks:
  egress:
    driver: bridge
  internal:
    internal: true
"""


def _migrate(contents=DEPLOYED):
    return _transform_to_init_containers(contents, "demoproj")


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.output = _migrate()
        self.model = yaml.safe_load(self.output)

    def test_the_result_is_valid_yaml_with_the_service_gone(self):
        self.assertNotIn("dlux-updater", self.model["services"])

    def test_the_init_containers_land_on_celery_not_web(self):
        """A step inherits its service's mounts; web's runtime volume is ro."""
        self.assertIn("pre_start", self.model["services"]["celery"])
        self.assertNotIn("pre_start", self.model["services"]["web"])

    def test_reconcile_runs_before_the_migrator(self):
        steps = self.model["services"]["celery"]["pre_start"]

        self.assertEqual(len(steps), 2)
        self.assertIn("dlux_reconcile", " ".join(steps[0]["command"]))
        self.assertIn("migrator", " ".join(steps[1]["command"]))

    def test_every_step_bypasses_the_boot_gate(self):
        for step in self.model["services"]["celery"]["pre_start"]:
            self.assertEqual(step["environment"]["DLUX_BOOT_GATE"], "off")

    def test_the_migrator_step_takes_the_deploy_flags(self):
        rendered = " ".join(self.model["services"]["celery"]["pre_start"][1]["command"])
        self.assertIn("${DLUX_MIGRATOR_FLAGS:-}", rendered)

    def test_celery_gains_write_access_where_the_steps_need_it(self):
        volumes = self.model["services"]["celery"]["volumes"]

        self.assertIn("dlux_runtime:/opt/dlux-runtime:rw", volumes)
        self.assertIn("static:/app/staticfiles:rw", volumes)

    def test_web_keeps_its_read_only_runtime_mount(self):
        self.assertIn("dlux_runtime:/opt/dlux-runtime:ro",
                      self.model["services"]["web"]["volumes"])

    def test_no_service_still_depends_on_the_removed_one(self):
        """An orphan depends_on makes the whole project invalid."""
        for name, spec in self.model["services"].items():
            with self.subTest(service=name):
                self.assertNotIn("dlux-updater", spec.get("depends_on") or {})

    def test_surviving_depends_on_entries_are_kept(self):
        self.assertIn("db", self.model["services"]["celery"]["depends_on"])
        self.assertIn("db", self.model["services"]["web"]["depends_on"])

    def test_the_post_start_migrator_hook_is_dropped(self):
        """It ran after health; the same work now runs before start."""
        self.assertNotIn("org.dlux.post-start", self.model["services"]["web"]["labels"])

    def test_native_post_start_hook_is_dropped_too(self):
        source = DEPLOYED.replace(
            '    labels:\n      org.dlux.restart: "safe"\n      org.dlux.post-start: "python -m dlux.updater.supervisor --no-watch -- python manage.py migrator"\n',
            '    labels:\n      org.dlux.restart: "safe"\n    post_start:\n      - command: python manage.py initialize\n',
        )
        model = yaml.safe_load(_migrate(source))

        self.assertNotIn("post_start", model["services"]["web"])
        self.assertNotIn("org.dlux.post-start", model["services"]["web"]["labels"])

    def test_unrelated_labels_survive(self):
        self.assertEqual(self.model["services"]["web"]["labels"]["org.dlux.restart"], "safe")

    def test_the_named_volumes_are_preserved(self):
        self.assertIn("dlux_runtime", self.model["volumes"])
        self.assertIn("static", self.model["volumes"])

    def test_migrating_twice_changes_nothing(self):
        self.assertEqual(_migrate(self.output), self.output)


class EmptyDependsOnTests(unittest.TestCase):
    """`depends_on:` with no children is not valid Compose."""

    SOURCE = DEPLOYED.replace("""    depends_on:
      db:
        condition: service_healthy
      dlux-updater:
        condition: service_healthy
    networks:
      - internal

volumes:""", """    depends_on:
      dlux-updater:
        condition: service_healthy
    networks:
      - internal

volumes:""")

    def test_the_key_is_removed_when_its_last_entry_goes(self):
        model = yaml.safe_load(_migrate(self.SOURCE))
        self.assertNotIn("depends_on", model["services"]["celery"])


class ComposeValidationTests(unittest.TestCase):
    """The migrated file has to satisfy Compose, not just yaml.safe_load.

    An orphan depends_on or an emptied mapping parses fine and is still a broken
    project — only `docker compose config` catches that.
    """

    @classmethod
    def setUpClass(cls):
        import shutil
        import subprocess

        cls.available = False
        if not shutil.which("docker"):
            return
        probe = subprocess.run(["docker", "compose", "version", "--short"],
                               capture_output=True, text=True)
        if probe.returncode != 0:
            return
        raw = probe.stdout.strip().lstrip("vV").split("-")[0].split(".")[:3]
        try:
            version = tuple(int(p) for p in raw)
        except ValueError:
            return
        cls.available = version + (0,) * (3 - len(version)) >= (5, 3, 0)

    def setUp(self):
        if not self.available:
            self.skipTest("docker compose 5.3.0+ is required to validate the migration")

    def _config(self, contents, env=None):
        import json
        import os
        import subprocess
        import tempfile
        from pathlib import Path

        project = Path(tempfile.mkdtemp())
        (project / "compose.yml").write_text(contents, encoding="utf-8")
        environment = dict(os.environ)
        environment.update(env or {})
        result = subprocess.run(
            ["docker", "compose", "config", "--format", "json"],
            cwd=str(project), capture_output=True, text=True, env=environment,
        )
        self.assertEqual(result.returncode, 0, f"invalid compose:\n{result.stderr}")
        return json.loads(result.stdout)

    def test_the_source_fixture_is_itself_valid(self):
        """Otherwise a passing migration proves nothing."""
        self.assertIn("dlux-updater", self._config(DEPLOYED)["services"])

    def test_the_migrated_file_is_valid(self):
        services = self._config(_migrate())["services"]

        self.assertNotIn("dlux-updater", services)
        self.assertEqual(len(services["celery"]["pre_start"]), 2)

    def test_compose_resolves_the_flags_into_the_migrated_step(self):
        services = self._config(_migrate(), {"DLUX_MIGRATOR_FLAGS": "-nm"})["services"]

        self.assertIn("-nm", " ".join(services["celery"]["pre_start"][1]["command"]))

    def test_a_stack_whose_depends_on_empties_is_still_valid(self):
        self._config(_migrate(EmptyDependsOnTests.SOURCE))


class WithoutMarkersTests(unittest.TestCase):
    """A hand-edited project may have lost the scaffold's block markers."""

    SOURCE = DEPLOYED.replace("  # DjangoLux updater start\n", "").replace(
        "  # DjangoLux updater end\n", "")

    def test_the_service_block_is_still_found_and_removed(self):
        model = yaml.safe_load(_migrate(self.SOURCE))

        self.assertNotIn("dlux-updater", model["services"])
        self.assertIn("pre_start", model["services"]["celery"])


class RefusalTests(unittest.TestCase):
    def test_a_stack_without_celery_is_refused(self):
        """The steps would have nowhere to run."""
        source = re.sub(r"(?ms)^  celery:\n.*?(?=^volumes:)", "", DEPLOYED)
        with self.assertRaises(AgentInstallError):
            _migrate(source)

    def test_a_celery_without_volumes_is_refused(self):
        source = DEPLOYED.replace("""    volumes:
      - static:/app/staticfiles:ro
      - dlux_runtime:/opt/dlux-runtime:ro
""", "")
        with self.assertRaises(AgentInstallError):
            _migrate(source)

    def test_an_already_migrated_stack_is_a_no_op(self):
        migrated = _migrate()
        self.assertEqual(_transform_to_init_containers(migrated, "demoproj"), migrated)


if __name__ == "__main__":
    unittest.main()
