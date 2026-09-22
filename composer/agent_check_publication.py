"""Publish a manual image check through the deployment's runtime mount."""

import json
import shlex
import sys
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path


# Use only stdlib so the resident Composer need not be upgraded with the CLI.
# A unique temporary file also avoids racing the resident agent's own writer.
_PUBLISH_SCRIPT = """import os, sys, tempfile
from pathlib import Path
target = Path(sys.argv[1])
with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
        dir=target.parent, prefix='.' + target.name + '.', delete=False) as stream:
    stream.write(sys.argv[2] + '\\n')
    os.fchmod(stream.fileno(), 0o644)
os.replace(stream.name, target)
"""


@dataclass
class DeploymentPublication:
    launcher: object
    service: str
    path: str
    images: list

    def publish(self, payload):
        ok, _out, err = self.launcher.run_docker_compose(
            ["exec", "-T", self.service, "python", "-c", _PUBLISH_SCRIPT,
             self.path, json.dumps(payload)],
            timeout=30,
        )
        if not ok:
            raise RuntimeError(
                f"Could not publish image availability through {self.service}: "
                f"{err.strip() or 'service unavailable or runtime file not writable'}"
            )


def _option_values(command, option):
    values = []
    for index, word in enumerate(command):
        if word == option and index + 1 < len(command):
            values.append(command[index + 1])
        elif word.startswith(option + "="):
            values.append(word.split("=", 1)[1])
    return values


def deployment_publication(args):
    """Find the resident publisher and its watched images in resolved Compose."""
    if args.image or args.availability_file or args.no_publish:
        return None
    candidates = [args.file] if args.file else ["compose.yml", "docker-compose.yml"]
    if not any(Path(path).is_file() for path in candidates):
        if args.file:
            raise RuntimeError(f"Compose file does not exist: {args.file}")
        return None

    from .launcher import DockerComposeLauncher

    with redirect_stdout(sys.stderr):
        launcher = DockerComposeLauncher()
        launcher.compose_file = args.file
        launcher.dev_mode = args.dev
        launcher.resolve_active_compose_files()
        launcher.resolve_secrets()
        config = launcher.compose_config_json()
    if config is None:
        raise RuntimeError("Could not resolve Compose configuration for image availability.")
    services = config.get("services") or {}
    for service in ("composer-agent", "composer-updater"):
        spec = services.get(service) or {}
        command = spec.get("command") or []
        if isinstance(command, str):
            command = shlex.split(command)
        images = list(dict.fromkeys(_option_values(command, "--check-image")))
        paths = _option_values(command, "--availability-file")
        if not paths and "run" in command:
            triggers = _option_values(command, "--trigger-file")
            if triggers:
                paths = [str(Path(triggers[-1]).with_name("image-available.json"))]
        if images and paths:
            return DeploymentPublication(launcher, service, paths[-1], images)
    return None
