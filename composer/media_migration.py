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

How a stored file is served matters as much as who gets it (break-it R2-001):
an uploaded page or SVG served inline runs its script as the site, with the
rights of whoever opens it. ``serving_gaps`` names the headers a proxy file
does not send on its ``/media/`` routes (``nosniff``, the sandboxing
``Content-Security-Policy``, ``Content-Disposition: attachment`` for active
types) and the maintenance page's ``Content-Security-Policy`` (R2-022).
``harden`` adds them to the generated shape in place, schema unchanged, and
``migrate`` applies it together with the schema 3 rewrite. Both need no
particular DjangoLux.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from .stack_schema import (
    STAMPED_FILES,
    ensure_writable,
    line_ending,
    preview_diff,
    restamp_secrets,
    restamp_step,
    secrets_file,
    secrets_note,
    write_updates,
)
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

#: The headers DjangoLux's scaffold proxies send on every ``/media/`` file.
MEDIA_CSP = "default-src 'none'; sandbox"
#: Types a browser shows inline without running them (previews, the PDF viewer).
INLINE_MEDIA = ("avif", "bmp", "gif", "ico", "jpeg", "jpg", "pdf", "png", "webp")
NOSNIFF, MEDIA_CSP_HEADER, DISPOSITION = "X-Content-Type-Options", "Content-Security-Policy", "Content-Disposition"
MEDIA_HEADERS = (NOSNIFF, MEDIA_CSP_HEADER, DISPOSITION)
#: The maintenance page and its assets run only their own files.
MAINTENANCE_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
)
MAINTENANCE_GAP = "the maintenance page's Content-Security-Policy"
CADDY_SNIPPET = "dlux_media_headers"

_CADDY_LEGACY = re.compile(
    r"^([ \t]*)(?:# Media files\.[^\n]*\n\1)?handle_path /media/\* \{\n"
    r"[ \t]*root \* /app/media\n[ \t]*file_server\n\1\}\n",
    re.M,
)
_NGINX_LEGACY = re.compile(r"^([ \t]*)location /media/ \{\n[ \t]*alias /app/media/;\n\1\}\n", re.M)
_CADDY_UPSTREAM = re.compile(r"^[ \t]*reverse_proxy[ \t]+(\S+)[ \t]*$", re.M)


def _indent(lines: Sequence[str], indent: str) -> str:
    return "".join(f"{indent}{line}\n" if line else "\n" for line in lines)


_NGINX_HEADER_LINES = (
    '    add_header X-Content-Type-Options "nosniff";',
    "    add_header Content-Security-Policy $dlux_media_csp;",
    "    add_header Content-Disposition $dlux_media_disposition;",
)


def caddy_media_snippet() -> str:
    """The top-level ``dlux_media_headers`` snippet both ``/media/`` routes import; the scaffold carries the same."""
    inline = " ".join(f"*.{suffix}" for suffix in INLINE_MEDIA)
    return "\n".join([
        "# How every stored file under /media/ is served, public or private: the",
        "# browser never sniffs a type (nosniff), and a file it would run as a page",
        "# (HTML, SVG, XML, JavaScript, anything not listed below) opens as a download",
        "# in a sandbox with no script and no requests (Content-Security-Policy), so an",
        "# upload cannot act on this site with the rights of whoever opens it. Raster",
        "# images and PDF stay inline for previews and the PDF viewer; a PDF has no",
        "# sandbox, which would stop the browser's PDF viewer. <img>, fetch() and",
        "# @font-face loads ignore Content-Disposition. Composer's stored-files check",
        f"# looks for this snippet by name: keep {CADDY_SNIPPET}.",
        "# (nginx: the $dlux_media_csp and $dlux_media_disposition maps)",
        f"({CADDY_SNIPPET}) {{",
        "\t@dlux_media_sandboxed not path *.pdf",
        f"\t@dlux_media_download not path {inline}",
        '\theader X-Content-Type-Options "nosniff"',
        f'\theader @dlux_media_sandboxed Content-Security-Policy "{MEDIA_CSP}"',
        '\theader @dlux_media_download Content-Disposition "attachment"',
        "}",
        "",
    ])


def nginx_media_maps() -> str:
    """The http-level maps behind the nginx ``/media/`` headers; the scaffold carries the same."""
    inline = "|".join(sorted({"jpe?g" if suffix in ("jpg", "jpeg") else suffix for suffix in INLINE_MEDIA}))
    return "\n".join([
        "# How every stored file under /media/ is served, public or private, the nginx",
        "# form of the Caddyfile's dlux_media_headers snippet: the browser never sniffs",
        "# a type (nosniff), and a file it would run as a page (HTML, SVG, XML,",
        "# JavaScript, anything not listed below) opens as a download in a sandbox with",
        "# no script and no requests (Content-Security-Policy), so an upload cannot act",
        "# on this site with the rights of whoever opens it. Raster images and PDF stay",
        "# inline for previews and the PDF viewer; a PDF has no sandbox, which would",
        "# stop the browser's PDF viewer. An empty value sends no header. Composer's",
        "# stored-files check looks for these maps by name: keep them.",
        "map $uri $dlux_media_csp {",
        f'    default     "{MEDIA_CSP}";',
        '    "~*\\.pdf$"  "";',
        "}",
        "",
        "map $uri $dlux_media_disposition {",
        '    default                                         "attachment";',
        f'    "~*\\.(?:{inline})$"  "";',
        "}",
        "",
    ])


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
        f"\timport {CADDY_SNIPPET}",
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
        f"\t\timport {CADDY_SNIPPET}",
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
        *(line for prefix in PUBLIC_MEDIA
          for line in (f"location ^~ /media/{prefix} {{", "    root /app;", *_NGINX_HEADER_LINES, "}")),
        "",
        "# Every other stored file is private: the app decides who gets it before",
        "# nginx sends it (403 when refused).",
        "location /media/ {",
        "    auth_request /_dlux_media_access;",
        "    root /app;",
        '    add_header Cache-Control "private, no-cache";',
        *_NGINX_HEADER_LINES,
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


def _block(text: str, opening: "re.Pattern[str]", start: int = 0):
    """``(start, end, indent)`` of the first brace block whose opening line matches, or None.

    ``end`` is the offset just past the line that closes it. Braces inside
    quotes or comments are not expected in the generated proxy files.
    """
    match = opening.search(text, start)
    if not match:
        return None
    depth, position = 0, match.start()
    line_start = text.rfind("\n", 0, match.start()) + 1
    indent = re.match(r"[ \t]*", text[line_start:]).group(0)
    while position < len(text):
        char = text[position]
        if char == "#":
            position = text.find("\n", position)
            if position < 0:
                return None
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = text.find("\n", position)
                return line_start, (len(text) if end < 0 else end + 1), indent
        position += 1
    return None


def _caddy_media_routes(text: str):
    """The public and private ``/media/`` handle blocks of a generated Caddyfile (either may be None)."""
    return (
        _block(text, re.compile(r"^[ \t]*handle @public_media \{[ \t]*$", re.M)),
        _block(text, re.compile(r"^[ \t]*handle /media/\* \{[ \t]*$", re.M)),
    )


def _caddy_maintenance_routes(text: str):
    """The maintenance asset route and the 502/503/504 page block (either may be None)."""
    errors = _block(text, re.compile(r"^[ \t]*handle_errors \{[ \t]*$", re.M))
    down = _block(text, re.compile(r"^[ \t]*handle @down \{[ \t]*$", re.M), errors[0]) if errors else None
    return _block(text, re.compile(r"^[ \t]*handle /_dlux/maintenance/\* \{[ \t]*$", re.M)), down


def _nginx_media_locations(text: str) -> list:
    """Every ``/media/`` location block except the backups refusal, in order."""
    found, start = [], 0
    pattern = re.compile(r"^[ \t]*location (?:\^~ )?/media/(?!dlux_backups/)\S* \{", re.M)
    while (block := _block(text, pattern, start)) is not None:
        found.append(block)
        start = block[1]
    return found


def _nginx_maintenance_locations(text: str):
    return (
        _block(text, re.compile(r"^[ \t]*location = /dlux-maintenance\.html \{", re.M)),
        _block(text, re.compile(r"^[ \t]*location \^~ /_dlux/maintenance/ \{", re.M)),
    )


def _caddy_snippet_headers(text: str) -> set:
    snippet = _block(text, re.compile(rf"^\({CADDY_SNIPPET}\) \{{[ \t]*$", re.M))
    if not snippet:
        return set()
    body = text[snippet[0]:snippet[1]]
    return {name for name in MEDIA_HEADERS if re.search(rf"^[ \t]*header\b[^\n]*\b{name}\b", body, re.M | re.I)}


def _headers_in(body: str, kind: str) -> set:
    directive = r"header" if kind == "caddy" else r"add_header"
    return {name for name in MEDIA_HEADERS if re.search(rf"^[ \t]*{directive}\b[^\n]*\b{name}\b", body, re.M | re.I)}


def serving_gaps(contents: str, kind: str) -> list:
    """What a private-media proxy file does not send: header names on ``/media/``, then the maintenance CSP.

    A header counts on a route when the route sends it itself or (Caddy) imports
    the ``dlux_media_headers`` snippet that sends it. Only meaningful for a
    file `classify` calls ``private``; a maintenance route the file lacks (a
    legacy maintenance page) is not reported here.
    """
    gaps = []
    if kind == "caddy":
        routes = [block for block in _caddy_media_routes(contents) if block]
        snippet = _caddy_snippet_headers(contents)
        missing = set()
        for start, end, _indent in routes:
            body = contents[start:end]
            sent = _headers_in(body, kind) | (snippet if re.search(rf"^[ \t]*import {CADDY_SNIPPET}[ \t]*$", body, re.M) else set())
            missing |= set(MEDIA_HEADERS) - sent
        maintenance = [block for block in _caddy_maintenance_routes(contents) if block]
    else:
        routes = _nginx_media_locations(contents)
        missing = set()
        for start, end, _indent in routes:
            missing |= set(MEDIA_HEADERS) - _headers_in(contents[start:end], kind)
        if "$dlux_media_csp" in contents and "map $uri $dlux_media_csp" not in contents:
            missing.add(MEDIA_CSP_HEADER)
        if "$dlux_media_disposition" in contents and "map $uri $dlux_media_disposition" not in contents:
            missing.add(DISPOSITION)
        maintenance = [block for block in _nginx_maintenance_locations(contents) if block]
    gaps.extend(name for name in MEDIA_HEADERS if name in missing)
    if any(not re.search(r"\bContent-Security-Policy\b", contents[start:end], re.I) for start, end, _ in maintenance):
        gaps.append(MAINTENANCE_GAP)
    return gaps


def _insert_before_closing_file_server(text: str, block, line: str):
    """``text`` with ``line`` added just above the block's own ``file_server`` line, or None when it has none."""
    start, end, _indent = block
    body = text[start:end]
    match = re.search(r"^([ \t]*)file_server\b", body, re.M)
    if not match:
        return None
    position = start + match.start()
    return text[:position] + f"{match.group(1)}{line}\n" + text[position:]


def _harden_caddyfile(text: str) -> str:
    if not _caddy_snippet_headers(text) >= set(MEDIA_HEADERS):
        if re.search(rf"^\({CADDY_SNIPPET}\) \{{", text, re.M):
            raise StackCleanupError(
                f"The Caddyfile's ({CADDY_SNIPPET}) snippet is not DjangoLux's; compare it with a fresh "
                "'python -m dlux startproject' by hand."
            )
        site = re.search(r"^(?!\(|\{[ \t]*$)[^\s#][^\n]*\{[ \t]*$", text, re.M)
        if not site:
            raise StackCleanupError("The Caddyfile has no site block to place the media headers before.")
        text = text[: site.start()] + caddy_media_snippet() + "\n" + text[site.start():]
    for index in (0, 1):
        block = _caddy_media_routes(text)[index]
        if block is None:
            continue
        body = text[block[0]:block[1]]
        if re.search(rf"^[ \t]*import {CADDY_SNIPPET}[ \t]*$", body, re.M):
            continue
        if _headers_in(body, "caddy"):
            raise StackCleanupError("A Caddyfile /media/ route sets its own serving headers; add the rest by hand.")
        updated = _insert_before_closing_file_server(text, block, f"import {CADDY_SNIPPET}")
        if updated is None:
            raise StackCleanupError("A Caddyfile /media/ route has no file_server line to place the media headers before.")
        text = updated
    for index in (0, 1):
        block = _caddy_maintenance_routes(text)[index]
        if block is None or re.search(r"\bContent-Security-Policy\b", text[block[0]:block[1]], re.I):
            continue
        updated = _insert_before_closing_file_server(text, block, f'header Content-Security-Policy "{MAINTENANCE_CSP}"')
        if updated is None:
            raise StackCleanupError("A Caddyfile maintenance route has no file_server line to place its CSP before.")
        text = updated
    return text


def _close_line(text: str, block) -> int:
    """Offset of the line that closes ``block``."""
    return text.rfind("\n", block[0], block[1] - 1) + 1


def _harden_nginx(text: str) -> str:
    if "map $uri $dlux_media_csp" not in text or "map $uri $dlux_media_disposition" not in text:
        if "map $uri $dlux_media_csp" in text or "map $uri $dlux_media_disposition" in text:
            raise StackCleanupError("The nginx template has only one of the media header maps; add the other by hand.")
        server = re.search(r"^server[ \t]*\{[ \t]*$", text, re.M)
        if not server:
            raise StackCleanupError("The nginx template has no server block to place the media header maps before.")
        text = text[: server.start()] + nginx_media_maps() + "\n" + text[server.start():]
    start = 0
    pattern = re.compile(r"^[ \t]*location (?:\^~ )?/media/(?!dlux_backups/)\S* \{", re.M)
    while (block := _block(text, pattern, start)) is not None:
        body = text[block[0]:block[1]]
        present = _headers_in(body, "nginx")
        if present >= set(MEDIA_HEADERS):
            start = block[1]
            continue
        if present:
            raise StackCleanupError("An nginx /media/ location sets some serving headers itself; add the rest by hand.")
        indent = block[2]
        one_line = re.fullmatch(r"([ \t]*location [^\n{]*)\{[ \t]*root /app;[ \t]*\}[ \t]*\n", body)
        lines = "".join(f"{indent}{line}\n" for line in _NGINX_HEADER_LINES)
        if one_line:
            replacement = f"{one_line.group(1).rstrip()} {{\n{indent}    root /app;\n{lines}{indent}}}\n"
            text = text[:block[0]] + replacement + text[block[1]:]
            start = block[0] + len(replacement)
        else:
            close = _close_line(text, block)
            text = text[:close] + lines + text[close:]
            start = block[1] + len(lines)
    for index, suffix in ((0, " always"), (1, "")):
        block = _nginx_maintenance_locations(text)[index]
        if block is None or re.search(r"\bContent-Security-Policy\b", text[block[0]:block[1]], re.I):
            continue
        close = _close_line(text, block)
        text = text[:close] + f'{block[2]}    add_header Content-Security-Policy "{MAINTENANCE_CSP}"{suffix};\n' + text[close:]
    return text


def harden(contents: str, kind: str) -> str:
    """``contents`` with the serving headers ``serving_gaps`` reports added; unchanged when none are missing."""
    if not serving_gaps(contents, kind):
        return contents
    return _harden_caddyfile(contents) if kind == "caddy" else _harden_nginx(contents)


def _proxy_files(project_root: Path):
    for relative, kind in ((CADDYFILE, "caddy"), (NGINX_TEMPLATE, "nginx")):
        path = project_root / relative
        if path.is_file():
            yield relative, path, kind


def _hardenable(contents: str, kind: str) -> bool:
    try:
        harden(contents, kind)
    except StackCleanupError:
        return False
    return True


def inspect(project_root: Path) -> Dict[str, Any]:
    """Each proxy file's ``/media/`` state; ``exposed`` when any still serves it publicly.

    ``gaps`` maps each private file to what it does not send (`serving_gaps`);
    ``hardenable`` lists those `harden` can complete, the rest need a human.
    """
    files, gaps, hardenable = {}, {}, []
    for relative, path, kind in _proxy_files(project_root):
        contents = path.read_text(encoding="utf-8")
        state = classify(contents, kind)
        if not state:
            continue
        files[str(relative)] = state
        if state == PRIVATE and (missing := serving_gaps(contents, kind)):
            gaps[str(relative)] = missing
            if _hardenable(contents, kind):
                hardenable.append(str(relative))
    return {
        "exposed": LEGACY in files.values(),
        "custom": sorted(name for name, state in files.items() if state == CUSTOM),
        "files": files,
        "gaps": gaps,
        "hardenable": sorted(hardenable),
    }


def _plan(project_root: Path, compose_files: Sequence[str]):
    sources = [_project_file(project_root, value) for value in compose_files]
    updates: Dict[Path, str] = {}
    schema_rewrite = False
    for _relative, path, kind in _proxy_files(project_root):
        text = path.read_text(encoding="utf-8")
        state = classify(text, kind)
        if state == LEGACY:
            schema_rewrite = True
            text = migrate_caddyfile(text) if kind == "caddy" else migrate_nginx(text)
            updates[path.resolve()] = harden(text, kind)
        elif state == PRIVATE and serving_gaps(text, kind) and _hardenable(text, kind):
            updates[path.resolve()] = harden(text, kind)
    if schema_rewrite:
        for path in (*sources, *(project_root / relative for relative in STAMPED_FILES)):
            path = path.resolve()
            if not path.is_file():
                continue
            text = updates.get(path, path.read_text(encoding="utf-8"))
            restamped = restamp_step(text, LEGACY_SCHEMA, TARGET_SCHEMA)
            if restamped is not None:
                updates[path] = restamped
        restamp_secrets(project_root, updates, LEGACY_SCHEMA, TARGET_SCHEMA)
    return sources, updates


def preview(project_dir: str, compose_files: Sequence[str]) -> Dict[str, Any]:
    """What ``migrate`` would change, as unified diffs. Writes nothing."""
    project_root = Path(project_dir).resolve()
    _sources, updates = _plan(project_root, compose_files)
    if not updates:
        return {"files": [], "diff": "", "note": ""}
    secrets = secrets_note(project_root, updates, TARGET_SCHEMA)
    return {
        "files": sorted(str(path.relative_to(project_root)) for path in updates),
        "diff": preview_diff(project_root, updates),
        "note": " ".join(
            ["The proxy config is validated first; the proxy is then recreated so it reads it."]
            + ([secrets] if secrets else [])
        ),
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
    ensure_writable(project_root, updates)
    if not shutil.which("docker"):
        raise StackCleanupError("Docker is required to validate the migrated proxy configuration.")

    model = _compose_model(command_runner, project_root, sources, environment, "Original configuration")
    secrets = secrets_file(project_root)
    proxy_updates = {
        path: text for path, text in updates.items() if path not in sources and path != secrets
    }
    mounts = _active_proxy_mounts(model, project_root, proxy_updates)
    staged: Dict[Path, Path] = {}
    try:
        for path, text in proxy_updates.items():
            candidate = path.with_name(f".{path.name}.composer-media.tmp")
            candidate.write_text(text, encoding="utf-8", newline=line_ending(path))
            staged[path] = candidate
        _validate_proxy_candidates(command_runner, project_root, sources, environment, mounts, staged)
    finally:
        for candidate in staged.values():
            if candidate.exists():
                candidate.unlink()

    archive = _archive_root(project_root)
    result["backup_root"] = str(archive)
    write_updates(project_root, updates, archive / "media-migration")

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
