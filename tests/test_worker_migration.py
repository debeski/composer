"""Stack schema 5: `check --fix` gives a stack DjangoLux's `worker` service.

DjangoLux 1.16 runs its tasks with `manage.py dlux_worker` in a `worker` service
that takes over what `celery` did for it. These tests take a schema 4 stack (the
DjangoLux 1.15 scaffold) and a stack still on the old layout (dlux-updater, the
tools/ runtime, the project's maintenance page, public media, unstamped) to
schema 5, and pin when `check --fix` may do it.
"""

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from composer import agent_installer, maintenance_migration, media_migration
from composer import worker_migration as wm
from composer.checkup import OK, WARN
from composer.launcher import DockerComposeLauncher
from composer.stack_schema import read_stamp

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCHEMA_4 = FIXTURES / "schema-4"
OLD_LAYOUT = FIXTURES / "old-layout"
DLUX_CHECKOUTS = Path(__file__).resolve().parents[3] / "pkg-django-lux"


class _Loader(yaml.SafeLoader):
    """Reads Compose override tags (`!reset`, `!override`) as plain values."""


_Loader.add_multi_constructor(
    "!",
    lambda loader, _suffix, node: (
        loader.construct_sequence(node) if isinstance(node, yaml.SequenceNode)
        else loader.construct_mapping(node) if isinstance(node, yaml.MappingNode)
        else loader.construct_scalar(node)
    ),
)


def _load(text):
    return yaml.load(text, Loader=_Loader)


def _copy(source: Path, case: unittest.TestCase) -> Path:
    holder = tempfile.TemporaryDirectory()
    case.addCleanup(holder.cleanup)
    root = Path(holder.name)
    shutil.copytree(source, root, dirs_exist_ok=True)
    return root


def _apply(updates):
    for path, text in updates.items():
        path.write_text(text, encoding="utf-8")


class _FakeCompose:
    """`docker compose config --format json` over the files it is handed."""

    def __init__(self, returncode=0):
        self.returncode = returncode
        self.calls = []

    def __call__(self, args, **_kwargs):
        self.calls.append(list(args))
        model = {"services": {}}
        files = [args[index + 1] for index, arg in enumerate(args) if arg == "-f"]
        for name in files:
            for service, spec in (_load(Path(name).read_text(encoding="utf-8"))["services"] or {}).items():
                model["services"].setdefault(service, {}).update(spec or {})
        return SimpleNamespace(
            returncode=self.returncode,
            stdout=json.dumps(model, default=str),
            stderr="" if self.returncode == 0 else "service worker is invalid",
        )


SCHEMA_4_COMPOSE = (SCHEMA_4 / "compose.yml").read_text(encoding="utf-8")


class Schema4ComposeTests(unittest.TestCase):
    def setUp(self):
        self.migrated = wm.migrate_compose(SCHEMA_4_COMPOSE)
        self.services = _load(self.migrated)["services"]
        self.worker = self.services["worker"]
        self.celery = self.services["celery"]

    def test_the_worker_runs_dlux_worker_under_the_supervisor(self):
        self.assertEqual(
            self.worker["command"],
            ["python", "-m", "dlux.updater.supervisor", "--", "python", "manage.py", "dlux_worker"],
        )
        self.assertEqual(self.worker["healthcheck"]["test"], ["CMD", "python", "manage.py", "dlux_worker", "--check"])
        self.assertEqual(self.worker["stop_grace_period"], "30s")

    def test_the_worker_applies_the_migrations(self):
        steps = [" ".join(step["command"]) for step in self.worker["pre_start"]]
        self.assertIn("dlux_reconcile", steps[0])
        self.assertIn("migrator ${DLUX_MIGRATOR_FLAGS:-}", steps[1])
        self.assertEqual(self.worker["environment"]["DLUX_BOOT_GATE"], "apply")

    def test_the_worker_writes_the_runtime_volume_and_restarts_safely(self):
        self.assertIn("dlux_runtime:/opt/dlux-runtime:rw", self.worker["volumes"])
        self.assertIn("static:/app/staticfiles:rw", self.worker["volumes"])
        self.assertEqual(self.worker["labels"]["org.dlux.restart"], "safe")
        self.assertEqual(self.worker["networks"], ["internal"])
        self.assertEqual(set(self.worker["depends_on"]), {"db", "redis"})

    def test_celery_keeps_the_project_tasks_only(self):
        self.assertNotIn("pre_start", self.celery)
        self.assertNotIn("DLUX_BOOT_GATE", self.celery["environment"])
        self.assertIn("celery", self.celery["command"])
        self.assertIn("inspect", self.celery["healthcheck"]["test"])
        # Project tasks may still write relay requests.
        self.assertIn("dlux_runtime:/opt/dlux-runtime:rw", self.celery["volumes"])

    def test_the_generated_celery_comment_no_longer_claims_dlux_work(self):
        celery_block = self.migrated.split("  celery:\n", 1)[1].split("\n  ocr:", 1)[0]
        self.assertNotIn("dlux_state_tick", celery_block)
        self.assertIn("The project's own Celery tasks", celery_block)

    def test_worker_sits_right_before_celery(self):
        names = list(self.services)
        self.assertEqual(names.index("worker") + 1, names.index("celery"))

    def test_worker_joins_the_restart_list_after_web(self):
        self.assertEqual(
            self.migrated.count('COMPOSER_AGENT_RESTART_SERVICES: "web,worker,celery,smtp-relay,caddy,ocr"'), 2
        )

    def test_migrating_twice_changes_nothing(self):
        self.assertEqual(wm.migrate_compose(self.migrated), self.migrated)

    def test_a_stack_without_celery_is_left_alone(self):
        without = self.migrated.replace(
            self.migrated.split("  celery:\n", 1)[1].split("\n  ocr:", 1)[0], ""
        ).replace("  celery:\n", "")
        self.assertEqual(wm.migrate_compose(without), without)


class ProjectExtrasTests(unittest.TestCase):
    """Archive mounts ScanLink releases; Trademarks merges a second anchor."""

    SOURCE = (
        SCHEMA_4_COMPOSE.replace(
            "x-environment:\n", "x-integrations: &integrations\n  SCANLINK: \"on\"\n\nx-environment:\n", 1
        )
        .replace(
            '      # This is the service that applies migrations. pre_start remains the fast\n'
            '      # path; `apply` is what covers the restart-policy restart Compose skips it\n'
            '      # on, where web and celery would otherwise both wait for a migration\n'
            '      # nothing was left to run.\n'
            '      DLUX_BOOT_GATE: "apply"\n',
            "",
        )
    )

    def _with_extras(self):
        start = self.SOURCE.index("  celery:\n")
        celery, rest = self.SOURCE[start:].split("\n  ocr:", 1)
        celery = celery.replace(
            "    volumes:\n", "    container_name: demo_celery\n    volumes:\n      - ./scanlink_releases/:/app/scanlink_releases:ro\n", 1
        ).replace("      <<: *de\n", "      <<: [*de, *integrations]\n", 1)
        return self.SOURCE[:start] + celery + "\n  ocr:" + rest

    def test_extra_mounts_and_anchors_come_along(self):
        worker = _load(wm.migrate_compose(self._with_extras()))["services"]["worker"]
        self.assertIn("./scanlink_releases/:/app/scanlink_releases:ro", worker["volumes"])
        self.assertEqual(worker["environment"]["SCANLINK"], "on")

    def test_a_missing_boot_gate_is_added_where_the_migrations_run(self):
        services = _load(wm.migrate_compose(self._with_extras()))["services"]
        self.assertEqual(services["worker"]["environment"]["DLUX_BOOT_GATE"], "apply")
        self.assertNotIn("DLUX_BOOT_GATE", services["celery"]["environment"])

    def test_a_container_name_stays_with_celery(self):
        services = _load(wm.migrate_compose(self._with_extras()))["services"]
        self.assertNotIn("container_name", services["worker"])
        self.assertEqual(services["celery"]["container_name"], "demo_celery")


class RefusalTests(unittest.TestCase):
    def test_dlux_updater_must_be_retired_first(self):
        with self.assertRaises(wm.WorkerMigrationError):
            wm.migrate_compose((OLD_LAYOUT / "compose.yml").read_text(encoding="utf-8"))

    def test_an_inline_environment_is_refused(self):
        start = SCHEMA_4_COMPOSE.index("  celery:\n")
        celery, rest = SCHEMA_4_COMPOSE[start:].split("\n  ocr:", 1)
        environment = celery[celery.index("    environment:\n"):celery.index("    healthcheck:\n")]
        source = SCHEMA_4_COMPOSE[:start] + celery.replace(environment, "    environment: *de\n") + "\n  ocr:" + rest
        with self.assertRaises(wm.WorkerMigrationError):
            wm.migrate_compose(source)


class DevOverrideTests(unittest.TestCase):
    def test_the_celery_override_is_mirrored_for_worker(self):
        source = (SCHEMA_4 / "compose.dev.yml").read_text(encoding="utf-8")
        migrated = wm.migrate_dev_override(source)
        services = _load(migrated)["services"]
        self.assertEqual(services["worker"]["build"], ".")
        self.assertEqual(services["worker"]["volumes"], services["celery"]["volumes"])
        self.assertEqual(services["worker"]["environment"], {"DLUX_INLINE_UPDATES_ENABLED": "False"})
        self.assertIn("worker runs the update tasks", migrated)
        self.assertEqual(wm.migrate_dev_override(migrated), migrated)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.root = _copy(SCHEMA_4, self)

    def test_a_deployment_restamps_everything_but_the_waiting_dev_override(self):
        _sources, updates, notes = wm._plan(self.root, ["compose.yml"])
        changed = {str(path.relative_to(self.root.resolve())) for path in updates}
        self.assertEqual(changed, {"compose.yml", "Dockerfile", "entrypoint.sh", ".proxy/Caddyfile"})
        for path, text in updates.items():
            self.assertEqual(read_stamp(text), 5, path)
        self.assertTrue(any("compose.dev.yml" in note for note in notes))

    def test_development_mirrors_the_override_and_restamps_it(self):
        _sources, updates, notes = wm._plan(self.root, ["compose.yml", "compose.dev.yml"])
        dev = (self.root / "compose.dev.yml").resolve()
        self.assertIn("worker", _load(updates[dev])["services"])
        self.assertEqual(read_stamp(updates[dev]), 5)
        self.assertEqual(notes, [])

    def test_pending_tracks_each_step(self):
        self.assertEqual(
            wm.pending(str(self.root), ["compose.yml"]), {"compose": True, "dev": False, "stamp": False}
        )
        _apply(wm._plan(self.root, ["compose.yml"])[1])
        self.assertEqual(
            wm.pending(str(self.root), ["compose.yml"]), {"compose": False, "dev": False, "stamp": False}
        )
        # The development override is mirrored by the next `-d check --fix`.
        self.assertEqual(
            wm.pending(str(self.root), ["compose.yml", "compose.dev.yml"]),
            {"compose": False, "dev": True, "stamp": False},
        )
        _apply(wm._plan(self.root, ["compose.yml", "compose.dev.yml"])[1])
        self.assertFalse(any(wm.pending(str(self.root), ["compose.yml", "compose.dev.yml"]).values()))

    def test_a_missing_compose_file_is_nothing_to_do(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertFalse(any(wm.pending(empty, ["compose.yml"]).values()))


class MigrateTests(unittest.TestCase):
    def setUp(self):
        self.root = _copy(SCHEMA_4, self)
        self.original = (self.root / "compose.yml").read_text(encoding="utf-8")

    def test_it_validates_writes_and_archives(self):
        compose = _FakeCompose()
        with patch("composer.worker_migration.shutil.which", return_value="/usr/bin/docker"):
            outcome = wm.migrate(str(self.root), ["compose.yml"], command_runner=compose)

        self.assertTrue(outcome["applied"])
        self.assertIn("compose.yml", outcome["files"])
        self.assertEqual(len(compose.calls), 1)
        self.assertIn("config", compose.calls[0])
        self.assertIn("worker", _load((self.root / "compose.yml").read_text(encoding="utf-8"))["services"])
        archived = Path(outcome["backup_root"]) / "worker-migration" / "compose.yml"
        self.assertEqual(archived.read_text(encoding="utf-8"), self.original)
        self.assertEqual(list(self.root.glob(".*.tmp")), [], "staged candidates must be removed")

    def test_a_failed_validation_changes_nothing(self):
        with patch("composer.worker_migration.shutil.which", return_value="/usr/bin/docker"):
            with self.assertRaises(wm.StackCleanupError):
                wm.migrate(str(self.root), ["compose.yml"], command_runner=_FakeCompose(returncode=1))

        self.assertEqual((self.root / "compose.yml").read_text(encoding="utf-8"), self.original)
        self.assertFalse((self.root / ".xclude").exists())

    def test_it_needs_docker_to_validate(self):
        with patch("composer.worker_migration.shutil.which", return_value=None):
            with self.assertRaises(wm.WorkerMigrationError):
                wm.migrate(str(self.root), ["compose.yml"], command_runner=_FakeCompose())

    def test_an_override_that_restores_celery_init_containers_is_refused(self):
        def keeps_pre_start(args, **kwargs):
            result = _FakeCompose()(args, **kwargs)
            model = json.loads(result.stdout)
            model["services"]["celery"]["pre_start"] = [{"command": "migrator"}]
            result.stdout = json.dumps(model)
            return result

        with patch("composer.worker_migration.shutil.which", return_value="/usr/bin/docker"):
            with self.assertRaises(wm.WorkerMigrationError):
                wm.migrate(str(self.root), ["compose.yml"], command_runner=keeps_pre_start)


class OldLayoutTests(unittest.TestCase):
    """dhub, dlux-panel and min-survey reach schema 5 in one `check --fix`.

    The steps run in `check --fix` order: maintenance page (3), stored files (4),
    dlux-updater retirement, the dev override, the packaged runtime (2), restart
    labels, then worker (5).
    """

    def setUp(self):
        self.root = _copy(OLD_LAYOUT, self)

    def _earlier_steps(self, *, maintenance=True):
        compose = (self.root / "compose.yml").read_text(encoding="utf-8")
        caddy = (self.root / ".proxy" / "Caddyfile").read_text(encoding="utf-8")
        dev = (self.root / "compose.dev.yml").read_text(encoding="utf-8")
        if maintenance:
            compose = maintenance_migration.migrate_compose(compose)
            caddy = maintenance_migration.migrate_caddyfile(caddy)
        caddy = media_migration.migrate_caddyfile(caddy)
        compose = agent_installer._transform_to_init_containers(compose, "oldstack")
        dev = agent_installer._transform_dev_init_override(dev, "oldstack")
        compose = agent_installer._migrate_dlux_updater_command(compose, "oldstack")
        compose = agent_installer._ensure_restart_labels(compose, "oldstack")
        (self.root / "compose.yml").write_text(compose, encoding="utf-8")
        (self.root / ".proxy" / "Caddyfile").write_text(caddy, encoding="utf-8")
        (self.root / "compose.dev.yml").write_text(dev, encoding="utf-8")

    def test_the_old_layout_reaches_schema_5(self):
        self._earlier_steps()
        _sources, updates, notes = wm._plan(self.root, ["compose.yml", "compose.dev.yml"])
        _apply(updates)
        compose = (self.root / "compose.yml").read_text(encoding="utf-8")
        services = _load(compose)["services"]
        dev = _load((self.root / "compose.dev.yml").read_text(encoding="utf-8"))["services"]

        self.assertEqual(notes, [])
        self.assertEqual(read_stamp(compose), 5)
        self.assertEqual(_load(compose)["x-environment"]["DLUX_STACK_SCHEMA"], "5")
        self.assertNotIn("dlux-updater", services)
        self.assertEqual(len(services["worker"]["pre_start"]), 2)
        self.assertNotIn("pre_start", services["celery"])
        self.assertNotIn("tools.dlux_runtime_supervisor", compose)
        self.assertIn("./imports/:/app/imports:ro", services["worker"]["volumes"])
        self.assertIn("dlux_runtime:/opt/dlux-runtime:rw", dev["worker"]["volumes"])
        self.assertIn('COMPOSER_AGENT_RESTART_SERVICES: "web,worker,celery,caddy"', compose)

    def test_a_stack_that_is_not_yet_schema_3_stays_unstamped_until_it_is(self):
        self._earlier_steps(maintenance=False)
        _sources, updates, notes = wm._plan(self.root, ["compose.yml"])
        _apply(updates)
        compose = (self.root / "compose.yml").read_text(encoding="utf-8")
        self.assertIn("worker", _load(compose)["services"])
        self.assertIsNone(read_stamp(compose))
        self.assertTrue(any("stack schema 3" in note for note in notes))

        # Once the page has moved, the next check --fix stamps it.
        compose = maintenance_migration.migrate_compose(compose)
        caddy = maintenance_migration.migrate_caddyfile((self.root / ".proxy" / "Caddyfile").read_text())
        (self.root / "compose.yml").write_text(compose, encoding="utf-8")
        (self.root / ".proxy" / "Caddyfile").write_text(caddy, encoding="utf-8")
        self.assertTrue(wm.pending(str(self.root), ["compose.yml"])["stamp"])
        _apply(wm._plan(self.root, ["compose.yml"])[1])
        self.assertEqual(read_stamp((self.root / "compose.yml").read_text(encoding="utf-8")), 5)


def _compose_5_3():
    if not shutil.which("docker"):
        return False
    probe = subprocess.run(["docker", "compose", "version", "--short"], capture_output=True, text=True)
    if probe.returncode != 0:
        return False
    try:
        version = tuple(int(part) for part in probe.stdout.strip().lstrip("vV").split("-")[0].split(".")[:3])
    except ValueError:
        return False
    return version + (0,) * (3 - len(version)) >= (5, 3, 0)


class ComposeValidationTests(unittest.TestCase):
    """`docker compose config` itself accepts the migrated files (read-only)."""

    def setUp(self):
        if not _compose_5_3():
            self.skipTest("docker compose 5.3.0+ is required to validate pre_start")

    def test_the_migrated_schema_4_stack_validates_with_its_dev_override(self):
        root = _copy(SCHEMA_4, self)
        outcome = wm.migrate(str(root), ["compose.yml", "compose.dev.yml"], environment=dict(os.environ))
        self.assertTrue(outcome["applied"])
        result = subprocess.run(
            ["docker", "compose", "-f", "compose.yml", "-f", "compose.dev.yml", "config", "--format", "json"],
            cwd=str(root), capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        services = json.loads(result.stdout)["services"]
        self.assertEqual(len(services["worker"]["pre_start"]), 2)
        self.assertNotIn("pre_start", services["celery"])


def _schema_5_scaffold():
    """DjangoLux's project scaffold at stack schema 5, when a checkout is alongside."""
    for template in sorted(DLUX_CHECKOUTS.glob("*/dlux/scaffold/templates/project/compose.yml.tmpl")):
        if "dlux_worker" in template.read_text(encoding="utf-8"):
            return template.parent
    return None


def _render(text, schema):
    for key, value in {
        "project_slug": "demo", "project_image": "registry.example/demo", "project_image_tag": "latest",
        "config_package": "config", "stack_schema": str(schema), "project_title": "Demo",
    }.items():
        text = text.replace("{{ " + key + " }}", value)
    return text


def _service_text(contents, service):
    start, end = agent_installer._service_block_span(contents, service)
    _header, entries, _trailer = wm._split_block(contents[start:end])
    return contents[start:start + len(_header)] + "".join(text for _, text in entries)


class DjangoLuxScaffoldTests(unittest.TestCase):
    """A migrated worker is the one DjangoLux's schema 5 scaffold generates."""

    def setUp(self):
        self.scaffold = _schema_5_scaffold()
        if self.scaffold is None:
            self.skipTest("no DjangoLux checkout with the stack schema 5 scaffold alongside")

    def test_the_worker_block_matches_the_scaffold(self):
        expected = _render((self.scaffold / "compose.yml.tmpl").read_text(encoding="utf-8"), 5)
        self.assertEqual(_service_text(wm.migrate_compose(SCHEMA_4_COMPOSE), "worker"), _service_text(expected, "worker"))

    def test_the_dev_override_matches_the_scaffold(self):
        expected = _render((self.scaffold / "compose.dev.yml.tmpl").read_text(encoding="utf-8"), 5)
        migrated = wm.migrate_dev_override((SCHEMA_4 / "compose.dev.yml").read_text(encoding="utf-8"))
        self.assertEqual(_service_text(migrated, "worker"), _service_text(expected, "worker"))


def _fix_args(dev=False):
    return SimpleNamespace(file=None, dev=dev, fix=True, yes=True, deep=False, json=False, beta=False, stable=False)


class CheckFixTests(unittest.TestCase):
    """When `check --fix` adds worker, and when it leaves a stack alone."""

    SERVICES = ["web", "celery", "caddy", "composer-agent", "composer-executor", "docker-socket-proxy"]

    def setUp(self):
        self.cwd = os.getcwd()
        self.root = _copy(SCHEMA_4, self)
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self.cwd)

    def _fix(self, running, *, dev=False, services=None, init=(None, "5.6.1")):
        launcher = DockerComposeLauncher()
        launcher.services = list(services or self.SERVICES)
        launcher.dev_mode = dev
        launcher.active_compose_files = ["compose.yml"] + (["compose.dev.yml"] if dev else [])
        compose = _FakeCompose()
        with (
            patch.object(launcher, "_running_dlux_version", return_value=running),
            patch.object(launcher, "_dlux_runtime_version", return_value=init[0]),
            patch.object(launcher, "run_command", return_value=(True, init[1], "")),
            patch.object(launcher, "build_compose_env", return_value={}),
            patch("composer.worker_migration.subprocess.run", compose),
            patch("composer.worker_migration.shutil.which", return_value="/usr/bin/docker"),
            patch("composer.wrappers.inspect_wrappers", return_value=[]),
            patch("composer.checkup.inspect_legacy_proxy_routes", return_value={"recognized": [], "unsupported": []}),
            patch("composer.agent_installer.enable_post_start_label", return_value={"files": []}),
            patch("composer.agent_installer.migrate_dlux_updater", return_value={"files": []}),
            patch("composer.agent_installer.enable_executor", return_value={"files": []}),
            patch("composer.agent_installer.normalize_restart_labels", return_value={"files": []}),
            patch("composer.agent_installer.migrate_dlux_init_containers", return_value={"files": []}),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            fixes = launcher._maybe_fix(_fix_args(dev), [])
        return fixes, launcher

    def _services(self):
        return _load(Path("compose.yml").read_text(encoding="utf-8"))["services"]

    def test_a_stack_on_dlux_1_16_gets_its_worker(self):
        fixes, _launcher = self._fix((1, 16, 0))
        added = [entry for entry in fixes if entry["name"] == "fix:task-worker"]
        self.assertEqual(added[0]["level"], OK, added)
        self.assertIn("Added the worker service", added[0]["message"])
        self.assertIn("worker", self._services())
        self.assertEqual(read_stamp(Path("compose.yml").read_text(encoding="utf-8")), 5)

    def test_a_stack_on_dlux_1_15_is_left_alone_quietly(self):
        """worker would crash-loop: 1.15 has no dlux_worker."""
        fixes, _launcher = self._fix((1, 15, 5))
        self.assertNotIn("worker", self._services())
        self.assertFalse([entry for entry in fixes if "task-worker" in entry["name"]])

    def test_an_unreadable_version_is_left_alone(self):
        self._fix(None)
        self.assertNotIn("worker", self._services())

    def test_development_follows_the_required_dlux(self):
        """`-d` builds from the source, so the requirement decides."""
        Path("requirements.txt").write_text("django-lux[ocr]==1.16.0b1\n", encoding="utf-8")
        self._fix((1, 15, 5), dev=True)
        self.assertIn("worker", self._services())
        self.assertIn("worker", _load(Path("compose.dev.yml").read_text(encoding="utf-8"))["services"])

    def test_a_deployment_does_not_trust_the_requirement(self):
        Path("requirements.txt").write_text("django-lux==1.16.0\n", encoding="utf-8")
        self._fix((1, 15, 5))
        self.assertNotIn("worker", self._services())

    def test_dlux_updater_must_go_first(self):
        fixes, _launcher = self._fix((1, 16, 0), services=self.SERVICES + ["dlux-updater"], init=((1, 7, 0), "5.6.1"))
        blocked = [entry for entry in fixes if entry["name"] == "task-worker"]
        self.assertEqual(blocked[0]["level"], WARN)
        self.assertIn("dlux-updater", blocked[0]["message"])
        self.assertNotIn("worker", self._services())


class TaskWorkerCheckTests(unittest.TestCase):
    def _check(self, services, running):
        launcher = DockerComposeLauncher()
        launcher.services = list(services)
        launcher.dev_mode = False
        with patch.object(launcher, "_running_dlux_version", return_value=running):
            return launcher._check_task_worker()

    def test_a_schema_5_stack_is_fine(self):
        self.assertEqual(self._check(["web", "worker"], (1, 16, 0))["level"], OK)

    def test_dlux_1_16_without_worker_warns(self):
        result = self._check(["web", "celery"], (1, 16, 0))
        self.assertEqual(result["level"], WARN)
        self.assertIn("check --fix", result["fix"])

    def test_dlux_1_15_on_celery_is_fine(self):
        self.assertEqual(self._check(["web", "celery"], (1, 15, 5))["level"], OK)

    def test_a_stack_without_a_task_service_is_not_reported(self):
        self.assertIsNone(self._check(["web"], (1, 16, 0)))


if __name__ == "__main__":
    unittest.main()
