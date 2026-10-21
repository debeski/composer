"""Move a stack's maintenance page from the project to Composer (stack schema 3).

Before Composer 1.7 the proxy served ``.proxy/maintenance.html``, a project file
bind-mounted into the proxy container. Now ``composer-agent`` publishes the page
to the runtime volume at ``proxy/maintenance/`` (see ``maintenance_page``), and
the proxy serves it from the runtime mount it already has, plus its CSS/JS under
``/_dlux/maintenance/`` so they load while ``web`` is down.

``migrate`` rewrites the proxy config and drops the bind mount, keeps a
customised page as the project override in ``.proxy/maintenance/``, validates the
new proxy config before touching anything, publishes the page through the agent
and recreates the proxy. Originals are archived under ``.xclude/composer-check/``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from . import maintenance_page
from .stack_schema import STAMPED_FILES, read_stamp, restamp
from .stack_cleanup import (
    StackCleanupError,
    _active_proxy_mounts,
    _archive_root,
    _compose_args,
    _compose_model,
    _project_file,
    _run,
    _running_services,
    _validate_proxy_candidates,
)

CADDYFILE = Path(".proxy") / "Caddyfile"
NGINX_TEMPLATE = Path(".proxy") / "default.conf.template"
LEGACY_PAGE = Path(".proxy") / "maintenance.html"

RUNTIME_PAGE_DIR = "/opt/dlux-runtime/proxy/maintenance"
# The page move is the only change in stack schema 3, so a migrated stack that was
# stamped 2 is fully schema 3 and is restamped; an unstamped stack stays unstamped.
LEGACY_SCHEMA = 2
TARGET_SCHEMA = 3
_LEGACY_MOUNT = re.compile(r"^[ \t]*#?[ \t]*-[ \t]*\./\.proxy/maintenance\.html:[^\n]*\n", re.M)
_CADDY_LEGACY_ROOT = "root * /usr/share/caddy/maintenance"
_CADDY_LEGACY_REWRITE = "rewrite * /maintenance.html"
_CADDY_LOG_HANDLE = re.compile(r"^([ \t]*)handle /_update/log\.txt \{\n(?:.*\n)*?\1\}\n", re.M)
_NGINX_LEGACY_LOCATION = re.compile(
    r"location = /dlux-maintenance\.html \{\n[ \t]*root /usr/share/nginx/html;\n", re.M
)


def caddy_asset_block(indent: str) -> str:
    """The asset route; DjangoLux's scaffold Caddyfile carries the same block."""
    lines = [
        "# Maintenance page assets, published by composer-agent on the runtime",
        "# volume and served here so they load while web is down.",
        "# (nginx: location ^~ /_dlux/maintenance/ { alias /opt/dlux-runtime/proxy/maintenance/; })",
        "handle /_dlux/maintenance/* {",
        "\troot * /opt/dlux-runtime/proxy",
        "\turi strip_prefix /_dlux",
        '\theader Cache-Control "no-store"',
        "\tfile_server",
        "}",
    ]
    return "".join(f"{indent}{line}\n" for line in lines)


NGINX_ASSET_LOCATION = (
    "    location ^~ /_dlux/maintenance/ {\n"
    f"        alias {RUNTIME_PAGE_DIR}/;\n"
    '        add_header Cache-Control "no-store";\n'
    "    }\n\n"
)


def is_legacy_compose(contents: str) -> bool:
    return bool(_LEGACY_MOUNT.search(contents))


def is_legacy_caddyfile(contents: str) -> bool:
    return _CADDY_LEGACY_ROOT in contents


def is_legacy_nginx(contents: str) -> bool:
    return bool(_NGINX_LEGACY_LOCATION.search(contents))


def migrate_compose(contents: str) -> str:
    return _LEGACY_MOUNT.sub("", contents)


def migrate_caddyfile(contents: str) -> str:
    match = _CADDY_LOG_HANDLE.search(contents)
    if not match:
        raise StackCleanupError(
            "The Caddyfile has no `handle /_update/log.txt` block to place the maintenance asset route after; "
            "edit it by hand (see DjangoLux's scaffold Caddyfile)."
        )
    updated = (
        contents[: match.end()] + "\n" + caddy_asset_block(match.group(1)) + contents[match.end():]
    )
    updated = updated.replace(_CADDY_LEGACY_ROOT, f"root * {RUNTIME_PAGE_DIR}", 1)
    return updated.replace(_CADDY_LEGACY_REWRITE, "rewrite * /index.html", 1)


def migrate_nginx(contents: str) -> str:
    updated = _NGINX_LEGACY_LOCATION.sub(
        f"location = /dlux-maintenance.html {{\n        alias {RUNTIME_PAGE_DIR}/index.html;\n", contents, count=1
    )
    marker = "    location = /_update/status.json {"
    if marker in updated and "/_dlux/maintenance/" not in updated:
        updated = updated.replace(marker, NGINX_ASSET_LOCATION + marker, 1)
    return updated


def publish_through_agent(
    project_root: Path,
    compose_files: Sequence[Path],
    environment: Mapping[str, str] | None,
    command_runner=subprocess.run,
    agent_service: str = "composer-agent",
) -> bool:
    """Publish the page inside the agent: the project override when one exists.

    Returns True when the project override was published.
    """
    override = project_root / maintenance_page.OVERRIDE
    publish = ["exec", "-T", agent_service, "python", "-m", "composer", "maintenance", "publish"]
    if override.is_dir():
        copy = _run(
            command_runner,
            _compose_args(project_root, compose_files, "cp", f"{override}/.", f"{agent_service}:/tmp/dlux-maintenance-override"),
            project_root,
            environment,
        )
        if copy.returncode != 0:
            raise StackCleanupError(f"Could not copy {maintenance_page.OVERRIDE} to {agent_service}.")
        publish += ["--override", "/tmp/dlux-maintenance-override"]
    published = _run(command_runner, _compose_args(project_root, compose_files, *publish), project_root, environment)
    if published.returncode != 0:
        detail = str(published.stderr or published.stdout or "").strip()[:500]
        raise StackCleanupError(
            f"{agent_service} could not publish the maintenance page (it needs Composer 1.7+; run "
            f"'composer agent update' first): {detail}"
        )
    return override.is_dir()


def inspect(project_root: Path, compose_files: Sequence[str]) -> Dict[str, Any]:
    """Which project files still serve the page from the project."""
    legacy: Dict[str, str] = {}
    for value in compose_files:
        path = project_root / value
        if path.is_file() and is_legacy_compose(path.read_text(encoding="utf-8")):
            legacy[value] = "compose"
    caddyfile = project_root / CADDYFILE
    if caddyfile.is_file() and is_legacy_caddyfile(caddyfile.read_text(encoding="utf-8")):
        legacy[str(CADDYFILE)] = "caddy"
    nginx = project_root / NGINX_TEMPLATE
    if nginx.is_file() and is_legacy_nginx(nginx.read_text(encoding="utf-8")):
        legacy[str(NGINX_TEMPLATE)] = "nginx"
    return {"legacy": bool(legacy), "files": legacy}


def migrate(
    project_dir: str,
    compose_files: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
    command_runner=subprocess.run,
    agent_service: str = "composer-agent",
) -> Dict[str, Any]:
    project_root = Path(project_dir).resolve()
    sources = [_project_file(project_root, value) for value in compose_files]
    updates: Dict[Path, str] = {}
    for source in sources:
        text = source.read_text(encoding="utf-8")
        if is_legacy_compose(text):
            updates[source] = migrate_compose(text)
    caddyfile = project_root / CADDYFILE
    if caddyfile.is_file() and is_legacy_caddyfile(caddyfile.read_text(encoding="utf-8")):
        updates[caddyfile.resolve()] = migrate_caddyfile(caddyfile.read_text(encoding="utf-8"))
    nginx = project_root / NGINX_TEMPLATE
    if nginx.is_file() and is_legacy_nginx(nginx.read_text(encoding="utf-8")):
        updates[nginx.resolve()] = migrate_nginx(nginx.read_text(encoding="utf-8"))
    if updates:
        stamped = [*sources, *(project_root / relative for relative in STAMPED_FILES)]
        for path in stamped:
            path = path.resolve()
            if path.name == LEGACY_PAGE.name or not path.is_file():
                continue
            text = updates.get(path, path.read_text(encoding="utf-8"))
            if read_stamp(text) == LEGACY_SCHEMA:
                updates[path] = restamp(text, TARGET_SCHEMA)

    result: Dict[str, Any] = {
        "applied": False,
        "files": sorted(str(path.relative_to(project_root)) for path in updates),
        "override": "",
        "backup_root": "",
        "recreated": [],
    }
    if not updates:
        return result
    if not shutil.which("docker"):
        raise StackCleanupError("Docker is required to validate the migrated proxy configuration.")

    model = _compose_model(command_runner, project_root, sources, environment, "Original configuration")
    proxy_updates = {path: text for path, text in updates.items() if path not in sources}
    mounts = _active_proxy_mounts(model, project_root, proxy_updates)
    staged: Dict[Path, Path] = {}
    archive = _archive_root(project_root)
    result["backup_root"] = str(archive)
    try:
        for path, text in proxy_updates.items():
            candidate = path.with_name(f".{path.name}.composer-maintenance.tmp")
            candidate.write_text(text, encoding="utf-8", newline="\n")
            staged[path] = candidate
        _validate_proxy_candidates(command_runner, project_root, sources, environment, mounts, staged)
    finally:
        for candidate in staged.values():
            if candidate.exists():
                candidate.unlink()

    for path in updates:
        destination = archive / "maintenance-migration" / path.relative_to(project_root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    for path, text in updates.items():
        with path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)

    page = project_root / LEGACY_PAGE
    override = project_root / maintenance_page.OVERRIDE
    if page.is_file():
        state = maintenance_page.inspect(project_root)["state"]
        if state in (maintenance_page.CUSTOM, maintenance_page.CUSTOM_STUCK) and not override.exists():
            override.mkdir(parents=True)
            shutil.copy2(page, override / "index.html")
            result["override"] = str(maintenance_page.OVERRIDE / "index.html")
        destination = archive / "maintenance-migration" / LEGACY_PAGE
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(page), destination)

    try:
        publish_through_agent(project_root, sources, environment, command_runner, agent_service)
    except StackCleanupError as exc:
        raise StackCleanupError(f"{exc} Proxy files were migrated; originals are in {archive}.") from exc

    running = _running_services(command_runner, project_root, sources, environment)
    services = model.get("services") if isinstance(model.get("services"), dict) else {}
    for service in ("caddy", "nginx"):
        if service not in running or service not in services:
            continue
        recreate = _run(
            command_runner,
            _compose_args(project_root, sources, "up", "-d", "--no-deps", "--force-recreate", service),
            project_root,
            environment,
        )
        if recreate.returncode != 0:
            detail = str(recreate.stderr or recreate.stdout or "").strip()[:500]
            raise StackCleanupError(f"Could not recreate {service}; restore from {archive}: {detail}")
        result["recreated"].append(service)
    result["applied"] = True
    return result
