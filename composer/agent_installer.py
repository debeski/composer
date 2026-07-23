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


COMPOSER_UPDATER_START = "  # Composer-as-updater start"
COMPOSER_UPDATER_END = "  # Composer-as-updater end"
COMPOSER_AGENT_START = "  # DjangoLux Composer agent start"
COMPOSER_AGENT_END = "  # DjangoLux Composer agent end"
MINIMUM_DLUX_VERSION = (1, 5, 0)
SAFE_RESTART_CANDIDATES = ("web", "celery", "smtp-relay", "caddy", "nginx")
PROTECTED_SERVICE_NAMES = (
    "db",
    "database",
    "postgres",
    "postgresql",
    "redis",
    "backup",
    "db-backup",
    "pgadmin",
)


class AgentInstallError(RuntimeError):
    pass


def _service_names(contents: str) -> set[str]:
    match = re.search(r"(?ms)^services:\s*\n(.*?)(?=^volumes:\s*$|^networks:\s*$|\Z)", contents)
    if not match:
        raise AgentInstallError("Could not find a standard top-level Compose services block.")
    return set(re.findall(r"(?m)^  ([A-Za-z0-9_-]+):\s*$", match.group(1)))


def _agent_stack(project_slug: str, services: set[str]) -> str:
    image = project_slug.lower()
    restart_services = [name for name in SAFE_RESTART_CANDIDATES if name in services]
    excluded_services = ["composer-agent", "docker-socket-proxy"]
    excluded_services.extend(name for name in PROTECTED_SERVICE_NAMES if name in services)
    restart_value = ",".join(restart_services)
    exclusion_value = ",".join(excluded_services)
    return f'''{COMPOSER_AGENT_START}
  docker-socket-proxy:
    image: tecnativa/docker-socket-proxy:latest
    restart: always
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
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks:
      - {project_slug}_docker_proxy

  composer-agent:
    image: debeski/composer:latest
    restart: unless-stopped
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    working_dir: "${{PWD}}"
    command:
      - agent
      - --trigger-file
      - /opt/dlux-runtime/state/image-update-request.json
      - --status-file
      - /opt/dlux-runtime/state/deploy-status.json
      - --bridge-dir
      - /opt/dlux-runtime/state/agent
      - --interval
      - "2"
      - --check-image
      - ${{WEB_IMAGE:-{image}:latest}}
      - --availability-file
      - /opt/dlux-runtime/state/image-available.json
      - --check-interval
      - "3600"
    environment:
      DOCKER_HOST: "tcp://docker-socket-proxy:2375"
      WEB_IMAGE: "${{WEB_IMAGE:-{image}:latest}}"
      COMPOSER_CONTROL_URL: "${{COMPOSER_CONTROL_URL:-}}"
      COMPOSER_ENROLLMENT_TOKEN: "${{COMPOSER_ENROLLMENT_TOKEN:-}}"
      COMPOSER_AGENT_STATE_DIR: "/var/lib/composer-agent"
      COMPOSER_VERSION_LABEL: "org.{project_slug}.dlux_baked_version"
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
        condition: service_started
    networks:
      - dlux_update_egress
      - {project_slug}_docker_proxy
  {COMPOSER_AGENT_END}'''


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
        return contents
    if contents.count(COMPOSER_UPDATER_START) != 1 or contents.count(COMPOSER_UPDATER_END) != 1:
        raise AgentInstallError("No single recognized generated composer-updater block was found.")
    services = _service_names(contents)
    if "composer-updater" not in services or "docker-socket-proxy" not in services:
        raise AgentInstallError("The marked legacy updater block is not a recognized topology.")
    if "composer-agent" in services:
        raise AgentInstallError("An unmarked composer-agent service already exists.")
    start = contents.index(COMPOSER_UPDATER_START)
    end = contents.index(COMPOSER_UPDATER_END, start) + len(COMPOSER_UPDATER_END)
    updated = contents[:start] + _agent_stack(project_slug, services) + contents[end:]
    volume_anchor = re.compile(r"(?m)^  dlux_runtime:\s*$")
    if len(volume_anchor.findall(updated)) != 1:
        raise AgentInstallError("Expected one generated dlux_runtime volume anchor.")
    updated = volume_anchor.sub("  dlux_runtime:\n  composer_agent_state:", updated, count=1)
    return updated.replace(
        "  # Isolated path from composer-updater to the docker-socket-proxy only.",
        "  # Isolated path from composer-agent to the docker-socket-proxy only.",
        1,
    )


def _dlux_readiness_warning(project_root: Path) -> str:
    declarations = []
    for path in (project_root / "requirements.txt", project_root / "pyproject.toml"):
        try:
            declarations.append(path.read_text(encoding="utf-8"))
        except OSError:
            continue
    combined = "\n".join(declarations)
    matches = re.findall(
        r"django-lux(?:\[[^\]]+\])?\s*(?:==|>=|~=)\s*[\"']?(\d+)\.(\d+)(?:\.(\d+))?",
        combined,
        flags=re.IGNORECASE,
    )
    if matches:
        versions = [tuple(int(part or 0) for part in match) for match in matches]
        if max(versions) >= MINIMUM_DLUX_VERSION:
            return ""
        return "DjangoLux 1.5.0 or newer is required for the typed local agent bridge."
    if re.search(r"django-lux", combined, flags=re.IGNORECASE):
        return "Could not verify that the declared DjangoLux dependency is 1.5.0 or newer."
    return "Could not find a DjangoLux dependency declaration to verify the local agent bridge."


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


def enable_agent(
    project_dir: str = ".",
    *,
    compose_file: str = "",
    apply: bool = False,
    allow_unverified_dlux: bool = False,
    include_diff: bool = False,
    command_runner=subprocess.run,
) -> Dict[str, Any]:
    project_root = Path(project_dir).resolve()
    manage_path = project_root / "manage.py"
    try:
        manage_contents = manage_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AgentInstallError("The project directory does not contain manage.py.") from exc
    if "Generated with django-lux" not in manage_contents:
        raise AgentInstallError("enable-agent supports only recognized DjangoLux-generated projects.")

    selected_file = compose_file or (
        "compose.yml" if (project_root / "compose.yml").is_file() else "docker-compose.yml"
    )
    compose_path = (project_root / selected_file).resolve()
    if not compose_path.is_relative_to(project_root) or not compose_path.is_file():
        raise AgentInstallError("The selected Compose file must exist inside the project directory.")
    contents = compose_path.read_text(encoding="utf-8")
    name_match = re.search(r"(?m)^name:\s*([A-Za-z0-9_-]+)\s*$", contents)
    if not name_match:
        raise AgentInstallError("Could not determine the generated Compose project name.")
    updated = _transform_compose(contents, name_match.group(1))
    changed = [str(compose_path.relative_to(project_root))] if updated != contents else []
    warning = _dlux_readiness_warning(project_root) if changed else ""
    result: Dict[str, Any] = {
        "applied": False,
        "files": changed,
        "command": (
            "docker compose up -d --force-recreate docker-socket-proxy composer-agent"
            if changed
            else ""
        ),
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
    if warning and not allow_unverified_dlux:
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
        raise AgentInstallError("Docker Compose v2 is required to apply the agent bootstrap.")
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
            print(f"✖ enable-agent: {exc}", file=sys.stderr)
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
