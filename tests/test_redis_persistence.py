"""Redis survives a hard stop: RDB snapshots, not an append-only file (stack recovery C).

On the CRM sales deployment a hard stop left the incremental append-only file
damaged mid-file ("Bad file format reading the append only file"); Redis refused
to start and restarted for good. A snapshot is renamed into place only when
complete, so a crash leaves the previous good one.
"""

import io
import json
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

from composer.checkup import FAIL, OK, WARN
from composer.launcher import DockerComposeLauncher

OLD = ["redis-server", "--appendonly", "yes", "--appendfsync", "everysec", "--maxmemory", "500mb",
       "--maxmemory-policy", "allkeys-lru"]
NEW = ["redis-server", "--appendonly", "no", "--save", "60", "1", "--maxmemory", "500mb",
       "--maxmemory-policy", "allkeys-lru"]
OLD_LINE = '    command: ["redis-server", "--appendonly", "yes", "--appendfsync", "everysec", "--maxmemory", "500mb", "--maxmemory-policy", "allkeys-lru"]\n'
NEW_LINE = '    command: ["redis-server", "--appendonly", "no", "--save", "60", "1", "--maxmemory", "500mb", "--maxmemory-policy", "allkeys-lru"]\n'

COMPOSE = """name: demo

services:
  web:
    image: app:1

  redis:
    image: redis:7
    restart: always
    labels:
      org.dlux.restart: "protected"
    # cache and sessions
{command}    volumes:
      - redis_data:/data
    networks:
      - internal

volumes:
  redis_data:

networks:
  internal:
"""


def _compose(command=OLD_LINE):
    return COMPOSE.format(command=command)


def _args(**over):
    base = dict(file=None, dev=False, fix=True, yes=True, deep=False, deep_service="web",
                deep_command="python manage.py dlux_doctor", json=False, beta=False, stable=False,
                no_recreate=False, resident_apply=False)
    base.update(over)
    return SimpleNamespace(**base)


class ClassifyTests(unittest.TestCase):
    def test_states(self):
        from composer.redis_persistence import AOF, CUSTOM, DEFAULT, RDB, classify

        self.assertEqual(classify(OLD), AOF)
        self.assertEqual(classify(NEW), RDB)
        self.assertEqual(classify(None), DEFAULT)
        self.assertEqual(classify(["redis-server", "--appendonly", "no"]), RDB)
        self.assertEqual(classify(["redis-server", "--maxmemory", "1gb"]), RDB)
        self.assertEqual(classify(["redis-server", "/usr/local/etc/redis/redis.conf"]), CUSTOM)
        self.assertEqual(classify(["redis-server", "/etc/redis.conf", "--appendonly", "yes"]), AOF)
        self.assertEqual(classify(["sh", "-c", "redis-server --appendonly yes"]), CUSTOM)
        self.assertEqual(classify("redis-server --appendonly yes"), AOF)

    def test_rdb_tokens_turn_aof_off_and_keep_every_other_flag(self):
        from composer.redis_persistence import rdb_tokens

        self.assertEqual(rdb_tokens(OLD), NEW)
        self.assertEqual(
            rdb_tokens(["redis-server", "--appendonly", "yes", "--save", "900", "1", "--maxclients", "50"]),
            ["redis-server", "--appendonly", "no", "--save", "900", "1", "--maxclients", "50"],
        )
        self.assertEqual(
            rdb_tokens(["redis-server", "--maxclients", "50", "--appendonly", "yes"]),
            ["redis-server", "--maxclients", "50", "--appendonly", "no", "--save", "60", "1"],
        )


class RewriteTests(unittest.TestCase):
    def _plan(self, text):
        from composer.redis_persistence import plan

        with TemporaryDirectory() as root:
            Path(root, "compose.yml").write_text(text, encoding="utf-8")
            _sources, updates, notes = plan(Path(root), ["compose.yml"])
            return (next(iter(updates.values())) if updates else None), notes

    def test_the_old_scaffold_line_becomes_the_new_scaffold_line(self):
        updated, _notes = self._plan(_compose())

        self.assertEqual(updated, _compose(NEW_LINE))
        self.assertIn("    # cache and sessions\n", updated)

    def test_a_string_command_stays_a_string(self):
        updated, _notes = self._plan(_compose("    command: redis-server --appendonly yes --maxmemory 500mb\n"))

        self.assertIn("    command: redis-server --appendonly no --save 60 1 --maxmemory 500mb\n", updated)

    def test_a_block_list_stays_a_block_list_and_quotes_what_yaml_would_misread(self):
        block = '    command:\n      - redis-server\n      - --appendonly\n      - yes\n      - --maxmemory\n      - 500mb\n'
        updated, _notes = self._plan(_compose(block))

        command = yaml.load(updated, Loader=yaml.BaseLoader)["services"]["redis"]["command"]
        self.assertEqual(command, ["redis-server", "--appendonly", "no", "--save", "60", "1", "--maxmemory", "500mb"])
        self.assertEqual(yaml.safe_load(updated)["services"]["redis"]["command"][2], "no")
        self.assertIn("      - --appendonly\n", updated)

    def test_a_stack_on_snapshots_is_left_alone(self):
        updated, notes = self._plan(_compose(NEW_LINE))

        self.assertIsNone(updated)
        self.assertEqual(notes, [])

    def test_a_stack_without_redis_is_left_alone(self):
        updated, _notes = self._plan("name: demo\n\nservices:\n  web:\n    image: app:1\n")

        self.assertIsNone(updated)


class MigrateTests(unittest.TestCase):
    def setUp(self):
        holder = TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)

    def _runner(self, command):
        def run(argv, **kwargs):
            if "config" in argv:
                model = {"name": "demo", "services": {"redis": {"image": "redis:7", "command": command}}}
                return Mock(returncode=0, stdout=json.dumps(model), stderr="")
            return Mock(returncode=0, stdout="", stderr="")
        return run

    def test_it_validates_archives_and_writes_keeping_crlf(self):
        from composer.redis_persistence import migrate

        original = _compose().replace("\n", "\r\n")
        (self.root / "compose.yml").write_bytes(original.encode("utf-8"))
        with patch("composer.redis_persistence.shutil.which", return_value="/usr/bin/docker"):
            outcome = migrate(str(self.root), ["compose.yml"], command_runner=self._runner(NEW))

        self.assertTrue(outcome["applied"])
        self.assertEqual(outcome["files"], ["compose.yml"])
        written = (self.root / "compose.yml").read_bytes()
        self.assertIn(NEW_LINE.replace("\n", "\r\n").encode("utf-8"), written)
        self.assertNotIn(b"\n", written.replace(b"\r\n", b""))
        backup = Path(outcome["backup_root"]) / "redis-persistence" / "compose.yml"
        self.assertEqual(backup.read_bytes(), original.encode("utf-8"))

    def test_another_file_that_keeps_aof_on_refuses_and_writes_nothing(self):
        from composer.redis_persistence import RedisPersistenceError, migrate

        (self.root / "compose.yml").write_text(_compose(), encoding="utf-8")
        with patch("composer.redis_persistence.shutil.which", return_value="/usr/bin/docker"), \
                self.assertRaises(RedisPersistenceError):
            migrate(str(self.root), ["compose.yml"], command_runner=self._runner(OLD))
        self.assertEqual((self.root / "compose.yml").read_text(encoding="utf-8"), _compose())


class MoveAsideScriptTests(unittest.TestCase):
    """The one-off container's script, run here with sh: it moves, it never deletes."""

    def test_the_append_only_files_move_into_the_archive_folder(self):
        from composer.redis_persistence import move_aside_argv

        argv = move_aside_argv("demo_redis_data", "redis:7", "20261125T210000Z", "/data", NEW)
        self.assertEqual(argv[:9], ["docker", "run", "--rm", "--network", "none", "--pull", "never",
                                    "--entrypoint", "sh"])
        self.assertIn("demo_redis_data:/data", argv)
        script_at = argv.index("-c") + 1
        with TemporaryDirectory() as data:
            (Path(data) / "appendonlydir").mkdir()
            (Path(data) / "appendonlydir" / "appendonly.aof.5.incr.aof").write_text("broken", encoding="utf-8")
            (Path(data) / "dump.rdb").write_text("snapshot", encoding="utf-8")
            args = [data if value == "/data" else value for value in argv[script_at + 1:]]
            done = subprocess.run(["sh", "-c", argv[script_at], *args], capture_output=True, text=True, check=True)

            archived = Path(data) / "aof-archive" / "20261125T210000Z" / "appendonlydir" / "appendonly.aof.5.incr.aof"
            self.assertEqual(archived.read_text(encoding="utf-8"), "broken")
            self.assertFalse((Path(data) / "appendonlydir").exists())
            self.assertEqual((Path(data) / "dump.rdb").read_text(encoding="utf-8"), "snapshot")
            self.assertIn("moved: appendonlydir", done.stdout)

    def test_unsafe_names_are_refused(self):
        from composer.redis_persistence import RedisPersistenceError, move_aside_argv

        with self.assertRaises(RedisPersistenceError):
            move_aside_argv("v", "redis:7", "s", "/data", ["redis-server", "--appenddirname", "../etc"])


class _Launcher(unittest.TestCase):
    def _launcher(self, model, rows=(), logs=""):
        launcher = DockerComposeLauncher()
        launcher.services = sorted(model["services"])
        launcher.active_compose_files = ["compose.yml"]
        patch.object(launcher, "compose_config_json", return_value=model).start()
        patch.object(launcher, "_container_states", return_value=list(rows)).start()
        self.logs = Mock(return_value=(True, logs, ""))
        patch.object(launcher, "run_docker_compose", self.logs).start()
        self.addCleanup(patch.stopall)
        return launcher


def _redis_model(command):
    return {"name": "demo", "services": {"redis": {"image": "redis:7", "command": command,
                                                    "volumes": [{"type": "volume", "source": "redis_data", "target": "/data"}]},
                                          "web": {"image": "app:1"}},
            "volumes": {"redis_data": {"name": "demo_redis_data"}}}


def _row(state="running", restarts=0):
    return {"service": "redis", "name": "demo-redis-1", "id": "r1", "state": state, "health": "",
            "restarts": restarts, "started_at": ""}


class PersistenceFindingTests(_Launcher):
    def test_aof_warns_and_says_what_the_fix_does(self):
        finding = self._launcher(_redis_model(OLD))._check_redis_persistence()

        self.assertEqual((finding["level"], finding["name"]), (WARN, "redis-persistence"))
        self.assertIn("append-only file", finding["message"])
        self.assertIn("check --fix", finding["fix"])

    def test_snapshots_are_ok(self):
        finding = self._launcher(_redis_model(NEW))._check_redis_persistence()

        self.assertEqual((finding["level"], finding["name"]), (OK, "redis-persistence"))

    def test_no_redis_no_finding(self):
        launcher = self._launcher({"name": "demo", "services": {"web": {"image": "app:1"}}})

        self.assertIsNone(launcher._check_redis_persistence())


class CorruptFindingTests(_Launcher):
    BAD = ("redis-1  | * DB loaded from base file appendonly.aof.5.base.rdb: 0.001 seconds\n"
           "redis-1  | # Bad file format reading the append only file appendonly.aof.5.incr.aof: make a backup "
           "of your AOF file, then use ./redis-check-aof --fix <filename.manifest>\n")

    def test_a_crash_loop_on_a_damaged_file_fails(self):
        finding = self._launcher(_redis_model(OLD), [_row("restarting", 7)], self.BAD)._check_redis_aof()

        self.assertEqual((finding["level"], finding["name"]), (FAIL, "redis-aof-corrupt"))
        self.assertIn("append-only file", finding["message"])
        self.assertIn("check --fix", finding["fix"])
        self.assertIn("sign in", finding["fix"])

    def test_a_crash_loop_for_another_reason_is_not_this_finding(self):
        launcher = self._launcher(_redis_model(OLD), [_row("restarting", 7)], "redis-1 | # Can't open the log file")

        self.assertIsNone(launcher._check_redis_aof())

    def test_a_healthy_redis_is_not_asked_for_its_logs(self):
        launcher = self._launcher(_redis_model(OLD), [_row("running", 0)], self.BAD)

        self.assertIsNone(launcher._check_redis_aof())
        self.logs.assert_not_called()
        self.assertEqual(launcher._check_redis_aof(report_ok=True)["level"], OK)


class RecoveryFixTests(_Launcher):
    def test_check_fix_moves_the_files_aside_switches_to_snapshots_and_recreates_redis(self):
        launcher = self._launcher(_redis_model(OLD), [_row("restarting", 7)])
        launcher._fix_force_recreate = set()
        commands = []

        def run_command(argv, timeout=None, env=None):
            commands.append(list(argv))
            return True, "moved: appendonlydir\n", ""

        results = [{"level": FAIL, "name": "redis-aof-corrupt", "message": "x"}]
        with (
            patch.object(launcher, "run_command", side_effect=run_command),
            patch.object(launcher, "resolve_secrets", return_value=(True, "")),
            patch("composer.redis_persistence.preview", return_value={"files": ["compose.yml"], "diff": "", "note": ""}),
            patch("composer.redis_persistence.migrate", return_value={"applied": True, "files": ["compose.yml"], "backup_root": "/x/b"}) as migrate,
            patch("composer.agent_installer.enable_post_start_label", return_value={"files": []}),
            patch("composer.agent_installer.migrate_dlux_updater", return_value={"files": []}),
            patch("composer.agent_installer.normalize_restart_labels", return_value={"files": []}),
            patch("composer.agent_installer.install_composer_stack", return_value={"files": []}),
            patch("composer.checkup.inspect_legacy_proxy_routes", return_value={"recognized": [], "unsupported": []}),
            patch("composer.wrappers.inspect_wrappers", return_value=[]),
            patch("composer.checkup.confirm", return_value=True),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            fixes = launcher._maybe_fix(_args(), results)

        self.assertIn(["stop", "redis"], [call.args[0] for call in self.logs.call_args_list])
        move = [argv for argv in commands if argv[:2] == ["docker", "run"]]
        self.assertEqual(len(move), 1, commands)
        self.assertIn("--network", move[0])
        self.assertIn("demo_redis_data:/data", move[0])
        recovered = [f for f in fixes if f["name"] == "fix:redis-aof-corrupt"][0]
        self.assertEqual(recovered["level"], OK, fixes)
        self.assertIn("aof-archive/", recovered["message"])
        migrate.assert_called_once()
        self.assertIn("redis", launcher._fix_force_recreate)
        self.assertTrue(any(f["name"] == "fix:redis-persistence" and f["level"] == OK for f in fixes), fixes)


class PreviewTests(unittest.TestCase):
    def test_the_preview_shows_the_rewrite_and_the_recovery(self):
        from composer.ops import _redis_repairs

        with TemporaryDirectory() as root:
            previous = os.getcwd()
            os.chdir(root)
            self.addCleanup(os.chdir, previous)
            Path("compose.yml").write_text(_compose(), encoding="utf-8")
            launcher = SimpleNamespace(_maintenance_compose_files=lambda: ["compose.yml"])
            entries = _redis_repairs(launcher, [{"level": "fail", "name": "redis-aof-corrupt", "message": ""}])

        self.assertEqual([e["name"] for e in entries], ["redis-aof-corrupt", "redis-persistence"])
        self.assertEqual(entries[1]["files"], ["compose.yml"])
        self.assertIn('+    command: ["redis-server", "--appendonly", "no", "--save", "60", "1"', entries[1]["diff"])
        self.assertIn("aof-archive", entries[0]["note"])


if __name__ == "__main__":
    unittest.main()
