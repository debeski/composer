import difflib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from .constants import DEFAULT_MIGRATOR_COMMAND, POST_START_LABEL


COMPOSER_UPDATER_START = "  # Composer-as-updater start"
COMPOSER_UPDATER_END = "  # Composer-as-updater end"
COMPOSER_AGENT_START = "  # DjangoLux Composer agent start"
COMPOSER_AGENT_END = "  # DjangoLux Composer agent end"
# Hardened-topology socket wiring (dedicated shared volume, not a dlux_runtime subpath).
COMPOSER_EXEC_SOCKET_DIR = "/run/composer-exec"
COMPOSER_EXEC_SOCKET_PATH = "/run/composer-exec/composer-exec.sock"
COMPOSER_EXEC_SOCKET_VOLUME = "composer_exec_sock"
MINIMUM_DLUX_VERSION = (1, 5, 0)
SAFE_RESTART_CANDIDATES = ("web", "celery", "smtp-relay", "caddy", "nginx")
RESTART_LABELS = {
    "web": "safe",
    "celery": "safe",
    "smtp-relay": "safe",
    "caddy": "safe",
    "nginx": "safe",
    "db": "protected",
    "database": "protected",
    "postgres": "protected",
    "postgresql": "protected",
    "redis": "protected",
    "docker-socket-proxy": "protected",
    "composer-agent": "protected",
    "composer-executor": "protected",
    "composer-updater": "protected",
    "dlux-updater": "protected",
}
PROTECTED_SERVICE_NAMES = (
    "db",
    "database",
    "postgres",
    "postgresql",
    "redis",
    "backup",
    "db-backup",
    "db_backup",
    "pgadmin",
)


class AgentInstallError(RuntimeError):
    pass


def _service_names(contents: str) -> set[str]:
    match = re.search(r"(?ms)^services:\s*\n(.*?)(?=^volumes:\s*$|^networks:\s*$|\Z)", contents)
    if not match:
        raise AgentInstallError("Could not find a standard top-level Compose services block.")
    return set(re.findall(r"(?m)^  ([A-Za-z0-9_-]+):\s*$", match.group(1)))


def _declared_networks(contents: str) -> set[str]:
    match = re.search(r"(?ms)^networks:\s*\n(.*?)(?=^\S|\Z)", contents)
    if not match:
        return set()
    return set(re.findall(r"(?m)^  ([A-Za-z0-9_-]+):\s*$", match.group(1)))


def _block_bodies(block: str) -> Dict[str, str]:
    entries = list(re.finditer(r"(?m)^  ([A-Za-z0-9_-]+):[ \t]*$", block))
    bodies: Dict[str, str] = {}
    for index, entry in enumerate(entries):
        end = entries[index + 1].start() if index + 1 < len(entries) else len(block)
        bodies[entry.group(1)] = block[entry.start() : end]
    return bodies


def _service_networks(body: str) -> list[str]:
    match = re.search(r"(?ms)^    networks:[ \t]*\n((?:^      -[ \t]+\S+[ \t]*\n?)+)", body)
    if not match:
        return []
    return re.findall(r"(?m)^      -[ \t]+(\S+)[ \t]*$", match.group(1))


def _environment_value(body: str, key: str) -> str:
    match = re.search(rf'(?m)^      {re.escape(key)}:[ \t]*"?(.*?)"?[ \t]*$', body)
    return match.group(1) if match else ""


def _legacy_topology(block: str, project_slug: str) -> Dict[str, Any]:
    """Carry the replaced updater's networks, version label, and image forward.

    Projects generated before the DjangoLux 1.5 scaffold name their networks
    `egress`/`docker_proxy` and stamp a deployment-specific version label, so
    deriving either from the project slug emits references the project never
    declares.
    """
    bodies = _block_bodies(block)
    updater = bodies.get("composer-updater", "")
    proxy = bodies.get("docker-socket-proxy", "")
    return {
        "proxy_networks": _service_networks(proxy),
        "agent_networks": _service_networks(updater),
        "version_label": _environment_value(updater, "COMPOSER_VERSION_LABEL")
        or f"org.{project_slug}.dlux_baked_version",
        "web_image": _environment_value(updater, "WEB_IMAGE")
        or f"${{WEB_IMAGE:-{project_slug.lower()}:latest}}",
    }


def _current_agent_topology(block: str, project_slug: str) -> Dict[str, Any]:
    """Carry the current agent block's networks, version label, and image into
    the hardened topology, so the migration never invents undeclared references."""
    bodies = _block_bodies(block)
    agent = bodies.get("composer-agent", "")
    proxy = bodies.get("docker-socket-proxy", "")
    return {
        "proxy_networks": _service_networks(proxy),
        "agent_networks": _service_networks(agent),
        "version_label": _environment_value(agent, "COMPOSER_VERSION_LABEL")
        or f"org.{project_slug}.dlux_baked_version",
        "web_image": _environment_value(agent, "WEB_IMAGE")
        or f"${{WEB_IMAGE:-{project_slug.lower()}:latest}}",
    }


def _networks_block(names: list[str]) -> str:
    if not names:
        return ""
    entries = "\n".join(f"      - {name}" for name in names)
    return f"\n    networks:\n{entries}"


def _agent_stack(project_slug: str, services: set[str], topology: Dict[str, Any]) -> str:
    restart_services = [name for name in SAFE_RESTART_CANDIDATES if name in services]
    excluded_services = ["composer-agent", "docker-socket-proxy"]
    excluded_services.extend(name for name in PROTECTED_SERVICE_NAMES if name in services)
    restart_value = ",".join(restart_services)
    exclusion_value = ",".join(excluded_services)
    image = topology["web_image"]
    version_label = topology["version_label"]
    proxy_networks = _networks_block(topology["proxy_networks"])
    agent_networks = _networks_block(topology["agent_networks"])
    return f'''{COMPOSER_AGENT_START}
  docker-socket-proxy:
    image: tecnativa/docker-socket-proxy:latest
    restart: always
    labels:
      org.dlux.restart: "protected"
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    environment:
      CONTAINERS: 1
      IMAGES: 1
      NETWORKS: 1
      VOLUMES: 1
      EVENTS: 1
      EXEC: 1
      POST: 1
      INFO: 1
      PING: 1
      VERSION: 1
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro{proxy_networks}

  composer-agent:
    image: debeski/composer:latest
    restart: unless-stopped
    labels:
      org.dlux.restart: "protected"
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    # Read-only file override so this uncapped UID-0 process can read the
    # project's 0600 .secrets/.env to deploy. No write/exec/setuid bypass.
    cap_add:
      - DAC_READ_SEARCH
    working_dir: "${{PWD}}"
    command:
      - agent
      - run
      - --trigger-file
      - /opt/dlux-runtime/state/image-update-request.json
      - --status-file
      - /opt/dlux-runtime/state/deploy-status.json
      - --bridge-dir
      - /opt/dlux-runtime/state/agent
      - --interval
      - "2"
      - --check-image
      - {image}
      - --availability-file
      - /opt/dlux-runtime/state/image-available.json
      - --check-interval
      - "3600"
    environment:
      DOCKER_HOST: "tcp://docker-socket-proxy:2375"
      WEB_IMAGE: "{image}"
      COMPOSER_CONTROL_URL: "${{COMPOSER_CONTROL_URL:-}}"
      COMPOSER_ENROLLMENT_TOKEN: "${{COMPOSER_ENROLLMENT_TOKEN:-}}"
      COMPOSER_AGENT_STATE_DIR: "/var/lib/composer-agent"
      COMPOSER_VERSION_LABEL: "{version_label}"
      COMPOSER_RELEASE_MANIFEST_LABEL: "org.dlux.project.release-manifest"
      COMPOSER_ACTIVE_VERSION_FILE: "/opt/dlux-runtime/state/active.json"
      COMPOSER_ACTIVE_VERSION_KEY: "version"
      COMPOSER_STATUS_FILE: "/opt/dlux-runtime/state/deploy-status.json"
      COMPOSER_WATCH_SELF_SERVICE: "composer-agent"
      COMPOSER_EXCLUDE_SERVICES: "{exclusion_value}"
      COMPOSER_AGENT_RESTART_SERVICES: "{restart_value}"
    volumes:
      - "${{PWD}}:${{PWD}}:ro"
      - dlux_runtime:/opt/dlux-runtime:rw
      - composer_agent_state:/var/lib/composer-agent:rw
    depends_on:
      docker-socket-proxy:
        condition: service_started{agent_networks}
{COMPOSER_AGENT_END}'''


def _hardened_stack(project_slug: str, services: set[str], topology: Dict[str, Any]) -> str:
    """The hardened topology block: a read-only docker-socket-proxy, a
    composer-executor holding the real docker.sock (the sole write authority), and
    a composer-agent that keeps only read-only proxy access and delegates writes
    to the executor over the shared unix socket.
    """
    restart_services = [name for name in SAFE_RESTART_CANDIDATES if name in services]
    excluded_services = ["composer-agent", "composer-executor", "docker-socket-proxy"]
    excluded_services.extend(name for name in PROTECTED_SERVICE_NAMES if name in services)
    restart_value = ",".join(restart_services)
    exclusion_value = ",".join(excluded_services)
    image = topology["web_image"]
    version_label = topology["version_label"]
    proxy_networks = _networks_block(topology["proxy_networks"])
    agent_networks = _networks_block(topology["agent_networks"])
    return f'''{COMPOSER_AGENT_START}
  docker-socket-proxy:
    image: tecnativa/docker-socket-proxy:latest
    restart: always
    labels:
      org.dlux.restart: "protected"
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    environment:
      CONTAINERS: 1
      IMAGES: 1
      EVENTS: 1
      INFO: 1
      PING: 1
      VERSION: 1
      NETWORKS: 0
      VOLUMES: 0
      POST: 0
      EXEC: 0
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro{proxy_networks}

  composer-executor:
    image: debeski/composer:latest
    restart: unless-stopped
    labels:
      org.dlux.restart: "protected"
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    # Read-only file override so this uncapped UID-0 process can read the
    # project's 0600 .secrets/.env to deploy. No write/exec/setuid bypass.
    cap_add:
      - DAC_READ_SEARCH
    working_dir: "${{PWD}}"
    command:
      - executor
      - run
      - --socket
      - {COMPOSER_EXEC_SOCKET_PATH}
      - --trigger-file
      - /opt/dlux-runtime/state/image-update-request.json
      - --status-file
      - /opt/dlux-runtime/state/deploy-status.json
      - --interval
      - "2"
    environment:
      WEB_IMAGE: "{image}"
      COMPOSER_EXECUTOR_SOCKET: "{COMPOSER_EXEC_SOCKET_PATH}"
      COMPOSER_VERSION_LABEL: "{version_label}"
      COMPOSER_RELEASE_MANIFEST_LABEL: "org.dlux.project.release-manifest"
      COMPOSER_ACTIVE_VERSION_FILE: "/opt/dlux-runtime/state/active.json"
      COMPOSER_ACTIVE_VERSION_KEY: "version"
      COMPOSER_STATUS_FILE: "/opt/dlux-runtime/state/deploy-status.json"
      COMPOSER_WATCH_SELF_SERVICE: "composer-executor"
      COMPOSER_EXCLUDE_SERVICES: "{exclusion_value}"
      COMPOSER_AGENT_RESTART_SERVICES: "{restart_value}"
    volumes:
      - "${{PWD}}:${{PWD}}:ro"
      - dlux_runtime:/opt/dlux-runtime:rw
      - {COMPOSER_EXEC_SOCKET_VOLUME}:{COMPOSER_EXEC_SOCKET_DIR}:rw
      - /var/run/docker.sock:/var/run/docker.sock:rw{proxy_networks}

  composer-agent:
    image: debeski/composer:latest
    restart: unless-stopped
    labels:
      org.dlux.restart: "protected"
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    working_dir: "${{PWD}}"
    command:
      - agent
      - run
      - --trigger-file
      - /opt/dlux-runtime/state/image-update-request.json
      - --status-file
      - /opt/dlux-runtime/state/deploy-status.json
      - --bridge-dir
      - /opt/dlux-runtime/state/agent
      - --interval
      - "2"
      - --check-image
      - {image}
      - --availability-file
      - /opt/dlux-runtime/state/image-available.json
      - --check-interval
      - "3600"
    environment:
      DOCKER_HOST: "tcp://docker-socket-proxy:2375"
      WEB_IMAGE: "{image}"
      COMPOSER_CONTROL_URL: "${{COMPOSER_CONTROL_URL:-}}"
      COMPOSER_ENROLLMENT_TOKEN: "${{COMPOSER_ENROLLMENT_TOKEN:-}}"
      COMPOSER_AGENT_STATE_DIR: "/var/lib/composer-agent"
      COMPOSER_EXECUTOR_SOCKET: "{COMPOSER_EXEC_SOCKET_PATH}"
      COMPOSER_VERSION_LABEL: "{version_label}"
      COMPOSER_RELEASE_MANIFEST_LABEL: "org.dlux.project.release-manifest"
      COMPOSER_ACTIVE_VERSION_FILE: "/opt/dlux-runtime/state/active.json"
      COMPOSER_ACTIVE_VERSION_KEY: "version"
      COMPOSER_STATUS_FILE: "/opt/dlux-runtime/state/deploy-status.json"
      COMPOSER_WATCH_SELF_SERVICE: "composer-agent"
      COMPOSER_EXCLUDE_SERVICES: "{exclusion_value}"
      COMPOSER_AGENT_RESTART_SERVICES: "{restart_value}"
    volumes:
      - "${{PWD}}:${{PWD}}:ro"
      - dlux_runtime:/opt/dlux-runtime:rw
      - composer_agent_state:/var/lib/composer-agent:rw
      - {COMPOSER_EXEC_SOCKET_VOLUME}:{COMPOSER_EXEC_SOCKET_DIR}:rw
    depends_on:
      docker-socket-proxy:
        condition: service_started
      composer-executor:
        condition: service_started{agent_networks}
{COMPOSER_AGENT_END}'''


def _ensure_deployer_read_cap(contents: str, project_slug: str) -> str:
    """Add ``cap_add: DAC_READ_SEARCH`` to the deploying role when missing.

    The deployer runs ``docker compose up``, which reads the project's 0600
    ``.secrets/.env``; under ``cap_drop: ALL`` a UID-0 process without
    ``CAP_DAC_READ_SEARCH`` cannot read a file it does not own, so the deploy
    fails the secrets guard. Stacks generated before the capability was required
    self-heal here. Targeted insert rather than a full block re-render, because
    the dlux scaffold and the composer generator emit slightly different blocks;
    a scoped edit is safe for both. No-op when the cap is already present.
    """
    if COMPOSER_AGENT_START not in contents:
        return contents
    start = contents.index(COMPOSER_AGENT_START)
    end = contents.index(COMPOSER_AGENT_END, start) + len(COMPOSER_AGENT_END)
    block = contents[start:end]
    bodies = _block_bodies(block)
    deployer = "composer-executor" if "composer-executor" in bodies else (
        "composer-agent" if "composer-agent" in bodies else "")
    if not deployer:
        return contents
    body = bodies[deployer]
    if "DAC_READ_SEARCH" in body:
        return contents
    cap_drop = re.compile(r"(?m)^    cap_drop:\n      - ALL\n")
    if not cap_drop.search(body):
        raise AgentInstallError(
            f"Cannot add the secrets read capability: {deployer} has no recognized cap_drop block."
        )
    healed = cap_drop.sub(
        "    cap_drop:\n      - ALL\n    cap_add:\n      - DAC_READ_SEARCH\n", body, count=1
    )
    return contents[:start] + block.replace(body, healed, 1) + contents[end:]


def _ensure_nested_role_commands(contents: str) -> str:
    """Normalize generated resident role commands to the nested CLI shape."""
    updated = contents
    for service, first, second in (
        ("composer-agent", "agent", "run"),
        ("composer-executor", "executor", "run"),
    ):
        span = _service_block_span(updated, service)
        if span is None:
            continue
        block = updated[span[0]:span[1]]
        old = f"    command:\n      - {first}\n"
        new = f"    command:\n      - {first}\n      - {second}\n"
        if old in block and new not in block:
            block = block.replace(old, new, 1)
            updated = updated[:span[0]] + block + updated[span[1]:]
    return updated


def _transform_compose(contents: str, project_slug: str) -> str:
    if COMPOSER_AGENT_START in contents:
        if (
            contents.count(COMPOSER_AGENT_START) != 1
            or contents.count(COMPOSER_AGENT_END) != 1
            or contents.count("  composer-agent:\n") != 1
        ):
            raise AgentInstallError("The existing Composer agent block is incomplete.")
        if COMPOSER_UPDATER_START in contents or "  composer-updater:\n" in contents:
            raise AgentInstallError("The project contains both agent and legacy updater services.")
        if not re.search(r"(?m)^  composer_agent_state:\s*$", contents):
            raise AgentInstallError("The existing Composer agent has no dedicated state volume.")
        return _ensure_nested_role_commands(_ensure_deployer_read_cap(contents, project_slug))
    if contents.count(COMPOSER_UPDATER_START) != 1 or contents.count(COMPOSER_UPDATER_END) != 1:
        raise AgentInstallError("No single recognized generated composer-updater block was found.")
    services = _service_names(contents)
    if "composer-updater" not in services or "docker-socket-proxy" not in services:
        raise AgentInstallError("The marked legacy updater block is not a recognized topology.")
    if "composer-agent" in services:
        raise AgentInstallError("An unmarked composer-agent service already exists.")
    start = contents.index(COMPOSER_UPDATER_START)
    end = contents.index(COMPOSER_UPDATER_END, start) + len(COMPOSER_UPDATER_END)
    topology = _legacy_topology(contents[start:end], project_slug)
    declared = _declared_networks(contents)
    referenced = set(topology["proxy_networks"]) | set(topology["agent_networks"])
    missing = sorted(referenced - declared)
    if declared and missing:
        raise AgentInstallError(
            "The legacy updater references undeclared networks: " + ", ".join(missing)
        )
    updated = contents[:start] + _agent_stack(project_slug, services, topology) + contents[end:]
    volume_anchor = re.compile(r"(?m)^  dlux_runtime:\s*$")
    if len(volume_anchor.findall(updated)) != 1:
        raise AgentInstallError("Expected one generated dlux_runtime volume anchor.")
    updated = volume_anchor.sub("  dlux_runtime:\n  composer_agent_state:", updated, count=1)
    return updated.replace(
        "  # Isolated path from composer-updater to the docker-socket-proxy only.",
        "  # Isolated path from composer-agent to the docker-socket-proxy only.",
        1,
    )


def _fresh_topology(contents: str, project_slug: str) -> Dict[str, Any]:
    """Derive the install topology from the project's own `web` service.

    A stack that never had Composer has no block to carry settings forward from,
    so the image and label come from `web` — the service that actually runs the
    project image — rather than being guessed from the slug.
    """
    match = re.search(r"(?ms)^services:\s*\n(.*?)(?=^volumes:\s*$|^networks:\s*$|\Z)", contents)
    if not match:
        raise AgentInstallError("Could not find a standard top-level Compose services block.")
    web = _block_bodies(match.group(1)).get("web", "")
    if not web:
        raise AgentInstallError(
            "No 'web' service found; Composer cannot derive the project image to watch."
        )
    image_match = re.search(r"(?m)^    image:[ \t]*(\S.*?)[ \t]*$", web)
    if not image_match:
        raise AgentInstallError("The 'web' service declares no image for Composer to watch.")
    declared = _declared_networks(contents)
    # Mirror the generated scaffold: the proxy and executor sit on an isolated
    # docker_proxy network, the agent adds egress for the control plane. A project
    # without an `egress` network keeps docker_proxy non-internal instead, so the
    # agent does not lose outbound access (see install_composer_stack).
    agent_networks = ["docker_proxy"]
    if "egress" in declared:
        agent_networks = ["egress", "docker_proxy"]
    return {
        "proxy_networks": ["docker_proxy"],
        "agent_networks": agent_networks,
        "version_label": f"org.{project_slug}.dlux_baked_version",
        "web_image": image_match.group(1),
    }


def _transform_to_installed(contents: str, project_slug: str) -> str:
    """Install the hardened Composer trio into a stack that has none.

    DjangoLux 1.8.0 hands its inline updates to Composer, so Composer is a
    required service in a DjangoLux deployment, not only the deployer. Projects
    generated by the 1.8.0 scaffold already ship this block; this transform is
    for the ones generated before it.

    A no-op (returns the contents unchanged) when any Composer service already
    exists, so `check --fix` can call it unconditionally.
    """
    services = _service_names(contents)
    if services & {"composer-agent", "composer-executor", "composer-updater"}:
        return contents
    if COMPOSER_AGENT_START in contents or COMPOSER_UPDATER_START in contents:
        return contents
    if not re.search(r"(?m)^  dlux_runtime:\s*$", contents):
        raise AgentInstallError(
            "No dlux_runtime volume declared; this does not look like a generated "
            "DjangoLux project."
        )
    if "docker-socket-proxy" in services:
        raise AgentInstallError(
            "A docker-socket-proxy service exists without any Composer service; "
            "Composer refuses to guess what owns it."
        )

    topology = _fresh_topology(contents, project_slug)
    volumes_anchor = re.search(r"(?m)^volumes:[ \t]*$", contents)
    if not volumes_anchor:
        raise AgentInstallError("Could not find a top-level Compose volumes block.")
    block = _hardened_stack(project_slug, services, topology)
    updated = (
        contents[: volumes_anchor.start()]
        + block.rstrip("\n")
        + "\n\n"
        + contents[volumes_anchor.start():]
    )

    runtime_anchor = re.compile(r"(?m)^  dlux_runtime:[ \t]*$")
    if len(runtime_anchor.findall(updated)) != 1:
        raise AgentInstallError("Expected one dlux_runtime volume anchor.")
    updated = runtime_anchor.sub(
        "  dlux_runtime:\n  composer_agent_state:\n"
        f"  {COMPOSER_EXEC_SOCKET_VOLUME}:",
        updated,
        count=1,
    )

    declared = _declared_networks(updated)
    if "docker_proxy" not in declared:
        # Internal only when the agent has a separate egress route; otherwise
        # this network is its only one and must not cut off the control plane.
        isolation = "\n    internal: true" if "egress" in declared else ""
        networks_anchor = re.search(r"(?m)^networks:[ \t]*$", updated)
        if not networks_anchor:
            raise AgentInstallError("Could not find a top-level Compose networks block.")
        insert_at = networks_anchor.end()
        updated = (
            updated[:insert_at]
            + f"\n  # Docker-control path: the agent's read-only route to "
              f"docker-socket-proxy,\n  # plus composer-executor."
              f"\n  docker_proxy:{isolation}"
            + updated[insert_at:]
        )
    return updated


# Markers the DjangoLux scaffold wrapped its updater service in, before 1.8.0
# retired it.
DLUX_UPDATER_START = "  # DjangoLux updater start"
DLUX_UPDATER_END = "  # DjangoLux updater end"

# The init containers a migrated stack gains, mirroring the 1.8.0 scaffold.
# Declared on celery, not web: a pre_start step inherits its service's mounts and
# dlux_reconcile writes the runtime pointer, which is read-only on web — there it
# swallows the error and no-ops forever. DLUX_BOOT_GATE=off is required because a
# step also inherits the entrypoint, whose gate waits on these very migrations.
DLUX_PRE_START_BLOCK = """    pre_start:
      - environment:
          DLUX_BOOT_GATE: "off"
        command: ["python", "-m", "dlux.updater.supervisor", "--no-watch", "--", "python", "manage.py", "dlux_reconcile"]
      - environment:
          DLUX_BOOT_GATE: "off"
        command: ["sh", "-c", "python -m dlux.updater.supervisor --no-watch -- python manage.py migrator ${DLUX_MIGRATOR_FLAGS:-}"]
"""


def _service_block_span(contents: str, service: str):
    """(start, end) of a service's block in the services section, or None."""
    section = re.search(r"(?ms)^services:\s*\n(.*?)(?=^volumes:\s*$|^networks:\s*$|\Z)", contents)
    if not section:
        return None
    header = re.search(rf"(?m)^  {re.escape(service)}:[ \t]*$", contents)
    if not header or not (section.start() <= header.start() < section.end()):
        return None
    nxt = re.search(r"(?m)^  [A-Za-z0-9_-]+:[ \t]*$", contents[header.end():section.end()])
    end = header.end() + nxt.start() if nxt else section.end()
    return header.start(), end


def _drop_depends_on_entry(block: str, service: str) -> str:
    """Remove one `depends_on` entry, and the key itself if it empties."""
    entry = re.compile(
        rf"(?m)^      {re.escape(service)}:[ \t]*\n(?:^        \S.*\n)*"
    )
    updated = entry.sub("", block)
    # A `depends_on:` with no children is not valid Compose.
    return re.sub(
        r"(?m)^    depends_on:[ \t]*\n(?=(?:^    \S|^  \S|\Z))", "", updated
    )


def _transform_to_init_containers(contents: str, project_slug: str) -> str:
    """Retire the dlux-updater service in favour of Compose init containers.

    DjangoLux 1.8.0 hands update execution to Composer, which left that service
    with only boot work — runtime reconcile and migrations — and small JSON
    writes the Celery beat schedule now carries. Both become `pre_start` steps on
    celery, so Compose enforces the ordering and a failed migration stops the
    stack instead of half-starting it.

    A no-op when the stack has already been migrated, so `check --fix` may call
    it unconditionally.
    """
    services = _service_names(contents)
    celery = _service_block_span(contents, "celery")
    if celery is None:
        raise AgentInstallError(
            "No 'celery' service found; the DjangoLux init containers have nowhere to run."
        )
    if "dlux-updater" not in services and "    pre_start:" in contents[celery[0]:celery[1]]:
        return contents

    updated = contents
    if "dlux-updater" in services:
        if DLUX_UPDATER_START in updated and DLUX_UPDATER_END in updated:
            start = updated.index(DLUX_UPDATER_START)
            end = updated.index(DLUX_UPDATER_END, start) + len(DLUX_UPDATER_END) + 1
            updated = updated[:start] + updated[end:]
        else:
            from .stack_cleanup import remove_obsolete_service_blocks

            updated, removed = remove_obsolete_service_blocks(updated, {"dlux-updater"})
            if "dlux-updater" not in removed:
                raise AgentInstallError(
                    "Could not locate a removable 'dlux-updater' service block."
                )

    # Nothing may still declare a dependency on the removed service.
    for name in _service_names(updated):
        span = _service_block_span(updated, name)
        if span is None:
            continue
        block = updated[span[0]:span[1]]
        rewritten = _drop_depends_on_entry(block, "dlux-updater")
        if rewritten != block:
            updated = updated[:span[0]] + rewritten + updated[span[1]:]

    # The migrator moved ahead of the health gate, so the post-start hook that
    # ran it afterwards would now be a second, redundant migrator run.
    web = _service_block_span(updated, "web")
    if web is not None:
        block = updated[web[0]:web[1]]
        stripped = re.sub(rf'(?m)^      {re.escape(POST_START_LABEL)}:.*\n', "", block)
        stripped = re.sub(r"(?m)^    post_start:[ \t]*\n(?:^(?:      .*)?\n)*", "", stripped)
        # Drop a `labels:` key left with no children.
        stripped = re.sub(r"(?m)^    labels:[ \t]*\n(?=(?:^    \S|^  \S|\Z))", "", stripped)
        updated = updated[:web[0]] + stripped + updated[web[1]:]

    span = _service_block_span(updated, "celery")
    block = updated[span[0]:span[1]]
    if "    pre_start:" not in block:
        anchor = re.search(r"(?m)^    volumes:[ \t]*$", block)
        if not anchor:
            raise AgentInstallError("The 'celery' service declares no volumes block.")
        block = block[:anchor.start()] + DLUX_PRE_START_BLOCK + block[anchor.start():]
    # The steps write the runtime pointer and collect static.
    block = block.replace("dlux_runtime:/opt/dlux-runtime:ro", "dlux_runtime:/opt/dlux-runtime:rw")
    block = re.sub(r"(?m)^(      - static:/app/staticfiles):ro$", r"\1:rw", block)
    updated = updated[:span[0]] + block + updated[span[1]:]
    return updated


def _ensure_environment_value(block: str, key: str, value: str) -> str:
    line = f'      {key}: "{value}"\n'
    pattern = re.compile(rf"(?m)^      {re.escape(key)}:[ \t]*.*\n")
    if pattern.search(block):
        return pattern.sub(line, block, count=1)
    if "    environment:\n" in block:
        return block.replace("    environment:\n", "    environment:\n" + line, 1)
    header = re.match(r"(?m)^  ([A-Za-z0-9_-]+):[ \t]*\n", block)
    if not header:
        return block
    return block[:header.end()] + "    environment:\n" + line + block[header.end():]


def _transform_dev_init_override(contents: str, project_slug: str) -> str:
    """Normalize compose.dev.yml after dlux-updater moves to celery pre_start."""
    updated = contents
    if "  dlux-updater:\n" in updated:
        from .stack_cleanup import remove_obsolete_service_blocks

        updated, _removed = remove_obsolete_service_blocks(updated, {"dlux-updater"})
    updated = _migrate_dlux_updater_command(updated, project_slug)
    for service in ("web", "celery"):
        span = _service_block_span(updated, service)
        if span is None:
            continue
        block = updated[span[0]:span[1]]
        if service == "celery":
            block = block.replace(
                "dlux_runtime:/opt/dlux-runtime:ro",
                "dlux_runtime:/opt/dlux-runtime:rw",
            )
        block = _ensure_environment_value(block, "DLUX_INLINE_UPDATES_ENABLED", "False")
        updated = updated[:span[0]] + block + updated[span[1]:]
    return updated


def _transform_to_hardened(contents: str, project_slug: str) -> str:
    """Rewrite a current composer-agent block into the hardened topology:
    read-only proxy + composer-executor (sole write authority) + agent that
    delegates writes. Idempotent, and refuses anything it does not recognize."""
    if COMPOSER_AGENT_START not in contents:
        raise AgentInstallError(
            "No recognized composer-agent block to harden. Run 'composer agent enable' first."
        )
    if (
        contents.count(COMPOSER_AGENT_START) != 1
        or contents.count(COMPOSER_AGENT_END) != 1
        or contents.count("  composer-agent:\n") != 1
    ):
        raise AgentInstallError("The existing Composer agent block is incomplete.")
    services = _service_names(contents)
    # Already hardened: no re-render, but heal a missing secrets read capability.
    if "composer-executor" in services:
        if "  composer-executor:\n" not in contents:
            raise AgentInstallError("An unmarked composer-executor service already exists.")
        return _ensure_nested_role_commands(_ensure_deployer_read_cap(contents, project_slug))
    if "composer-agent" not in services or "docker-socket-proxy" not in services:
        raise AgentInstallError("The marked agent block is not a recognized topology.")
    if COMPOSER_UPDATER_START in contents or "  composer-updater:\n" in contents:
        raise AgentInstallError("The project contains both agent and legacy updater services.")
    start = contents.index(COMPOSER_AGENT_START)
    end = contents.index(COMPOSER_AGENT_END, start) + len(COMPOSER_AGENT_END)
    block = contents[start:end]
    topology = _current_agent_topology(block, project_slug)
    declared = _declared_networks(contents)
    referenced = set(topology["proxy_networks"]) | set(topology["agent_networks"])
    missing = sorted(referenced - declared)
    if declared and missing:
        raise AgentInstallError(
            "The agent block references undeclared networks: " + ", ".join(missing)
        )
    updated = contents[:start] + _hardened_stack(project_slug, services, topology) + contents[end:]
    volume_anchor = re.compile(r"(?m)^  composer_agent_state:\s*$")
    if len(volume_anchor.findall(updated)) != 1:
        raise AgentInstallError("Expected one generated composer_agent_state volume anchor.")
    updated = volume_anchor.sub(
        f"  composer_agent_state:\n  {COMPOSER_EXEC_SOCKET_VOLUME}:", updated, count=1
    )
    return updated


def _post_start_blocks(contents: str) -> Dict[str, tuple[str, str]]:
    """Map service -> (raw post_start block, single command) for native hooks.

    Services whose hook holds anything other than exactly one ``- command:``
    entry are left out; those are not the generated shape and are not safe to
    fold into a single label.
    """
    match = re.search(r"(?ms)^services:\s*\n(.*?)(?=^volumes:\s*$|^networks:\s*$|\Z)", contents)
    if not match:
        return {}
    found: Dict[str, tuple[str, str]] = {}
    for service, body in _block_bodies(match.group(1)).items():
        # MULTILINE only: DOTALL here would let `.*` run past the end of the
        # hook and swallow the rest of the service body.
        block = re.search(r"(?m)^    post_start:[ \t]*\n(?:^(?:      .*)?\n)*", body)
        if not block:
            continue
        entries = re.findall(r"(?m)^      -\s+command:\s+(.+?)\s*$", block.group(0))
        if len(entries) != 1:
            continue
        found[service] = (block.group(0), entries[0])
    return found


def _transform_post_start_to_label(contents: str, project_slug: str) -> str:
    """Replace native Compose ``post_start`` hooks with the composer label.

    Compose runs a native hook itself, on container start, without composer's
    flags — while composer separately execs the same command after health with
    them. Two migrators overlap, one clearing staticfiles under the other. The
    label removes Compose as a runner and leaves composer the only one.
    """
    blocks = _post_start_blocks(contents)
    if not blocks:
        # Existing DLUX updater projects can have neither declaration: the
        # updater was enabled before the label contract existed, and its
        # surgical upgrade path never revisited the web service. This topology
        # is specific enough to install the standard DLUX migrator safely.
        if (
            "  dlux-updater:\n" not in contents
            or "  web:\n" not in contents
            or POST_START_LABEL in contents
        ):
            return contents
        body_match = re.search(
            r"(?ms)^  web:[ \t]*\n.*?(?=^  [A-Za-z0-9_-]+:[ \t]*$|^volumes:[ \t]*$|^networks:[ \t]*$|\Z)",
            contents,
        )
        if not body_match:
            return contents
        body = body_match.group(0)
        command = DEFAULT_MIGRATOR_COMMAND
        updater_module = re.search(
            r"\b(?:tools\.dlux_runtime_supervisor|dlux\.updater\.supervisor)\b",
            contents,
        )
        if updater_module:
            command = command.replace("dlux.updater.supervisor", updater_module.group(0))
        label_line = f'      {POST_START_LABEL}: "{command}"\n'
        if "    labels:\n" in body:
            updated_body = body.replace("    labels:\n", "    labels:\n" + label_line, 1)
        else:
            updated_body = body.replace("  web:\n", "  web:\n    labels:\n" + label_line, 1)
        return contents[: body_match.start()] + updated_body + contents[body_match.end() :]
    updated = contents
    for service, (block, command) in blocks.items():
        if f"{POST_START_LABEL}:" in block:
            continue
        label_line = f'      {POST_START_LABEL}: "{command}"\n'
        body_match = re.search(
            rf"(?ms)^  {re.escape(service)}:[ \t]*\n.*?(?=^  [A-Za-z0-9_-]+:[ \t]*$|\Z)", updated
        )
        if not body_match:
            continue
        body = body_match.group(0)
        if POST_START_LABEL in body:
            continue
        stripped = body.replace(block, "", 1)
        if "    labels:\n" in stripped:
            stripped = stripped.replace("    labels:\n", "    labels:\n" + label_line, 1)
        else:
            stripped = stripped.replace(
                f"  {service}:\n", f"  {service}:\n    labels:\n" + label_line, 1
            )
        updated = updated[: body_match.start()] + stripped + updated[body_match.end() :]
    return updated


def _unquote(value: str) -> str:
    value = str(value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _is_local_tools_mount(source: str, target: str) -> bool:
    source = _unquote(source).rstrip("/")
    target = _unquote(target).rstrip("/")
    normalized_source = source.replace("\\", "/")
    return target in {
        "/app/tools",
        "/app/tools/smtp-relay",
        "/app/tools/smtp_relay",
        "/app/smtp-relay",
        "/app/smtp_relay",
    } and (
        normalized_source == "./tools"
        or normalized_source == "../tools"
        or normalized_source.endswith("/tools")
        or normalized_source.endswith("/tools/smtp-relay")
        or normalized_source.endswith("/tools/smtp_relay")
    )


def _volume_item_is_local_tools_mount(lines: list[str]) -> bool:
    first = lines[0].strip()
    if first.startswith("- "):
        value = first[2:].split("#", 1)[0].strip()
        short = _unquote(value)
        if ":" in short:
            parts = short.split(":")
            for index in range(1, len(parts)):
                if _is_local_tools_mount(":".join(parts[:index]), parts[index]):
                    return True

    source = target = kind = ""
    for line in lines:
        match = re.search(r"(?m)^\s+(source|target|type):\s*(.*?)\s*$", line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).split("#", 1)[0].strip()
        if key == "source":
            source = value
        elif key == "target":
            target = value
        elif key == "type":
            kind = _unquote(value)
    return bool(source and target and kind in {"", "bind"} and _is_local_tools_mount(source, target))


def _drop_local_tools_mounts_from_service(block: str) -> str:
    lines = block.splitlines(keepends=True)
    output = []
    index = 0
    while index < len(lines):
        if not re.match(r"^    volumes:[ \t]*$", lines[index]):
            output.append(lines[index])
            index += 1
            continue

        header = lines[index]
        index += 1
        kept = []
        removed = False
        while index < len(lines) and not re.match(r"^    [A-Za-z0-9_-]+:[ \t]*$", lines[index]):
            if re.match(r"^      -", lines[index]):
                item = [lines[index]]
                index += 1
                while (
                    index < len(lines)
                    and not re.match(r"^      -", lines[index])
                    and not re.match(r"^    [A-Za-z0-9_-]+:[ \t]*$", lines[index])
                ):
                    item.append(lines[index])
                    index += 1
                if _volume_item_is_local_tools_mount(item):
                    removed = True
                else:
                    kept.extend(item)
            else:
                kept.append(lines[index])
                index += 1

        if not removed or any(re.match(r"^      -", line) for line in kept):
            output.append(header)
            output.extend(kept)
    return "".join(output)


def _drop_local_tools_mounts(contents: str) -> str:
    updated = contents
    spans = []
    for service in _service_names(contents):
        span = _service_block_span(contents, service)
        if span is not None:
            spans.append(span)
    for start, end in sorted(spans, reverse=True):
        block = updated[start:end]
        rewritten = _drop_local_tools_mounts_from_service(block)
        if rewritten != block:
            updated = updated[:start] + rewritten + updated[end:]
    return updated


def _ensure_restart_labels(contents: str, project_slug: str) -> str:
    updated = contents
    spans = []
    for service in _service_names(contents):
        if service not in RESTART_LABELS:
            continue
        span = _service_block_span(contents, service)
        if span is not None:
            spans.append((span[0], span[1], service))
    for start, end, service in sorted(spans, reverse=True):
        block = updated[start:end]
        if "org.dlux.restart" in block:
            continue
        label_line = f'      org.dlux.restart: "{RESTART_LABELS[service]}"\n'
        if "    labels:\n" in block:
            rewritten = block.replace("    labels:\n", "    labels:\n" + label_line, 1)
        else:
            rewritten = block.replace(
                f"  {service}:\n",
                f"  {service}:\n    labels:\n{label_line}",
                1,
            )
        updated = updated[:start] + rewritten + updated[end:]
    return updated


def _runtime_migration_floor(contents: str) -> tuple[int, int, int]:
    floor = (0, 0, 0)
    if "tools.dlux_runtime_supervisor" in contents or "/app/tools" in contents:
        floor = max(floor, DLUX_PACKAGED_RUNTIME_MIN)
    if "tools.smtp_relay" in contents or "/app/tools/smtp" in contents:
        floor = max(floor, DLUX_SMTP_RELAY_MIN)
    return floor


def _dlux_readiness_warning(project_root: Path) -> tuple[str, bool]:
    declarations = []
    for path in (project_root / "requirements.txt", project_root / "pyproject.toml"):
        try:
            declarations.append(path.read_text(encoding="utf-8"))
        except OSError:
            continue
    if not declarations:
        return (
            "No dependency manifest is present, so the DjangoLux 1.5.0+ bridge could not be "
            "verified from this directory.",
            False,
        )
    combined = "\n".join(declarations)
    matches = re.findall(
        r"django-lux(?:\[[^\]]+\])?\s*(?:==|>=|~=)\s*[\"']?(\d+)\.(\d+)(?:\.(\d+))?",
        combined,
        flags=re.IGNORECASE,
    )
    if matches:
        versions = [tuple(int(part or 0) for part in match) for match in matches]
        if max(versions) >= MINIMUM_DLUX_VERSION:
            return "", False
        return "DjangoLux 1.5.0 or newer is required for the typed local agent bridge.", True
    if re.search(r"django-lux", combined, flags=re.IGNORECASE):
        return "Could not verify that the declared DjangoLux dependency is 1.5.0 or newer.", True
    return "Could not find a DjangoLux dependency declaration to verify the local agent bridge.", True


def _backup_root(project_root: Path) -> Path:
    base = project_root / ".xpose" / "dlux-agent-bootstrap"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = base / stamp
    suffix = 1
    while destination.exists():
        suffix += 1
        destination = base / f"{stamp}-{suffix}"
    return destination


def _atomic_write(path: Path, contents: str):
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    mode = stat.S_IMODE(path.stat().st_mode)
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(mode)
    os.replace(temporary, path)


def _selected_compose_path(project_dir: str, compose_file: str) -> tuple[Path, str, Path]:
    project_root = Path(project_dir).resolve()
    selected_file = compose_file or (
        "compose.yml" if (project_root / "compose.yml").is_file() else "docker-compose.yml"
    )
    compose_path = (project_root / selected_file).resolve()
    if not compose_path.is_relative_to(project_root) or not compose_path.is_file():
        raise AgentInstallError("The selected Compose file must exist inside the project directory.")
    return project_root, selected_file, compose_path


def dlux_runtime_migration_floor(project_dir: str = ".", *, compose_file: str = ""):
    _project_root, _selected_file, compose_path = _selected_compose_path(project_dir, compose_file)
    return _runtime_migration_floor(compose_path.read_text(encoding="utf-8"))


def _apply_stack_migration(
    project_dir: str,
    *,
    compose_file: str,
    transform,
    redeploy_command: str,
    apply: bool,
    allow_unverified_dlux: bool,
    include_diff: bool,
    command_runner,
) -> Dict[str, Any]:
    """Shared dry-run-first stack migration: transform the Compose file, and on
    --apply validate with `docker compose config`, back up to .xpose/, and
    atomically write. Used by both agent enable and executor enable."""
    project_root, selected_file, compose_path = _selected_compose_path(project_dir, compose_file)
    contents = compose_path.read_text(encoding="utf-8")
    name_match = re.search(r"(?m)^name:\s*([A-Za-z0-9_-]+)\s*$", contents)
    if not name_match:
        raise AgentInstallError("Could not determine the generated Compose project name.")
    updated = transform(contents, name_match.group(1))
    changed = [str(compose_path.relative_to(project_root))] if updated != contents else []
    warning, blocking = _dlux_readiness_warning(project_root) if changed else ("", False)
    result: Dict[str, Any] = {
        "applied": False,
        "files": changed,
        "command": redeploy_command if changed else "",
        "backup_root": "",
        "warnings": [warning] if warning else [],
    }
    if include_diff and changed:
        result["diff"] = "".join(
            difflib.unified_diff(
                contents.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=f"a/{selected_file}",
                tofile=f"b/{selected_file}",
            )
        )
    if not apply:
        return result
    if warning and blocking and not allow_unverified_dlux:
        raise AgentInstallError(f"{warning} Upgrade DjangoLux first or pass --allow-unverified-dlux.")
    if not shutil.which("docker"):
        raise AgentInstallError("Docker is required to validate the generated Compose configuration.")
    probe = command_runner(
        ["docker", "compose", "version"],
        cwd=str(project_root),
        check=False,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        raise AgentInstallError("Docker Compose v2 is required to apply the stack migration.")
    validation = command_runner(
        ["docker", "compose", "--project-directory", str(project_root), "-f", "-", "config"],
        cwd=str(project_root),
        check=False,
        capture_output=True,
        text=True,
        input=updated,
    )
    if validation.returncode != 0:
        detail = str(validation.stderr or "").strip()[:1000]
        suffix = f": {detail}" if detail else ""
        raise AgentInstallError(f"docker compose config failed; no project files were changed{suffix}")
    if changed:
        backup_root = _backup_root(project_root)
        backup_path = backup_root / compose_path.relative_to(project_root)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(compose_path, backup_path)
        _atomic_write(compose_path, updated)
        result["backup_root"] = str(backup_root)
    result["applied"] = True
    return result


def _apply_dev_override_migration(
    project_dir: str,
    *,
    compose_file: str,
    base_file: str,
    apply: bool,
    include_diff: bool,
    command_runner,
) -> Dict[str, Any]:
    project_root, selected_file, compose_path = _selected_compose_path(project_dir, compose_file)
    base_path = (project_root / base_file).resolve()
    if not base_path.is_relative_to(project_root) or not base_path.is_file():
        raise AgentInstallError("The base Compose file must exist inside the project directory.")
    contents = compose_path.read_text(encoding="utf-8")
    name_match = re.search(r"(?m)^name:\s*([A-Za-z0-9_-]+)\s*$", contents)
    if not name_match:
        raise AgentInstallError("Could not determine the generated Compose project name.")
    updated = _transform_dev_init_override(contents, name_match.group(1))
    changed = [str(compose_path.relative_to(project_root))] if updated != contents else []
    result: Dict[str, Any] = {
        "applied": False,
        "files": changed,
        "command": "docker compose -f compose.yml -f compose.dev.yml up -d" if changed else "",
        "backup_root": "",
        "warnings": [],
    }
    if include_diff and changed:
        result["diff"] = "".join(
            difflib.unified_diff(
                contents.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=f"a/{selected_file}",
                tofile=f"b/{selected_file}",
            )
        )
    if not apply:
        return result
    if not shutil.which("docker"):
        raise AgentInstallError("Docker is required to validate the generated Compose configuration.")
    probe = command_runner(
        ["docker", "compose", "version"],
        cwd=str(project_root),
        check=False,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        raise AgentInstallError("Docker Compose v2 is required to apply the dev override migration.")
    validation = command_runner(
        ["docker", "compose", "--project-directory", str(project_root),
         "-f", str(base_path), "-f", "-", "config"],
        cwd=str(project_root),
        check=False,
        capture_output=True,
        text=True,
        input=updated,
    )
    if validation.returncode != 0:
        detail = str(validation.stderr or "").strip()[:1000]
        suffix = f": {detail}" if detail else ""
        raise AgentInstallError(
            f"docker compose config failed for the dev override; no project files were changed{suffix}"
        )
    if changed:
        backup_root = _backup_root(project_root)
        backup_path = backup_root / compose_path.relative_to(project_root)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(compose_path, backup_path)
        _atomic_write(compose_path, updated)
        result["backup_root"] = str(backup_root)
    result["applied"] = True
    return result


def enable_agent(
    project_dir: str = ".",
    *,
    compose_file: str = "",
    apply: bool = False,
    allow_unverified_dlux: bool = False,
    include_diff: bool = False,
    command_runner=subprocess.run,
) -> Dict[str, Any]:
    return _apply_stack_migration(
        project_dir,
        compose_file=compose_file,
        transform=_transform_compose,
        redeploy_command="docker compose up -d --force-recreate docker-socket-proxy composer-agent",
        apply=apply,
        allow_unverified_dlux=allow_unverified_dlux,
        include_diff=include_diff,
        command_runner=command_runner,
    )


def migrate_dlux_init_containers(
    project_dir: str = ".",
    *,
    compose_file: str = "",
    apply: bool = False,
    allow_unverified_dlux: bool = False,
    include_diff: bool = False,
    command_runner=subprocess.run,
) -> Dict[str, Any]:
    """Retire dlux-updater in favour of Compose init containers on celery."""
    return _apply_stack_migration(
        project_dir,
        compose_file=compose_file,
        transform=_transform_to_init_containers,
        redeploy_command="docker compose up -d --force-recreate celery web",
        apply=apply,
        allow_unverified_dlux=allow_unverified_dlux,
        include_diff=include_diff,
        command_runner=command_runner,
    )


def migrate_dlux_dev_override(
    project_dir: str = ".",
    *,
    compose_file: str = "compose.dev.yml",
    base_file: str = "compose.yml",
    apply: bool = False,
    include_diff: bool = False,
    command_runner=subprocess.run,
) -> Dict[str, Any]:
    """Normalize the development override for the init-container topology."""
    return _apply_dev_override_migration(
        project_dir,
        compose_file=compose_file,
        base_file=base_file,
        apply=apply,
        include_diff=include_diff,
        command_runner=command_runner,
    )


def install_composer_stack(
    project_dir: str = ".",
    *,
    compose_file: str = "",
    apply: bool = False,
    allow_unverified_dlux: bool = False,
    include_diff: bool = False,
    command_runner=subprocess.run,
) -> Dict[str, Any]:
    """Add the hardened Composer trio to a DjangoLux stack that has none.

    Since DjangoLux 1.8.0 the updater hands execution to Composer, so a Composer
    service is required in the deployment, not just on the deploying machine.
    """
    return _apply_stack_migration(
        project_dir,
        compose_file=compose_file,
        transform=_transform_to_installed,
        redeploy_command=(
            "docker compose up -d docker-socket-proxy composer-executor composer-agent"
        ),
        apply=apply,
        allow_unverified_dlux=allow_unverified_dlux,
        include_diff=include_diff,
        command_runner=command_runner,
    )


def enable_executor(
    project_dir: str = ".",
    *,
    compose_file: str = "",
    apply: bool = False,
    allow_unverified_dlux: bool = False,
    include_diff: bool = False,
    command_runner=subprocess.run,
) -> Dict[str, Any]:
    return _apply_stack_migration(
        project_dir,
        compose_file=compose_file,
        transform=_transform_to_hardened,
        redeploy_command=(
            "docker compose up -d --force-recreate "
            "docker-socket-proxy composer-executor composer-agent"
        ),
        apply=apply,
        allow_unverified_dlux=allow_unverified_dlux,
        include_diff=include_diff,
        command_runner=command_runner,
    )


def enable_post_start_label(
    project_dir: str = ".",
    *,
    compose_file: str = "",
    apply: bool = False,
    allow_unverified_dlux: bool = False,
    include_diff: bool = False,
    command_runner=subprocess.run,
) -> Dict[str, Any]:
    return _apply_stack_migration(
        project_dir,
        compose_file=compose_file,
        transform=_transform_post_start_to_label,
        redeploy_command="docker compose up -d --force-recreate web",
        apply=apply,
        allow_unverified_dlux=allow_unverified_dlux,
        include_diff=include_diff,
        command_runner=command_runner,
    )


# First dlux releases that ship modules old scaffolds used to run from local
# tools/. The migration must only be applied to an image at or above the module
# it needs, or the compose would point at imports the image lacks.
DLUX_PACKAGED_RUNTIME_MIN = (1, 6, 2)
DLUX_SMTP_RELAY_MIN = (1, 7, 0)


def parse_dlux_version(text):
    """(major, minor, patch) parsed from a `dlux --version` line, or None."""
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", str(text or ""))
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))


def _migrate_dlux_updater_command(contents: str, project_slug: str) -> str:
    """Point existing runtime references at the packaged DjangoLux runtime.

    The supervisor moved into the dlux package. Pre-1.6.2 scaffolds also mounted
    the project's local ``tools/`` package into containers at ``/app/tools``;
    that mount must go once compose commands import ``dlux.updater.supervisor``.
    Existing dlux-updater services get the pre-migration ``dlux_reconcile`` guard
    so a stale pinned release can't wedge the boot chain behind a maintenance
    screen.
    """
    migrated = contents.replace("tools.dlux_runtime_supervisor", "dlux.updater.supervisor")
    migrated = migrated.replace("tools.smtp_relay", "dlux.smtp_relay")
    migrated = _drop_local_tools_mounts(migrated)
    if "  dlux-updater:\n" in migrated and "dlux_reconcile" not in migrated:
        migrated = migrated.replace(
            "python manage.py migrator && exec python manage.py dlux_update_worker",
            "python manage.py dlux_reconcile; python manage.py migrator "
            "&& exec python manage.py dlux_update_worker",
        )
    return migrated


def migrate_dlux_updater(
    project_dir: str = ".",
    *,
    compose_file: str = "",
    apply: bool = False,
    allow_unverified_dlux: bool = False,
    include_diff: bool = False,
    command_runner=subprocess.run,
) -> Dict[str, Any]:
    """Migrate local tools runtime references to the packaged DjangoLux runtime.

    Pure, deployment-safe file transform (idempotent) plus the shared
    validate/backup/write. It is the CALLER's job to confirm the project image
    actually ships the packaged runtime before applying — the compose on a pulled
    deployment has no requirements.txt to read, so the image itself (via ``dlux
    --version``) is the only authoritative signal.
    """
    return _apply_stack_migration(
        project_dir,
        compose_file=compose_file,
        transform=_migrate_dlux_updater_command,
        redeploy_command="docker compose up -d --force-recreate dlux-updater",
        apply=apply,
        allow_unverified_dlux=allow_unverified_dlux,
        include_diff=include_diff,
        command_runner=command_runner,
    )


def normalize_restart_labels(
    project_dir: str = ".",
    *,
    compose_file: str = "",
    apply: bool = False,
    allow_unverified_dlux: bool = False,
    include_diff: bool = False,
    command_runner=subprocess.run,
) -> Dict[str, Any]:
    """Install missing org.dlux.restart labels on generated stack services."""
    return _apply_stack_migration(
        project_dir,
        compose_file=compose_file,
        transform=_ensure_restart_labels,
        redeploy_command="docker compose up -d",
        apply=apply,
        allow_unverified_dlux=allow_unverified_dlux,
        include_diff=include_diff,
        command_runner=command_runner,
    )


def run_enable_agent(args) -> int:
    try:
        result = enable_agent(
            args.project_dir,
            compose_file=args.file or "",
            apply=args.apply,
            allow_unverified_dlux=args.allow_unverified_dlux,
            include_diff=not args.json and not args.apply,
        )
    except AgentInstallError as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, sort_keys=True))
        else:
            print(f"✖ agent enable: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, sort_keys=True))
        return 0
    mode = "Applied" if result["applied"] else "Dry run"
    files = ", ".join(result["files"]) if result["files"] else "no changes"
    print(f"{mode}: {files}")
    for warning in result["warnings"]:
        print(f"⚠ {warning}")
    if result.get("diff"):
        print(result["diff"], end="" if result["diff"].endswith("\n") else "\n")
    if result["backup_root"]:
        print(f"Preserved originals: {result['backup_root']}")
    if result["command"]:
        print(f"Redeploy once: {result['command']}")
    else:
        print("Agent topology is already enabled.")
    return 0


def run_enable_executor(args) -> int:
    try:
        result = enable_executor(
            args.project_dir,
            compose_file=args.file or "",
            apply=args.apply,
            allow_unverified_dlux=args.allow_unverified_dlux,
            include_diff=not args.json and not args.apply,
        )
    except AgentInstallError as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, sort_keys=True))
        else:
            print(f"✖ executor enable: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, sort_keys=True))
        return 0
    mode = "Applied" if result["applied"] else "Dry run"
    files = ", ".join(result["files"]) if result["files"] else "no changes"
    print(f"{mode}: {files}")
    for warning in result["warnings"]:
        print(f"⚠ {warning}")
    if result.get("diff"):
        print(result["diff"], end="" if result["diff"].endswith("\n") else "\n")
    if result["backup_root"]:
        print(f"Preserved originals: {result['backup_root']}")
    if result["command"]:
        print(f"Redeploy once: {result['command']}")
    else:
        print("Hardened executor topology is already enabled.")
    return 0
