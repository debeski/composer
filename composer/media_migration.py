"""Make a stack's stored files private (DjangoLux stack schema 4, security).

Through DjangoLux stack schema 3 the generated proxy served every ``/media/``
file straight from disk, before Django, so anyone who could reach the site could
download a stored file by its URL without signing in. DjangoLux 1.15.3 adds
``/sys/files/media-access/``; the schema 4 proxy serves only the public folders
directly and asks that endpoint before sending any other file.

``inspect`` sorts each proxy file into ``legacy`` (the generated public block,
which ``migrate`` rewrites), ``private`` (already asks the app) or ``custom``
(its own ``/media/`` rules, reported but never rewritten). ``migrate`` validates
the rewritten config before touching anything, restamps schema 3 files to 4,
archives the originals under ``.xclude/composer-check/`` and recreates the
proxy. Callers run it only once the running DjangoLux has the endpoint
(``MEDIA_ACCESS_FLOOR``); before that every private file would be refused.
"""

from __future__ import annotations

import difflib
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

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

#: The first DjangoLux that answers ``/sys/files/media-access/``.
MEDIA_ACCESS_FLOOR = (1, 15, 3)
ACCESS_URI = "/sys/files/media-access/"
LEGACY_SCHEMA = 3
TARGET_SCHEMA = 4

LEGACY, PRIVATE, CUSTOM = "legacy", "private", "custom"

#: Must match DjangoLux's ``dlux.files.media.PUBLIC_MEDIA`` and its scaffold proxies.
PUBLIC_MEDIA = ("public/", "dlux/branding/", "dlux/assets/image/", "dlux/assets/font/")

_CADDY_LEGACY = re.compile(
    r"^([ \t]*)(?:# Media files\.[^\n]*\n\1)?handle_path /media/\* \{\n"
    r"[ \t]*root \* /app/media\n[ \t]*file_server\n\1\}\n",
    re.M,
)
_NGINX_LEGACY = re.compile(r"^([ \t]*)location /media/ \{\n[ \t]*alias /app/media/;\n\1\}\n", re.M)
_CADDY_UPSTREAM = re.compile(r"^[ \t]*reverse_proxy[ \t]+(\S+)[ \t]*$", re.M)


def _indent(lines: Sequence[str], indent: str) -> str:
    return "".join(f"{indent}{line}\n" if line else "\n" for line in lines)


def caddy_media_blocks(indent: str, upstream: str = "web:8000") -> str:
    """The schema 4 ``/media/`` routes; DjangoLux's scaffold Caddyfile carries the same."""
    public = " ".join(f"/media/{prefix}*" for prefix in PUBLIC_MEDIA)
    lines = [
        "# Public media: branding, managed images and fonts, and the project's",
        "# public/ folder, served to anyone. Keep in step with",
        "# dlux.files.media.PUBLIC_MEDIA.",
        "# (nginx: location ^~ /media/public/ … { root /app; })",
        f"@public_media path {public}",
        "handle @public_media {",
        "\turi strip_prefix /media",
        "\troot * /app/media",
        "\tfile_server",
        "}",
        "",
        "# Every other stored file is private: the app decides who gets it",
        "# (signed in, and a registered document file only to users who may view",
        "# its document) before the file server sends a byte. Projects add public",
        "# folders with DLUX_PUBLIC_MEDIA and app-only ones with DLUX_APP_ONLY_MEDIA.",
        "# (nginx: location /media/ { auth_request /_dlux_media_access; root /app; })",
        "handle /media/* {",
        "\troute {",
        f"\t\tforward_auth {upstream} {{",
        f"\t\t\turi {ACCESS_URI}",
        "\t\t}",
        "\t\turi strip_prefix /media",
        "\t\troot * /app/media",
        '\t\theader Cache-Control "private, no-cache"',
        "\t\tfile_server",
        "\t}",
        "}",
    ]
    return _indent(lines, indent)


def nginx_media_blocks(indent: str, upstream: str = "$web_upstream") -> str:
    """The schema 4 ``/media/`` locations; DjangoLux's scaffold nginx template carries the same."""
    lines = [
        "# Public media (branding, managed images and fonts, the project's public/",
        "# folder). Keep in step with dlux.files.media.PUBLIC_MEDIA.",
        *(f"location ^~ /media/{prefix} {{ root /app; }}" for prefix in PUBLIC_MEDIA),
        "",
        "# Every other stored file is private: the app decides who gets it before",
        "# nginx sends it (403 when refused).",
        "location /media/ {",
        "    auth_request /_dlux_media_access;",
        "    root /app;",
        '    add_header Cache-Control "private, no-cache";',
        "}",
        "",
        "location = /_dlux_media_access {",
        "    internal;",
        f"    proxy_pass http://{upstream}{ACCESS_URI};",
        "    proxy_pass_request_body off;",
        '    proxy_set_header Content-Length "";',
        "    proxy_set_header Host $host;",
        "    # Always set here, so a client cannot supply its own.",
        "    proxy_set_header X-Forwarded-Uri $request_uri;",
        "    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
        "    proxy_set_header X-Forwarded-Proto $forwarded_proto;",
        "}",
    ]
    return _indent(lines, indent)


def _media_rules(contents: str) -> list:
    """Non-comment lines naming a ``/media/`` path other than the backups block."""
    return [
        line for line in contents.splitlines()
        if "/media/" in line and not line.lstrip().startswith("#") and "/media/dlux_backups/" not in line
    ]


def classify(contents: str, kind: str) -> str:
    """``legacy``, ``private``, ``custom`` or ``""`` (no ``/media/`` route at all).

    ``legacy`` is only the generated block on its own. A file that adds its own
    ``/media/`` rules (a project that already refuses its private folders, or
    keeps uploads public on purpose) is ``custom``: rewriting it could break
    what it serves, so it is reported, never rewritten.
    """
    if ACCESS_URI in contents:
        return PRIVATE
    pattern = _CADDY_LEGACY if kind == "caddy" else _NGINX_LEGACY
    match = pattern.search(contents)
    if match and not _media_rules(contents[: match.start()] + contents[match.end():]):
        return LEGACY
    return CUSTOM if _media_rules(contents) else ""


def migrate_caddyfile(contents: str) -> str:
    match = _CADDY_LEGACY.search(contents)
    if not match:
        raise StackCleanupError("The Caddyfile has no generated `handle_path /media/*` block to replace.")
    upstreams = _CADDY_UPSTREAM.findall(contents)
    upstream = upstreams[-1] if upstreams else "web:8000"
    return contents[: match.start()] + caddy_media_blocks(match.group(1), upstream) + contents[match.end():]


def migrate_nginx(contents: str) -> str:
    match = _NGINX_LEGACY.search(contents)
    if not match:
        raise StackCleanupError("The nginx template has no generated `location /media/` block to replace.")
    if "$forwarded_proto" not in contents:
        raise StackCleanupError(
            "The nginx template predates `$forwarded_proto`; apply DjangoLux's stack schema 4 "
            "nginx template by hand."
        )
    upstream = "$web_upstream" if "set $web_upstream" in contents else "web:8000"
    return contents[: match.start()] + nginx_media_blocks(match.group(1), upstream) + contents[match.end():]


def _proxy_files(project_root: Path):
    for relative, kind in ((CADDYFILE, "caddy"), (NGINX_TEMPLATE, "nginx")):
        path = project_root / relative
        if path.is_file():
            yield relative, path, kind


def inspect(project_root: Path) -> Dict[str, Any]:
    """Each proxy file's ``/media/`` state; ``exposed`` when any still serves it publicly."""
    files = {
        str(relative): state
        for relative, path, kind in _proxy_files(project_root)
        if (state := classify(path.read_text(encoding="utf-8"), kind))
    }
    return {
        "exposed": LEGACY in files.values(),
        "custom": sorted(name for name, state in files.items() if state == CUSTOM),
        "files": files,
    }


def _plan(project_root: Path, compose_files: Sequence[str]):
    sources = [_project_file(project_root, value) for value in compose_files]
    updates: Dict[Path, str] = {}
    for _relative, path, kind in _proxy_files(project_root):
        text = path.read_text(encoding="utf-8")
        if classify(text, kind) == LEGACY:
            updates[path.resolve()] = migrate_caddyfile(text) if kind == "caddy" else migrate_nginx(text)
    if updates:
        for path in (*sources, *(project_root / relative for relative in STAMPED_FILES)):
            path = path.resolve()
            if not path.is_file():
                continue
            text = updates.get(path, path.read_text(encoding="utf-8"))
            if read_stamp(text) == LEGACY_SCHEMA:
                updates[path] = restamp(text, TARGET_SCHEMA)
    return sources, updates


def preview(project_dir: str, compose_files: Sequence[str]) -> Dict[str, Any]:
    """What ``migrate`` would change, as unified diffs. Writes nothing."""
    project_root = Path(project_dir).resolve()
    _sources, updates = _plan(project_root, compose_files)
    if not updates:
        return {"files": [], "diff": "", "note": ""}
    diff = "".join(
        "".join(difflib.unified_diff(
            path.read_text(encoding="utf-8").splitlines(keepends=True),
            updates[path].splitlines(keepends=True),
            fromfile=f"a/{name}", tofile=f"b/{name}",
        ))
        for name, path in sorted((str(path.relative_to(project_root)), path) for path in updates)
    )
    return {
        "files": sorted(str(path.relative_to(project_root)) for path in updates),
        "diff": diff,
        "note": "The proxy config is validated first; the proxy is then recreated so it reads it.",
    }


def migrate(
    project_dir: str,
    compose_files: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
    command_runner=subprocess.run,
) -> Dict[str, Any]:
    project_root = Path(project_dir).resolve()
    sources, updates = _plan(project_root, compose_files)
    result: Dict[str, Any] = {
        "applied": False,
        "files": sorted(str(path.relative_to(project_root)) for path in updates),
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
    try:
        for path, text in proxy_updates.items():
            candidate = path.with_name(f".{path.name}.composer-media.tmp")
            candidate.write_text(text, encoding="utf-8", newline="\n")
            staged[path] = candidate
        _validate_proxy_candidates(command_runner, project_root, sources, environment, mounts, staged)
    finally:
        for candidate in staged.values():
            if candidate.exists():
                candidate.unlink()

    archive = _archive_root(project_root)
    result["backup_root"] = str(archive)
    for path in updates:
        destination = archive / "media-migration" / path.relative_to(project_root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    for path, text in updates.items():
        with path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)

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
