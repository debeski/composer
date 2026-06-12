import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

from .cli import parse_args
from .config import ConfigMixin
from .constants import ERROR, IDLE, OK, RUNNING
from .docker_compose_manager import DockerComposeMixin
from .health_monitor import HealthMonitorMixin
from .post_start_hooks import PostStartHooksMixin
from .rendering import RenderingMixin
from .secrets_manager import SecretsMixin
from .version import read_composer_version


class DockerComposeLauncher(
    PostStartHooksMixin,
    HealthMonitorMixin,
    ConfigMixin,
    SecretsMixin,
    DockerComposeMixin,
    RenderingMixin,
):
    def __init__(self):
        self.app_url = "http://localhost"
        self.composer_version = read_composer_version()
        self.enc_file = "./secrets.enc"
        self.loaded_secrets: List[str] = []
        self.debug_mode = False
        self.no_migrate = False
        self.force_makemigrations = False
        self.skip_decrypt = False
        self.compose_file = None
        self.active_compose_files: List[str] = []
        self.dev_mode = False
        self.target_app = None
        self.update_images = False
        self.pull_service = None
        self.down_mode = False
        self.down_volumes = False
        self.last_progress_text = ""
        self.last_progress_label = ""
        self.last_runtime_diagnostic = ""
        self.last_render_line_count = 0
        self.compose_runtime_override: Optional[Path] = None
        self.build_images = False

        self.sections = {
            "secrets": IDLE,
            "pull": IDLE,
            "compose": IDLE,
            "health": IDLE,
            "post_start": IDLE,
        }

        self.services: List[str] = []
        self.service_state: Dict[str, str] = {}

    def cleanup(self):
        for k in self.loaded_secrets:
            os.environ.pop(k, None)
        self.remove_runtime_compose_override()

    def handle_interrupt(self):
        if self.last_render_line_count or self.last_progress_text:
            print("\r\033[2K", end="")
        print("\nInterrupted by user. Exiting cleanly.", flush=True)

    def run(self):
        try:
            args = parse_args()

            if args.version:
                print(f"composer {self.composer_version}")
                return
            self.no_migrate = args.no_migrate
            self.force_makemigrations = args.make_migrations
            self.dev_mode = args.dev
            self.skip_decrypt = args.skip_decrypt or self.dev_mode
            self.compose_file = args.file

            if self.compose_file:
                self.active_compose_files = [self.compose_file]
            else:
                base_file = "compose.yml"
                if not Path(base_file).exists() and Path("docker-compose.yml").exists():
                    base_file = "docker-compose.yml"

                self.active_compose_files = [base_file]
                if self.dev_mode:
                    self.active_compose_files.append("compose.dev.yml")

            self.target_app = args.app
            self.build_images = args.build
            if args.update:
                self.update_images = True
                if isinstance(args.update, str):
                    self.pull_service = args.update
            self.down_mode = args.down
            self.down_volumes = args.volumes

            self.extract_config()

            self.discover_services(silent=True)

            if args.encrypt:
                public_key = (
                    args.key
                    or args.key_positional
                    or os.environ.get("SOPS_AGE_PUBLIC_KEY")
                    or input("Paste AGE public key: ").strip()
                )
                if public_key.startswith("AGE-SECRET-KEY-"):
                    print(
                        "✖ Expected a public key (age1...) but got a private key (AGE-SECRET-KEY-...)",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                in_path = args.input or ".secrets/.env"
                out_path = args.output or self.enc_file
                ok, out = self.encrypt_secrets_raw(
                    public_key=public_key,
                    input_file=args.input,
                    output_file=args.output,
                )
                if not ok:
                    print(f"✖ Encryption failed: {out}", file=sys.stderr)
                    sys.exit(1)
                print(f"✅ Encrypted {in_path} → {out_path}")
                return

            if args.decrypt:
                key = (
                    args.key
                    or args.key_positional
                    or os.environ.get("SOPS_AGE_KEY")
                    or input("Paste AGE key: ").strip()
                )
                in_path = args.input or self.enc_file
                out_path = args.output
                ok, out = self.decrypt_secrets_raw(
                    key=key,
                    input_file=args.input,
                    output_file=args.output,
                )
                if not ok:
                    print(f"✖ Decryption failed: {out}", file=sys.stderr)
                    if "no identity matched" in out:
                        print(
                            "   Hint: verify the private key matches the public key used for encryption.",
                            file=sys.stderr,
                        )
                        print(
                            "   Run: age-keygen -y .secrets/.key  (compare output with the recipient in the error above)",
                            file=sys.stderr,
                        )
                    sys.exit(1)
                if out_path:
                    print(f"✅ Decrypted {in_path} → {out_path}")
                else:
                    print(out)
                return

            if self.down_mode:
                print("🛑 Stopping and removing containers...")
                if self.down_volumes:
                    print("   (Volumes will be removed)")
                ok, err = self.down_containers()
                if not ok:
                    print(f"✖ Failed to stop containers:\n  {err.strip()}")
                    sys.exit(1)
                print("✅ Containers stopped")
                return

            if self.services:
                self.update_service_states()
            self.render()

            self.sections["secrets"] = RUNNING
            self.render()

            if self.skip_decrypt:
                if not self.load_secrets_from_file():
                    self.sections["secrets"] = ERROR
                    self.render("Failed to load secrets from file")
                    sys.exit(1)
            else:
                key = (
                    args.key
                    or args.key_positional
                    or os.environ.get("SOPS_AGE_KEY")
                    or input("Paste AGE key: ").strip()
                )
                ok, out = self.decrypt_secrets_raw(key=key)

                if not ok:
                    self.sections["secrets"] = ERROR
                    self.render("Failed to decrypt secrets")
                    sys.exit(1)

                for line in out.splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        os.environ[k] = v.strip("'\"")
                        self.loaded_secrets.append(k)
            self.sections["secrets"] = OK

            if self.update_images:
                self.sections["pull"] = RUNNING
                self.render()
                ok, out, err = self.pull_images()
                if not ok:
                    self.sections["pull"] = ERROR
                    detail = self.build_failure_detail(out, err)
                    self.render(f"Failed to pull images\n\n{detail}")
                    sys.exit(1)
                self.sections["pull"] = OK

            self.sections["compose"] = RUNNING
            self.render()
            if not self.discover_services():
                self.sections["compose"] = ERROR
                self.render(
                    "Failed to read compose services\n\n"
                    + (self.last_runtime_diagnostic or "Check the compose file and environment values.")
                )
                sys.exit(1)

            ok, out, err = self.launch_containers()
            if not ok:
                self.sections["compose"] = ERROR
                diagnostics = self.collect_service_diagnostics()
                detail = self.build_failure_detail(out, err, diagnostics)
                self.render(f"Failed to start containers\n\n{detail}")
                sys.exit(1)
            self.sections["compose"] = OK

            self.sections["health"] = RUNNING
            self.render()

            health_ok, health_detail = self.monitor_health()
            if not health_ok:
                self.sections["health"] = ERROR
                self.render(health_detail)
                sys.exit(1)
            self.sections["health"] = OK

            self.sections["post_start"] = RUNNING
            self.render()
            hooks_ok, hooks_detail = self.run_post_start_hooks()
            if not hooks_ok:
                self.sections["post_start"] = ERROR
                self.render(f"Failed to execute post_start commands\n\n{hooks_detail}")
            else:
                self.sections["post_start"] = OK
                self.render()

            print("\n🎉 Environment ready")

        except KeyboardInterrupt:
            self.handle_interrupt()
            raise SystemExit(130)
        finally:
            self.cleanup()
