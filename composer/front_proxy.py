"""Let a stack choose the front proxies Caddy trusts (``DLUX_FRONT_PROXY``).

The generated Caddyfile keeps ``X-Forwarded-For`` from every private-range peer
(``trusted_proxies static private_ranges``). A deployment behind a front proxy
on a private address needs that to see real client addresses, but it also lets a
client on a private network that reaches Caddy directly choose the address
DjangoLux records (login lockout, audit log).

The managed form reads the range from the project's secrets file: the Caddyfile
line is ``trusted_proxies static {$DLUX_FRONT_PROXY:private_ranges}`` and compose
passes ``DLUX_FRONT_PROXY: "${DLUX_FRONT_PROXY-private_ranges}"`` to ``caddy``.
Unset, Caddy trusts every private range; empty, no proxy; otherwise only the
space-separated addresses and CIDR ranges listed (``private_ranges`` among them
if named).

``inspect`` reports the Caddyfile (``legacy``, ``managed``, ``custom``, ``none``,
``missing``), whether compose passes the variable (``passed``, ``absent``,
``custom``, ``no-service``) and the value set. ``migrate`` rewrites the legacy
line and adds the variable to the caddy service's environment, validates both
(``docker compose config``, then ``caddy validate`` with the new environment),
archives the originals under ``.xclude/composer-check/`` and recreates a running
``caddy``. A custom ``trusted_proxies`` line is never touched.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .stack_cleanup import (
    StackCleanupError,
    _active_proxy_mounts,
    _archive_root,
    _compose_args,
    _compose_model,
    _project_file,
    _run,
    _running_services,
    _stage_candidate,
    _validate_proxy_candidates,
)
from .stack_schema import ensure_writable, preview_diff, secrets_file, write_updates
from .worker_migration import WorkerMigrationError, _inline_value, _span, _split_block

VARIABLE = "DLUX_FRONT_PROXY"
PRIVATE_RANGES = "private_ranges"
SERVICE = "caddy"
CADDYFILE = Path(".proxy") / "Caddyfile"
DEFAULT_SECRETS = ".secrets/.env"
CADDY_RANGES = "{$DLUX_FRONT_PROXY:private_ranges}"
COMPOSE_VALUE = "${DLUX_FRONT_PROXY-private_ranges}"

LEGACY, MANAGED, CUSTOM, NONE, MISSING = "legacy", "managed", "custom", "none", "missing"
PASSED, ABSENT, NO_SERVICE = "passed", "absent", "no-service"

_DIRECTIVE = re.compile(r"^[ \t]*trusted_proxies(?:[ \t]|$)")
_LEGACY_LINE = re.compile(r"^([ \t]*)trusted_proxies[ \t]+static[ \t]+private_ranges((?:[ \t]+#.*)?[ \t]*)$")
_MANAGED_LINE = re.compile(
    r"^[ \t]*trusted_proxies[ \t]+static[ \t]+\{\$DLUX_FRONT_PROXY:private_ranges\}(?:[ \t]+#.*)?[ \t]*$"
)
_ENV_ENTRY = re.compile(
    r"""^[ \t]+(?:-[ \t]+)?["']?DLUX_FRONT_PROXY(?:["']?[ \t]*:(?:[ \t]+|$)|=|["']?[ \t]*$)(.*)$"""
)
_ENVIRONMENT_KEY = re.compile(r"^    environment:")
_PREFIX = re.compile(r"[0-9]{1,3}")


class FrontProxyError(StackCleanupError):
    """The stack is not in a shape this rewrite recognizes."""


def classify_caddyfile(contents: str) -> str:
    """``legacy``, ``managed``, ``custom`` or ``none`` (no ``trusted_proxies`` directive)."""
    lines = [line for line in contents.splitlines() if _DIRECTIVE.match(line)]
    if not lines:
        return NONE
    if any(not (_LEGACY_LINE.match(line) or _MANAGED_LINE.match(line)) for line in lines):
        return CUSTOM
    return LEGACY if any(_LEGACY_LINE.match(line) for line in lines) else MANAGED


def migrate_caddyfile(contents: str) -> str:
    """The Caddyfile with each generated ``trusted_proxies`` line reading ``DLUX_FRONT_PROXY``."""
    if classify_caddyfile(contents) not in (LEGACY, MANAGED):
        raise FrontProxyError("The Caddyfile has no generated `trusted_proxies static private_ranges` line.")
    rewritten = []
    for line in contents.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        match = _LEGACY_LINE.match(body)
        if match:
            line = f"{match.group(1)}trusted_proxies static {CADDY_RANGES}{match.group(2)}{line[len(body):]}"
        rewritten.append(line)
    return "".join(rewritten)


def _entry_value(raw: str) -> str:
    return raw.split(" #", 1)[0].strip().strip("'\"")


def compose_value(contents: str) -> Optional[str]:
    """The ``DLUX_FRONT_PROXY`` value the caddy service sets, or None when it sets none."""
    span = _span(contents, SERVICE)
    if span is None:
        return None
    values = [
        _entry_value(match.group(1))
        for line in contents[span[0]:span[1]].splitlines()
        if (match := _ENV_ENTRY.match(line))
    ]
    return values[-1] if values else None


def add_compose_variable(contents: str) -> str:
    """The compose file with ``DLUX_FRONT_PROXY`` in the caddy service's environment.

    A block mapping or list gains one entry in its own style and indentation; a
    service with no ``environment`` gets a mapping after ``ports``. Anything else
    (an alias, a flow mapping, an unusual layout) raises ``FrontProxyError``.
    """
    span = _span(contents, SERVICE)
    if span is None:
        raise FrontProxyError("No caddy service to pass DLUX_FRONT_PROXY to.")
    if compose_value(contents) is not None:
        return contents
    start, end = span
    try:
        header, entries, trailer = _split_block(contents[start:end])
    except WorkerMigrationError as exc:
        raise FrontProxyError(f"Composer cannot edit the caddy service's layout ({exc}).") from exc
    environment = next((entry for entry in entries if entry[0] == "environment"), None)
    if environment is None:
        keys = [key for key, _text in entries]
        after = next((keys.index(key) + 1 for key in ("ports", "labels", "restart", "image") if key in keys), 0)
        entries.insert(after, ["environment", f'    environment:\n      {VARIABLE}: "{COMPOSE_VALUE}"\n'])
    else:
        if _inline_value(environment[1]):
            raise FrontProxyError("caddy's environment is not a block mapping or list.")
        lines = environment[1].splitlines(keepends=True)
        key_index = next(index for index, line in enumerate(lines) if _ENVIRONMENT_KEY.match(line))
        body = [
            line for line in lines[key_index + 1:]
            if line.strip() and not line.lstrip().startswith("#")
        ]
        first = body[0] if body else "      "
        indent = first[: len(first) - len(first.lstrip(" "))]
        item = first.strip()
        if item.startswith("-"):
            quote = '"' if item[1:].lstrip().startswith('"') else ""
            added = f"{indent}- {quote}{VARIABLE}={COMPOSE_VALUE}{quote}\n"
        else:
            added = f'{indent}{VARIABLE}: "{COMPOSE_VALUE}"\n'
        last = max(index for index, line in enumerate(lines) if line.strip())
        lines.insert(last + 1, added)
        environment[1] = "".join(lines)
    return contents[:start] + header + "".join(text for _key, text in entries) + trailer + contents[end:]


def _env_values(path: Path, strip_quotes: bool = True) -> Dict[str, str]:
    """A dotenv file as Composer's ``parse_env_file`` reads it (quotes kept when ``strip_quotes`` is False)."""
    values: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        values[key.strip()] = value.strip("'\"") if strip_quotes else value
    return values


def _secrets(project_root: Path, raw: bool = False) -> Tuple[str, Optional[Dict[str, str]]]:
    """``(relative path, values)`` of the secrets file; values None when it cannot be read."""
    path = secrets_file(project_root)
    if path is None:
        return DEFAULT_SECRETS, None
    try:
        return str(path.relative_to(project_root)), _env_values(path, strip_quotes=not raw)
    except (OSError, ValueError):
        return str(path.relative_to(project_root)), None


def read_setting(project_root: Path, environ: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """``{"set", "value", "quoted", "source", "secrets"}``: the secrets file's value, else the environment's.

    ``./start.sh`` hands the secrets file to Composer with ``docker run --env-file``, which keeps quotes, so
    a quoted value reaches Caddy with its quotes; ``quoted`` reports it and ``value`` is the unquoted text.
    """
    project_root = Path(project_root).resolve()
    label, values = _secrets(project_root, raw=True)
    environ = os.environ if environ is None else environ
    if values is not None and VARIABLE in values:
        raw, source = values[VARIABLE], label
    elif VARIABLE in environ:
        raw, source = str(environ[VARIABLE]).strip(), "the environment"
    else:
        return {"set": False, "value": "", "quoted": False, "source": label, "secrets": label}
    value = raw.strip("'\"")
    return {"set": True, "value": value, "quoted": value != raw, "source": source, "secrets": label}


def _valid_range(token: str) -> bool:
    if token == PRIVATE_RANGES:
        return True
    if "%" in token:
        return False
    if "/" in token and not _PREFIX.fullmatch(token.split("/", 1)[1]):
        return False
    try:
        ipaddress.ip_network(token, strict=False)
    except ValueError:
        return False
    return True


def parse_ranges(value: str) -> Tuple[List[str], List[str]]:
    """``(entries, invalid entries)`` of a ``DLUX_FRONT_PROXY`` value."""
    entries = value.split()
    return entries, [entry for entry in entries if not _valid_range(entry)]


def published_port(project_root: Path, environ: Optional[Mapping[str, str]] = None) -> str:
    """Caddy's published port as the generated compose reads it: ``CADDY_PORT``, ``NGINX_PORT``, 80."""
    _label, values = _secrets(Path(project_root).resolve())
    environ = os.environ if environ is None else environ

    def lookup(name):
        return str(environ.get(name) or (values or {}).get(name) or "").strip().strip("'\"")

    return lookup("CADDY_PORT") or lookup("NGINX_PORT") or "80"


def _sources(project_root: Path, compose_files: Sequence[str], strict: bool) -> List[Path]:
    if strict:
        return [_project_file(project_root, value) for value in compose_files]
    sources = []
    for value in compose_files:
        try:
            sources.append(_project_file(project_root, value))
        except StackCleanupError:
            continue
    return sources


def _compose_state(sources: Sequence[Path]) -> Tuple[str, Optional[Path]]:
    """``(state, the file that defines caddy)``."""
    defining = []
    values = []
    for path in sources:
        text = path.read_text(encoding="utf-8")
        if _span(text, SERVICE) is None:
            continue
        defining.append(path)
        value = compose_value(text)
        if value is not None:
            values.append(value)
    if not defining:
        return NO_SERVICE, None
    if not values:
        return ABSENT, defining[0]
    return (PASSED if values[-1] == COMPOSE_VALUE else CUSTOM), defining[0]


def _plan(project_root: Path, compose_files: Sequence[str], strict: bool = True):
    """``(sources, updates, notes)``: every rewrite ``migrate`` makes, and what it cannot."""
    project_root = Path(project_root).resolve()
    sources = _sources(project_root, compose_files, strict)
    updates: Dict[Path, str] = {}
    notes: List[str] = []
    caddyfile = project_root / CADDYFILE
    if not caddyfile.is_file():
        return sources, updates, notes
    text = caddyfile.read_text(encoding="utf-8")
    if classify_caddyfile(text) not in (LEGACY, MANAGED):
        return sources, updates, notes
    state, defining = _compose_state(sources)
    if state == NO_SERVICE:
        return sources, updates, notes
    if classify_caddyfile(text) == LEGACY:
        updates[caddyfile.resolve()] = migrate_caddyfile(text)
    if state == ABSENT:
        try:
            updates[defining] = add_compose_variable(defining.read_text(encoding="utf-8"))
        except FrontProxyError as exc:
            notes.append(str(exc))
    return sources, updates, notes


def inspect(
    project_root: Path, compose_files: Sequence[str], environ: Optional[Mapping[str, str]] = None
) -> Dict[str, Any]:
    """The stack's state for ``check``. Reads files only; writes nothing."""
    project_root = Path(project_root).resolve()
    caddyfile = project_root / CADDYFILE
    sources, updates, notes = _plan(project_root, compose_files, strict=False)
    compose, defining = _compose_state(sources)
    setting = read_setting(project_root, environ)
    ranges, invalid = parse_ranges(setting["value"]) if setting["set"] else ([], [])
    return {
        "caddyfile": classify_caddyfile(caddyfile.read_text(encoding="utf-8")) if caddyfile.is_file() else MISSING,
        "compose": compose,
        "compose_file": str(defining.relative_to(project_root)) if defining else "",
        **setting,
        "ranges": ranges,
        "invalid": invalid,
        "files": sorted(str(path.relative_to(project_root)) for path in updates),
        "unsupported": " ".join(notes),
        "port": published_port(project_root, environ),
    }


def blocked_reason(project_root: Path, environ: Optional[Mapping[str, str]] = None) -> str:
    """Why ``migrate`` must not hand Caddy the current value, or ``""``."""
    setting = read_setting(project_root, environ)
    if setting["set"] and setting["quoted"]:
        return (
            f"DLUX_FRONT_PROXY in {setting['source']} is in quotes, which reach Caddy as part of the value, and "
            "Caddy would not start with a quoted list. Remove the quotes, then run 'composer check --fix' again."
        )
    _ranges, invalid = parse_ranges(setting["value"]) if setting["set"] else ([], [])
    if not invalid:
        return ""
    return (
        f"DLUX_FRONT_PROXY in {setting['source']} has entries that are not an IP address, a CIDR range or "
        f"private_ranges ({', '.join(invalid)}), and Caddy would not start with them. Correct it, then run "
        "'composer check --fix' again."
    )


def preview(
    project_dir: str, compose_files: Sequence[str], environ: Optional[Mapping[str, str]] = None
) -> Dict[str, Any]:
    """What ``migrate`` would change, as unified diffs. Writes nothing."""
    project_root = Path(project_dir).resolve()
    _sources_, updates, notes = _plan(project_root, compose_files, strict=False)
    if not updates:
        return {"files": [], "diff": "", "note": " ".join(notes)}
    blocked = blocked_reason(project_root, environ)
    if blocked:
        return {"files": [], "diff": "", "note": blocked}
    return {
        "files": sorted(str(path.relative_to(project_root)) for path in updates),
        "diff": preview_diff(project_root, updates),
        "note": " ".join(
            ["Left unset, DLUX_FRONT_PROXY keeps Caddy trusting every private network. The Compose and Caddy "
             "configuration is validated first; a running caddy is then recreated so it reads them."]
            + notes
        ),
    }


def _passes_variable(model: Mapping[str, Any]) -> bool:
    services = model.get("services") if isinstance(model.get("services"), dict) else {}
    environment = (services.get(SERVICE) or {}).get("environment") if isinstance(services.get(SERVICE), dict) else None
    if isinstance(environment, dict):
        return VARIABLE in environment
    if isinstance(environment, list):
        return any(str(item).split("=", 1)[0] == VARIABLE for item in environment)
    return False


def migrate(
    project_dir: str,
    compose_files: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
    command_runner=None,
) -> Dict[str, Any]:
    command_runner = command_runner or subprocess.run
    project_root = Path(project_dir).resolve()
    sources, updates, notes = _plan(project_root, compose_files)
    result: Dict[str, Any] = {
        "applied": False,
        "files": sorted(str(path.relative_to(project_root)) for path in updates),
        "backup_root": "",
        "recreated": [],
        "notes": notes,
    }
    if not updates:
        return result
    ensure_writable(project_root, updates)
    blocked = blocked_reason(project_root, environment)
    if blocked:
        raise FrontProxyError(blocked)
    if not shutil.which("docker"):
        raise FrontProxyError("Docker is required to validate the updated proxy configuration.")

    caddyfile = (project_root / CADDYFILE).resolve()
    staged: Dict[Path, Path] = {}
    try:
        for path in sources:
            if path in updates:
                staged[path] = _stage_candidate(path, updates[path])
        candidates = [staged.get(path, path) for path in sources]
        model = _compose_model(
            command_runner, project_root, candidates, environment, "The updated Compose configuration"
        )
        if any(path in updates for path in sources) and not _passes_variable(model):
            raise FrontProxyError(
                "Another Compose file replaces caddy's environment, so it would not receive DLUX_FRONT_PROXY; "
                "add it there by hand."
            )
        proxy = {caddyfile: updates.get(caddyfile) or caddyfile.read_text(encoding="utf-8")}
        mounts = [mount for mount in _active_proxy_mounts(model, project_root, proxy) if mount[0] == SERVICE]
        if mounts:
            staged[caddyfile] = _stage_candidate(caddyfile, proxy[caddyfile])
            _validate_proxy_candidates(command_runner, project_root, candidates, environment, mounts, staged)
    finally:
        for candidate in staged.values():
            if candidate.exists():
                candidate.unlink()

    archive = _archive_root(project_root)
    result["backup_root"] = str(archive)
    write_updates(project_root, updates, archive / "front-proxy")

    services = model.get("services") if isinstance(model.get("services"), dict) else {}
    if SERVICE in services and SERVICE in _running_services(command_runner, project_root, sources, environment):
        recreate = _run(
            command_runner,
            _compose_args(project_root, sources, "up", "-d", "--no-deps", "--force-recreate", SERVICE),
            project_root,
            environment,
        )
        if recreate.returncode != 0:
            detail = str(recreate.stderr or recreate.stdout or "").strip()[:500]
            raise FrontProxyError(f"Could not recreate {SERVICE}; restore from {archive}: {detail}")
        result["recreated"].append(SERVICE)
    result["applied"] = True
    return result
