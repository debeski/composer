"""Reaching the DjangoLux runtime volume from a composer that cannot see it.

``/opt/dlux-runtime`` is a *container* path. The runtime volume is mounted there
in `composer-executor`, `composer-agent`, `web` and `celery` — and nowhere else.
A composer started from the project root (the `./start.sh` wrapper, or a native
`python -m composer`) has no such mount, so `dlux-update` failed with "No
DjangoLux runtime volume" on every correctly deployed stack.

Handing the work to a stack service does not fix it: `composer-executor` holds
the Docker authority an apply needs but sits on an ``internal: true`` network and
cannot reach PyPI, and `composer-agent` has the egress but reaches Docker only
through the read-only proxy. The deployer CLI is the one place that has both, so
composer re-runs *itself* in a sibling container with the project's runtime
volume attached.

The volume is discovered from the merged compose config — the service mount
targeting the runtime root names it — and it must already exist. ``docker run -v
name:path`` would otherwise create an empty one, which looks like a working
update against a runtime DjangoLux never reads.
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path
from typing import Iterator, List, Tuple

from .constants import INHERITED_SECRET_KEYS_ENV
from .secrets_manager import PLAINTEXT_ENV_CANDIDATES

DOCKER_SOCKET = "/var/run/docker.sock"


class RuntimeVolumeError(RuntimeError):
    """The runtime volume could not be located, so nothing was delegated."""


def _mounts(spec: dict) -> Iterator[Tuple[str, str, str]]:
    """Yield ``(kind, source, target)`` for one service's volumes.

    `docker compose config` emits long form, but a hand-written file read
    through another path can still carry ``name:/container/path:rw`` strings.
    """
    for entry in spec.get("volumes") or []:
        if isinstance(entry, dict):
            yield (
                str(entry.get("type") or "volume"),
                str(entry.get("source") or ""),
                str(entry.get("target") or ""),
            )
            continue
        parts = str(entry).split(":")
        if len(parts) < 2:
            continue
        source, target = parts[0], parts[1]
        kind = "bind" if source.startswith((".", "/", "~")) else "volume"
        yield kind, source, target


def find_runtime_volume(config: dict, runtime_root: str) -> str:
    """Compose's short name for the volume mounted at `runtime_root`, or ""."""
    root = str(runtime_root).rstrip("/")
    for spec in (config.get("services") or {}).values():
        if not isinstance(spec, dict):
            continue
        for kind, source, target in _mounts(spec):
            if kind == "volume" and source and target.rstrip("/") == root:
                return source
    return ""


def qualified_volume_name(config: dict, short_name: str) -> str:
    """The Docker-level name: an explicit `name:`, else `<project>_<short>`."""
    declared = (config.get("volumes") or {}).get(short_name)
    if isinstance(declared, dict):
        name = str(declared.get("name") or "").strip()
        if name:
            return name
    project = str(config.get("name") or "").strip()
    return f"{project}_{short_name}" if project else short_name


def self_image(runner) -> str:
    """The image this composer runs from, for the sibling container.

    `.Image` on our own container is an image ID, which is exactly right: the
    child is then the same build, not whatever `:latest` now points at. A native
    (non-container) composer has no such container and falls back to the tag the
    wrapper installs.
    """
    from .launcher import DEFAULT_SELF_IMAGE

    configured = os.environ.get("COMPOSER_SELF_IMAGE", "").strip()
    if configured:
        return configured
    ok, out, _err = runner(
        ["docker", "inspect", "--format", "{{.Image}}", socket.gethostname()]
    )
    resolved = out.strip() if ok else ""
    return resolved or DEFAULT_SELF_IMAGE


def secret_flags(env=None, project_dir=None) -> List[str]:
    """Pass the deployment's secrets to the child without printing them.

    ``-e KEY`` (no value) forwards this process's value, so nothing lands in the
    child's command line. A composer that was not launched by the wrapper has no
    inherited keys and points the child at the same env file the wrapper reads.
    """
    environ = os.environ if env is None else env
    raw = str(environ.get(INHERITED_SECRET_KEYS_ENV) or "")
    keys = [key.strip() for key in raw.replace(",", " ").split() if key.strip()]
    if keys:
        flags = ["-e", INHERITED_SECRET_KEYS_ENV]
        for key in keys:
            flags.extend(["-e", key])
        return flags
    root = Path(project_dir or os.getcwd())
    for candidate in PLAINTEXT_ENV_CANDIDATES:
        path = root / candidate
        if path.is_file() and os.access(path, os.R_OK):
            return ["--env-file", str(path)]
    return []


def build_delegated_command(
    *,
    image: str,
    volume: str,
    runtime_root: str,
    argv: List[str],
    project_dir: str,
    interactive: bool,
    socket_path: str = DOCKER_SOCKET,
    env=None,
) -> List[str]:
    """`docker run` for a composer that can see the runtime volume."""
    command = ["docker", "run", "--rm", "-i"]
    if interactive:
        command.append("-t")
    command.extend(["-v", f"{volume}:{runtime_root}:rw"])
    if Path(socket_path).exists():
        command.extend(["-v", f"{socket_path}:{socket_path}"])
    command.extend(["-v", f"{project_dir}:{project_dir}", "-w", project_dir])
    command.extend(secret_flags(env=env, project_dir=project_dir))
    command.append(image)
    command.extend(["dlux-update", *argv])
    # Appended last so they win over anything the caller typed: the child must
    # look where we mounted the volume, and must never delegate again.
    command.extend(["--runtime-root", runtime_root, "--no-delegate"])
    return command


def resolve_runtime_volume(launcher, runtime_root: str) -> str:
    """The existing Docker volume behind `runtime_root`, or raise."""
    config = launcher.compose_config_json()
    if not config:
        detail = getattr(launcher, "last_runtime_diagnostic", "") or ""
        raise RuntimeVolumeError(
            "Could not read the compose configuration from this directory."
            + (f"\n{detail}" if detail else "")
        )
    short_name = find_runtime_volume(config, runtime_root)
    if not short_name:
        raise RuntimeVolumeError(
            f"No service in this stack mounts {runtime_root}, so it has no "
            "DjangoLux runtime volume. This deployment does not use inline "
            "DjangoLux updates; update the image instead."
        )
    volume = qualified_volume_name(config, short_name)
    ok, _out, _err = launcher.run_command(["docker", "volume", "inspect", volume], timeout=20)
    if not ok:
        raise RuntimeVolumeError(
            f"The stack declares volume '{volume}' but Docker does not have it "
            "yet. Start the stack once (./start.sh) and try again."
        )
    return volume


def delegate_dlux_update(args, argv: List[str], *, launcher=None) -> int:
    """Re-run this `dlux-update` in a container that mounts the volume."""
    if launcher is None:
        from .launcher import DockerComposeLauncher

        launcher = DockerComposeLauncher()
        launcher.compose_file = args.file
        launcher.dev_mode = args.dev
        launcher.resolve_active_compose_files()
        # Best effort: the compose config below interpolates deployment
        # variables, and a failure here is reported by the config read itself.
        launcher.resolve_secrets()

    volume = resolve_runtime_volume(launcher, args.runtime_root)
    command = build_delegated_command(
        image=self_image(launcher.run_command),
        volume=volume,
        runtime_root=args.runtime_root,
        argv=list(argv),
        project_dir=os.getcwd(),
        interactive=sys.stdin.isatty() and sys.stdout.isatty(),
    )
    print(
        f"⟳ {args.runtime_root} lives in the stack's containers — "
        f"running this update with '{volume}' attached.",
        flush=True,
    )
    return launcher.run_command_interactive(command)
