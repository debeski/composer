import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from composer.stack_cleanup import (
    StackCleanupError,
    remove_obsolete_service_blocks,
    remove_obsolete_services,
)


COMPOSE = """name: demo

services:
  web:
    image: example/web:latest

  pgadmin:
    image: dpage/pgadmin4:latest
    volumes:
      - pgadmin_data:/var/lib/pgadmin

  db-backup:
    image: example/backup:latest
    volumes:
      - database_backups:/backups

  db_backup:
    image: example/old-backup:latest

volumes:
  pgadmin_data:
  database_backups:
"""


ORIGINAL_MODEL = json.dumps(
    {
        "services": {
            "web": {},
            "pgadmin": {},
            "db-backup": {},
            "db_backup": {},
        },
        "volumes": {
            "pgadmin_data": {"name": "demo_pgadmin_data"},
            "database_backups": {"name": "demo_database_backups"},
        },
    }
)
CLEAN_MODEL = json.dumps(
    {
        "services": {"web": {}},
        "volumes": {
            "pgadmin_data": {"name": "demo_pgadmin_data"},
            "database_backups": {"name": "demo_database_backups"},
        },
    }
)
EXISTING_VOLUMES = "demo_pgadmin_data\ndemo_database_backups\n"
PROXY_CADDY = """example.test {
    route {
        handle_path /pgadmin4/* {
            reverse_proxy pgadmin:80 {
                header_up X-Script-Name /pgadmin4
            }
        }
        reverse_proxy web:8000
    }
}
"""
PROXY_NGINX = """server {
    location /pgadmin4/ {
        proxy_pass http://pgadmin:80/;
        proxy_set_header X-Script-Name /pgadmin4;
    }
    location / {
        proxy_pass http://web:8000;
    }
}
"""


def _completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _successful_lifecycle():
    return [
        _completed(),
        _completed(stdout=ORIGINAL_MODEL),
        _completed(stdout=EXISTING_VOLUMES),
        _completed(stdout=CLEAN_MODEL),
        _completed(),
        _completed(stdout=CLEAN_MODEL),
        _completed(stdout=EXISTING_VOLUMES),
    ]


class StackCleanupTransformTests(unittest.TestCase):
    def test_removes_all_obsolete_spellings_but_keeps_data_volumes(self):
        updated, removed = remove_obsolete_service_blocks(COMPOSE)

        self.assertEqual(removed, {"pgadmin", "db-backup", "db_backup"})
        self.assertIn("  web:", updated)
        self.assertNotIn("  pgadmin:", updated)
        self.assertNotIn("  db-backup:", updated)
        self.assertNotIn("  db_backup:", updated)
        self.assertIn("  pgadmin_data:", updated)
        self.assertIn("  database_backups:", updated)

    def test_inline_service_and_following_comment_are_handled(self):
        contents = """services:
  pgadmin: {image: dpage/pgadmin4:latest}
  # Main application
  web:
    image: example/web:latest
"""
        updated, removed = remove_obsolete_service_blocks(contents)

        self.assertEqual(removed, {"pgadmin"})
        self.assertIn("  # Main application", updated)
        self.assertIn("  web:", updated)

    def test_clean_compose_is_unchanged(self):
        contents = "services:\n  web:\n    image: example/web:latest\n"
        self.assertEqual(remove_obsolete_service_blocks(contents), (contents, set()))


class StackCleanupApplyTests(unittest.TestCase):
    def test_validates_backs_up_and_applies_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compose = root / "compose.yml"
            compose.write_text(COMPOSE, encoding="utf-8")
            runner = Mock(side_effect=_successful_lifecycle())

            with patch("composer.stack_cleanup.shutil.which", return_value="/usr/bin/docker"):
                result = remove_obsolete_services(
                    str(root),
                    ["compose.yml"],
                    command_runner=runner,
                )

            self.assertTrue(result["applied"])
            self.assertEqual(
                result["removed_services"],
                ["db-backup", "db_backup", "pgadmin"],
            )
            self.assertNotIn("  pgadmin:", compose.read_text(encoding="utf-8"))
            backup = Path(result["backup_root"]) / "original" / "compose.yml"
            self.assertEqual(backup.read_text(encoding="utf-8"), COMPOSE)
            self.assertTrue(result["container_cleanup_applied"])
            self.assertTrue(result["postflight_verified"])
            self.assertEqual(
                result["preserved_volumes"],
                ["demo_database_backups", "demo_pgadmin_data"],
            )
            validation = runner.call_args_list[3].args[0]
            self.assertEqual(validation[-3:], ["config", "--format", "json"])
            self.assertIn("--project-directory", validation)
            cleanup = runner.call_args_list[4].args[0]
            self.assertEqual(
                cleanup[-6:],
                ["rm", "-s", "-f", "db-backup", "db_backup", "pgadmin"],
            )
            self.assertNotIn("up", cleanup)

    def test_validation_failure_keeps_source_and_archives_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compose = root / "compose.yml"
            compose.write_text(COMPOSE, encoding="utf-8")
            runner = Mock(
                side_effect=[
                    _completed(),
                    _completed(stdout=ORIGINAL_MODEL),
                    _completed(stdout=EXISTING_VOLUMES),
                    _completed(1, stderr="invalid"),
                ]
            )

            with (
                patch("composer.stack_cleanup.shutil.which", return_value="/usr/bin/docker"),
                self.assertRaises(StackCleanupError),
            ):
                remove_obsolete_services(
                    str(root),
                    ["compose.yml"],
                    command_runner=runner,
                )

            self.assertEqual(compose.read_text(encoding="utf-8"), COMPOSE)
            rejected = list((root / ".xclude" / "composer-check").glob("*/rejected/compose.yml"))
            self.assertEqual(len(rejected), 1)
            self.assertNotIn("  pgadmin:", rejected[0].read_text(encoding="utf-8"))

    def test_multiple_active_compose_files_are_cleaned_together(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "compose.yml"
            override = root / "compose.dev.yml"
            base.write_text(
                "services:\n  web:\n    image: example/web:latest\n  pgadmin:\n    image: dpage/pgadmin4:latest\n",
                encoding="utf-8",
            )
            override.write_text(
                "services:\n  db_backup:\n    environment:\n      KEEP_DAYS: 7\n",
                encoding="utf-8",
            )
            runner = Mock(side_effect=_successful_lifecycle())

            with patch("composer.stack_cleanup.shutil.which", return_value="/usr/bin/docker"):
                result = remove_obsolete_services(
                    str(root),
                    ["compose.yml", "compose.dev.yml"],
                    command_runner=runner,
                )

            self.assertNotIn("  pgadmin:", base.read_text(encoding="utf-8"))
            self.assertNotIn("  db_backup:", override.read_text(encoding="utf-8"))
            backup = Path(result["backup_root"]) / "original"
            self.assertTrue((backup / "compose.yml").is_file())
            self.assertTrue((backup / "compose.dev.yml").is_file())
            validation = runner.call_args_list[3].args[0]
            self.assertEqual(validation.count("-f"), 2)

    def test_targeted_container_failure_keeps_original_compose(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compose = root / "compose.yml"
            compose.write_text(COMPOSE, encoding="utf-8")
            lifecycle = _successful_lifecycle()
            lifecycle[4] = _completed(1, stderr="container removal failed")
            runner = Mock(side_effect=lifecycle)

            with (
                patch("composer.stack_cleanup.shutil.which", return_value="/usr/bin/docker"),
                self.assertRaisesRegex(StackCleanupError, "container removal failed"),
            ):
                remove_obsolete_services(
                    str(root),
                    ["compose.yml"],
                    command_runner=runner,
                )

            self.assertEqual(compose.read_text(encoding="utf-8"), COMPOSE)
            originals = list(
                (root / ".xclude" / "composer-check").glob("*/original/compose.yml")
            )
            self.assertEqual(len(originals), 1)

    def test_postflight_fails_if_existing_named_volume_disappears(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compose = root / "compose.yml"
            compose.write_text(COMPOSE, encoding="utf-8")
            lifecycle = _successful_lifecycle()
            lifecycle[-1] = _completed(stdout="demo_pgadmin_data\n")
            runner = Mock(side_effect=lifecycle)

            with (
                patch("composer.stack_cleanup.shutil.which", return_value="/usr/bin/docker"),
                self.assertRaisesRegex(StackCleanupError, "demo_database_backups"),
            ):
                remove_obsolete_services(
                    str(root),
                    ["compose.yml"],
                    command_runner=runner,
                )

            self.assertNotIn("  pgadmin:", compose.read_text(encoding="utf-8"))
            originals = list(
                (root / ".xclude" / "composer-check").glob("*/original/compose.yml")
            )
            self.assertEqual(len(originals), 1)

    def test_proxy_only_caddy_route_is_validated_archived_and_reloaded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compose = root / "compose.yml"
            proxy = root / ".proxy" / "Caddyfile"
            proxy.parent.mkdir()
            compose.write_text(
                "services:\n  web:\n    image: example/web\n  caddy:\n    image: caddy:latest\n",
                encoding="utf-8",
            )
            proxy.write_text(PROXY_CADDY, encoding="utf-8")
            model = json.dumps(
                {
                    "services": {
                        "web": {},
                        "caddy": {
                            "image": "caddy:latest",
                            "volumes": [
                                {
                                    "type": "bind",
                                    "source": str(proxy.resolve()),
                                    "target": "/etc/caddy/Caddyfile",
                                }
                            ]
                        },
                    }
                }
            )
            runner = Mock(
                side_effect=[
                    _completed(),
                    _completed(stdout=model),
                    _completed(),
                    _completed(stdout=model),
                    _completed(),
                    _completed(stdout="caddy\nweb\n"),
                    _completed(stdout=model),
                    _completed(),
                    _completed(),
                ]
            )

            with patch("composer.stack_cleanup.shutil.which", return_value="/usr/bin/docker"):
                result = remove_obsolete_services(
                    str(root),
                    ["compose.yml"],
                    command_runner=runner,
                )

            self.assertEqual(result["removed_services"], [])
            self.assertEqual(result["proxy_files"], [".proxy/Caddyfile"])
            self.assertEqual(result["proxy_candidates_validated"], ["caddy"])
            self.assertEqual(result["proxy_services_reloaded"], ["caddy"])
            self.assertEqual(result["proxy_services_restarted"], [])
            self.assertNotIn("pgadmin", proxy.read_text(encoding="utf-8").lower())
            backup = Path(result["backup_root"]) / "original" / ".proxy" / "Caddyfile"
            self.assertEqual(backup.read_text(encoding="utf-8"), PROXY_CADDY)
            validation = runner.call_args_list[4].args[0]
            self.assertEqual(validation[-6:-4], ["caddy", "validate"])
            reload_command = runner.call_args_list[8].args[0]
            self.assertIn("reload", reload_command)
            self.assertFalse(
                any(
                    "rm" in call.args[0] and "-s" in call.args[0]
                    for call in runner.call_args_list
                )
            )

    def test_proxy_only_nginx_template_is_validated_and_restarted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compose = root / "compose.yml"
            proxy = root / ".proxy" / "default.conf.template"
            proxy.parent.mkdir()
            compose.write_text(
                "services:\n  web:\n    image: example/web\n  nginx:\n    image: nginx:latest\n",
                encoding="utf-8",
            )
            proxy.write_text(PROXY_NGINX, encoding="utf-8")
            model = json.dumps(
                {
                    "services": {
                        "web": {},
                        "nginx": {
                            "image": "nginx:latest",
                            "volumes": [
                                {
                                    "type": "bind",
                                    "source": str(proxy.resolve()),
                                    "target": "/etc/nginx/templates/default.conf.template",
                                }
                            ]
                        },
                    }
                }
            )
            runner = Mock(
                side_effect=[
                    _completed(),
                    _completed(stdout=model),
                    _completed(),
                    _completed(stdout=model),
                    _completed(),
                    _completed(stdout="nginx\nweb\n"),
                    _completed(stdout=model),
                    _completed(),
                    _completed(),
                    _completed(),
                ]
            )

            with patch("composer.stack_cleanup.shutil.which", return_value="/usr/bin/docker"):
                result = remove_obsolete_services(
                    str(root),
                    ["compose.yml"],
                    command_runner=runner,
                )

            self.assertEqual(result["proxy_candidates_validated"], ["nginx"])
            self.assertEqual(result["proxy_services_reloaded"], [])
            self.assertEqual(result["proxy_services_restarted"], ["nginx"])
            self.assertNotIn("pgadmin", proxy.read_text(encoding="utf-8").lower())
            validation = runner.call_args_list[4].args[0]
            self.assertEqual(validation[-3:], ["nginx:latest", "nginx", "-t"])
            # R2-060: no `compose run`, so the validation creates no network or volume.
            self.assertEqual(validation[:2], ["docker", "run"])
            self.assertEqual(validation[validation.index("--network") + 1], "none")
            restart_command = runner.call_args_list[8].args[0]
            self.assertEqual(
                restart_command[-2:],
                ["restart", "nginx"],
            )
            live_validation = runner.call_args_list[9].args[0]
            self.assertEqual(
                live_validation[-5:],
                ["exec", "-T", "nginx", "nginx", "-t"],
            )

    def test_clean_stack_is_idempotent_without_docker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "compose.yml").write_text(
                "services:\n  web:\n    image: example/web:latest\n",
                encoding="utf-8",
            )
            runner = Mock()

            result = remove_obsolete_services(
                str(root),
                ["compose.yml"],
                command_runner=runner,
            )

            self.assertFalse(result["applied"])
            self.assertEqual(result["removed_services"], [])
            runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class ProxyValidationCommandTests(unittest.TestCase):
    """R2-060: the validator is a network-free ``docker run`` of the proxy image."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "certs").mkdir()
        self.candidate = self.root / ".Caddyfile.composer-check-x.tmp"
        self.candidate.write_text(":80\n", encoding="utf-8")
        self.model = {
            "name": "shop",
            "services": {
                "web": {"image": "shop/web"},
                "caddy": {
                    "image": "caddy:2.8",
                    "environment": {"DLUX_FRONT_PROXY": "10.0.0.5", "UNSET": None},
                    "volumes": [
                        {"type": "bind", "source": str(self.root / "Caddyfile"), "target": "/etc/caddy/Caddyfile"},
                        {"type": "bind", "source": str(self.root / "certs"), "target": "/certs", "read_only": True},
                        {"type": "bind", "source": str(self.root / "missing"), "target": "/srv/missing"},
                        {"type": "volume", "source": "caddy_data", "target": "/data"},
                    ],
                },
            },
        }

    def test_no_network_no_volume_no_missing_bind_and_no_values_on_the_command_line(self):
        from composer.stack_cleanup import proxy_validation_command

        argv, keys = proxy_validation_command(self.model, "caddy", self.candidate, "/etc/caddy/Caddyfile", "caddy")
        self.assertEqual(argv[:2], ["docker", "run"])
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertIn("web:127.0.0.1", argv)
        mounts = [argv[i + 1] for i, token in enumerate(argv) if token == "--mount"]
        self.assertEqual(mounts, [f"type=bind,source={self.root / 'certs'},target=/certs,readonly"])
        self.assertIn(f"{self.candidate}:/etc/caddy/Caddyfile:ro", argv)
        self.assertNotIn("caddy_data", " ".join(argv))
        self.assertEqual(keys, ["DLUX_FRONT_PROXY"])
        self.assertNotIn("10.0.0.5", " ".join(argv))
        self.assertEqual(argv[-7:], ["caddy:2.8", "caddy", "validate", "--config", "/etc/caddy/Caddyfile",
                                     "--adapter", "caddyfile"])

    def test_a_build_only_proxy_uses_composes_image_name_without_pulling(self):
        from composer.stack_cleanup import proxy_validation_command

        self.model["services"]["caddy"].pop("image")
        self.model["services"]["caddy"]["build"] = {"context": "."}
        argv, _keys = proxy_validation_command(self.model, "caddy", self.candidate, "/etc/caddy/Caddyfile", "caddy")
        self.assertIn("shop-caddy", argv)
        self.assertEqual(argv[argv.index("--pull") + 1], "never")

    def test_a_docker_failure_is_not_reported_as_a_rejected_file(self):
        from composer.stack_cleanup import StackCleanupError, _validate_proxy_candidates

        source = self.root / "Caddyfile"
        runner = Mock(return_value=_completed(125, stderr="docker: Error response from daemon: all predefined "
                                                     "address pools have been fully subnetted"))
        with self.assertRaises(StackCleanupError) as raised:
            _validate_proxy_candidates(runner, self.root, [], {}, [("caddy", source, "/etc/caddy/Caddyfile", "caddy")],
                                       {source: self.candidate}, self.model)
        self.assertIn("Docker could not run the container", str(raised.exception))
        self.assertNotIn("rejected", str(raised.exception))
        self.assertEqual(runner.call_args.kwargs["env"]["DLUX_FRONT_PROXY"], "10.0.0.5")

        runner.return_value = _completed(1, stderr="Error: adapting config using caddyfile: unrecognized directive")
        with self.assertRaises(StackCleanupError) as raised:
            _validate_proxy_candidates(runner, self.root, [], {}, [("caddy", source, "/etc/caddy/Caddyfile", "caddy")],
                                       {source: self.candidate}, self.model)
        self.assertIn("caddy rejected the cleaned proxy configuration", str(raised.exception))
