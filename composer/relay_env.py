"""Share the SMTP relay's non-secret provider settings with ``web`` and ``worker``.

DjangoLux's System Settings Email step shows the relay's host, port, encryption
and user, and whether a password is set, read from web's own environment. The
generated compose passes them in the shared ``x-environment`` block, which
``web``, ``worker`` and ``smtp-relay`` merge::

  SMTP_RELAY_HOST: "${SMTP_RELAY_HOST:-smtp.gmail.com}"
  ...
  SMTP_RELAY_PASSWORD_SET: "${SMTP_RELAY_PASSWORD:+yes}"

The password itself stays in ``smtp-relay``'s environment; web gets
``SMTP_RELAY_PASSWORD_SET`` (``yes`` or empty) instead.

``inspect`` reports ``current``, ``legacy`` (``smtp-relay`` alone receives the
settings, each in the generated ``"${SMTP_RELAY_<KEY>:-<default>}"`` form),
``custom`` or ``no-service``. ``migrate`` moves a legacy stack's lines into the
shared block with the defaults the stack already used, validates the result with
``docker compose config`` (web sees what the relay uses, and no service but
``smtp-relay`` receives the password), archives the original under
``.xclude/composer-check/relay-env/`` and recreates the running ``web``,
``worker`` and ``smtp-relay``. Nothing here reads the secrets file, and the
password's value never appears in a preview, a message or an archive.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .stack_cleanup import (
    StackCleanupError,
    _archive_root,
    _compose_args,
    _compose_model,
    _project_file,
    _run,
    _running_services,
    _stage_candidate,
)
from .stack_schema import preview_diff, write_updates
from .worker_migration import _span

SERVICE = "smtp-relay"
RELAY_MODULES = ("dlux.smtp_relay", "tools.smtp_relay")
RECREATE = ("web", "worker", SERVICE)
READERS = ("web", "worker")
PASSWORD = "SMTP_RELAY_PASSWORD"
PASSWORD_SET = "SMTP_RELAY_PASSWORD_SET"
PASSWORD_SET_VALUE = "${SMTP_RELAY_PASSWORD:+yes}"
LEGACY_KEYS = ("SMTP_RELAY_HOST", "SMTP_RELAY_PORT", "SMTP_RELAY_USE_TLS", "SMTP_RELAY_USER")
SHARED_KEYS = ("SMTP_RELAY_HOST", "SMTP_RELAY_PORT", "SMTP_RELAY_USE_TLS", "SMTP_RELAY_USE_SSL", "SMTP_RELAY_USER")
MANAGED_KEYS = SHARED_KEYS + (PASSWORD_SET,)
INSERT_AFTER = "DEFAULT_FROM_EMAIL"
COMMENT = (
    "# The relay's provider settings. Shared so the Email step can show what the",
    "# relay uses; the password itself reaches smtp-relay only, web gets set/unset.",
    "# SMTP_RELAY_USE_SSL empty: SSL on port 465, STARTTLS (SMTP_RELAY_USE_TLS) otherwise.",
)
USE_SSL_VALUE = "${SMTP_RELAY_USE_SSL:-}"

CURRENT, LEGACY, CUSTOM, NO_SERVICE = "current", "legacy", "custom", "no-service"

_X_ENVIRONMENT = re.compile(r"^x-environment:[ \t]*(?:&([\w-]+))?[ \t]*(?:#.*)?$")
_ANCHOR_LINE = re.compile(r"^[ \t]+&([\w-]+)[ \t]*(?:#.*)?$")
_ENTRY = re.compile(r"^([ \t]+)(<<|[A-Za-z_][A-Za-z0-9_]*)[ \t]*:(?:[ \t]+(.*?))?[ \t]*$")
_ENVIRONMENT = re.compile(r"^    environment:[ \t]*(.*?)[ \t]*$")
_SERVICE_KEY = re.compile(r"^    [A-Za-z0-9_.-]+:")
_PASSWORD_REFERENCE = re.compile(r"^\$\{SMTP_RELAY_PASSWORD(?::?-)?\}$")
_PASSWORD_LINE = re.compile(r"""^([+\- ][ \t]*(?:-[ \t]*)?["']?SMTP_RELAY_PASSWORD["']?[ \t]*[:=][ \t]*)(.*)$""")


class RelayEnvError(StackCleanupError):
    """The stack is not in a shape this rewrite recognizes."""


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _content(line: str) -> str:
    return line.rstrip("\r\n")


def _scalar(raw: Optional[str]) -> Optional[str]:
    """A one-line YAML scalar's text, or None for anything else (a block, a flow, an escape)."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw[0] in "\"'":
        quote = raw[0]
        end = raw.find(quote, 1)
        if end < 0 or "\\" in raw[1:end]:
            return None
        rest = raw[end + 1:].strip()
        if rest and not rest.startswith("#"):
            return None
        return raw[1:end]
    if raw[0] in "[{|>&*!":
        return None
    value = re.split(r"[ \t]+#", raw, maxsplit=1)[0].strip()
    return value


def _pass_through(key: str, value: Optional[str]) -> bool:
    """``${KEY:-default}`` (or ``${KEY-default}``): the variable, else a default written into the file."""
    if value is None:
        return False
    return bool(re.fullmatch(rf"\$\{{{re.escape(key)}(?::?-)[^}}\"'\\$\n]*\}}", value))


def _anchor_block(lines: List[str]) -> Optional[Dict[str, Any]]:
    """The ``x-environment`` block: its anchor, entries and where an entry may be added."""
    for index, line in enumerate(lines):
        match = _X_ENVIRONMENT.match(_content(line))
        if match:
            break
    else:
        return None
    anchor = match.group(1)
    entries: Dict[str, Tuple[int, Optional[str]]] = {}
    indent = None
    last_entry_end = None
    insert_after = None
    position = index + 1
    while position < len(lines):
        text = _content(lines[position])
        stripped = text.strip()
        if stripped and _indent(text) == 0 and not stripped.startswith("#"):
            break
        if not stripped or stripped.startswith("#"):
            position += 1
            continue
        if anchor is None and indent is None and _ANCHOR_LINE.match(text):
            anchor = _ANCHOR_LINE.match(text).group(1)
            position += 1
            continue
        entry = _ENTRY.match(text)
        if entry and (indent is None or len(entry.group(1)) == indent):
            indent = len(entry.group(1))
            entries[entry.group(2)] = (position, _scalar(entry.group(3)))
            last_entry_end = position
            if entry.group(2) == INSERT_AFTER:
                insert_after = position
        elif indent is not None and _indent(text) > indent:
            last_entry_end = position
            if insert_after is not None and insert_after == position - 1:
                insert_after = position
        else:
            return None
        position += 1
    if anchor is None or indent is None:
        return None
    return {
        "anchor": anchor,
        "entries": entries,
        "indent": " " * indent,
        "insert": (insert_after if insert_after is not None else last_entry_end) + 1,
    }


def _relay_block(lines: List[str], contents: str) -> Optional[Dict[str, Any]]:
    """``smtp-relay``'s environment entries and merged anchors, or None when no service runs the relay."""
    span = _span(contents, SERVICE)
    if span is None:
        return None
    first = contents[:span[0]].count("\n")
    last = first + contents[span[0]:span[1]].count("\n")
    block = lines[first:last]
    if not any(module in "".join(block) for module in RELAY_MODULES):
        return None
    relay: Dict[str, Any] = {"environment": None, "entries": {}, "merges": set(), "problem": ""}
    for offset, line in enumerate(block):
        match = _ENVIRONMENT.match(_content(line))
        if not match:
            continue
        if match.group(1) and not match.group(1).startswith("#"):
            relay["problem"] = "smtp-relay's environment is not a block mapping"
            return relay
        relay["environment"] = first + offset
        indent = None
        for position in range(first + offset + 1, last):
            text = _content(lines[position])
            stripped = text.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if _indent(text) <= 4 or _SERVICE_KEY.match(text):
                break
            entry = _ENTRY.match(text)
            if not entry or (indent is not None and len(entry.group(1)) != indent):
                relay["problem"] = "smtp-relay's environment is not a block mapping"
                return relay
            indent = len(entry.group(1))
            if entry.group(2) == "<<":
                relay["merges"].update(re.findall(r"\*([\w-]+)", entry.group(3) or ""))
            else:
                relay["entries"][entry.group(2)] = (position, _scalar(entry.group(3)))
        break
    if relay["environment"] is None:
        relay["problem"] = "smtp-relay has no environment"
    return relay


def classify(contents: str) -> Dict[str, Any]:
    """``{"state", "details", "shared_password"}`` for one compose file's text."""
    contents = contents.replace("\r\n", "\n")
    lines = contents.splitlines(keepends=True)
    relay = _relay_block(lines, contents)
    if relay is None:
        return {"state": NO_SERVICE, "details": [], "shared_password": False}
    block = _anchor_block(lines)
    shared = block["entries"] if block else {}
    shared_password = PASSWORD in shared
    details: List[str] = []
    if shared_password:
        details.append("x-environment passes SMTP_RELAY_PASSWORD to every service that merges it")
    if block is None:
        details.append("Composer cannot read an anchored x-environment block")
    elif relay["problem"]:
        details.append(relay["problem"])
    elif block["anchor"] not in relay["merges"]:
        details.append(f"smtp-relay's environment does not merge *{block['anchor']}")
    if details:
        return {"state": CUSTOM, "details": details, "shared_password": shared_password}

    own = relay["entries"]
    password = own.get(PASSWORD)
    if password is None:
        details.append("smtp-relay is not passed SMTP_RELAY_PASSWORD")
    elif not _PASSWORD_REFERENCE.match(password[1] or ""):
        details.append('smtp-relay\'s SMTP_RELAY_PASSWORD is not "${SMTP_RELAY_PASSWORD:-}"')

    present = [key for key in MANAGED_KEYS if key in shared]
    if present:
        differ = [key for key in SHARED_KEYS if key in shared and not _pass_through(key, shared[key][1])]
        if PASSWORD_SET in shared and shared[PASSWORD_SET][1] != PASSWORD_SET_VALUE:
            differ.append(PASSWORD_SET)
        lacking = [key for key in MANAGED_KEYS if key not in shared]
        if differ:
            details.append("x-environment sets " + ", ".join(differ) + " differently from the generated lines")
        if lacking:
            details.append("x-environment lacks " + ", ".join(lacking))
        overrides = [
            key for key in MANAGED_KEYS
            if key in own and (key not in shared or own[key][1] != shared[key][1])
        ]
        if overrides:
            details.append("smtp-relay sets its own " + ", ".join(overrides))
        return {"state": CUSTOM if details else CURRENT, "details": details, "shared_password": False}

    literal = [key for key in SHARED_KEYS if key in own and not _pass_through(key, own[key][1])]
    missing = [key for key in LEGACY_KEYS if key not in own]
    if PASSWORD_SET in own:
        literal.append(PASSWORD_SET)
    if literal:
        details.append("smtp-relay sets " + ", ".join(literal) + " to its own value")
    if missing:
        details.append("smtp-relay is not passed " + ", ".join(missing))
    return {"state": CUSTOM if details else LEGACY, "details": details, "shared_password": False}


def migrate_compose(contents: str) -> str:
    """The compose text with the relay's settings in the shared block and out of ``smtp-relay``."""
    state = classify(contents)
    if state["state"] == CURRENT:
        return contents
    if state["state"] != LEGACY:
        raise RelayEnvError(
            "The SMTP relay's settings are not in the generated form ("
            + ("; ".join(state["details"]) or state["state"]) + ")."
        )
    crlf = "\r\n" in contents
    contents = contents.replace("\r\n", "\n")
    lines = contents.splitlines(keepends=True)
    block = _anchor_block(lines)
    relay = _relay_block(lines, contents)
    own = relay["entries"]
    values = {key: own[key][1] for key in SHARED_KEYS if key in own}
    values.setdefault("SMTP_RELAY_USE_SSL", USE_SSL_VALUE)
    added = [f"{block['indent']}{line}\n" for line in COMMENT]
    added += [f'{block["indent"]}{key}: "{values[key]}"\n' for key in SHARED_KEYS]
    added.append(f'{block["indent"]}{PASSWORD_SET}: "{PASSWORD_SET_VALUE}"\n')
    removed = sorted((own[key][0] for key in SHARED_KEYS if key in own), reverse=True)
    for position in removed:
        del lines[position]
    insert = block["insert"] - sum(1 for position in removed if position < block["insert"])
    if insert > 0 and not lines[insert - 1].endswith("\n"):
        lines[insert - 1] += "\n"
    lines[insert:insert] = added
    text = "".join(lines)
    return text.replace("\n", "\r\n") if crlf else text


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


def _defining(sources: Sequence[Path]) -> Tuple[Optional[Path], Dict[str, Any]]:
    """The compose file that defines the relay service, and its state."""
    for path in sources:
        text = path.read_text(encoding="utf-8")
        if _span(text, SERVICE) is None:
            continue
        state = classify(text)
        if state["state"] != NO_SERVICE:
            return path, state
    return None, {"state": NO_SERVICE, "details": [], "shared_password": False}


def _plan(project_root: Path, compose_files: Sequence[str], strict: bool = True):
    """``(sources, defining file, state, updates)``."""
    project_root = Path(project_root).resolve()
    sources = _sources(project_root, compose_files, strict)
    defining, state = _defining(sources)
    updates: Dict[Path, str] = {}
    if defining is not None and state["state"] == LEGACY:
        updates[defining] = migrate_compose(defining.read_text(encoding="utf-8"))
    return sources, defining, state, updates


def inspect(project_root: Path, compose_files: Sequence[str]) -> Dict[str, Any]:
    """The stack's state for ``check``. Reads compose files only; writes nothing."""
    project_root = Path(project_root).resolve()
    _sources_, defining, state, updates = _plan(project_root, compose_files, strict=False)
    return {
        **state,
        "compose_file": str(defining.relative_to(project_root)) if defining else "",
        "files": sorted(str(path.relative_to(project_root)) for path in updates),
    }


def _mask_password(diff: str) -> str:
    """The diff with any ``SMTP_RELAY_PASSWORD`` value other than the plain reference hidden."""
    masked = []
    for line in diff.splitlines(keepends=True):
        body = _content(line)
        match = _PASSWORD_LINE.match(body)
        if match:
            raw = match.group(2)
            value = raw.strip().strip("'\"") if match.group(1).rstrip().endswith("=") else _scalar(raw)
            if not _PASSWORD_REFERENCE.match(value or ""):
                line = f"{match.group(1)}[hidden]{line[len(body):]}"
        masked.append(line)
    return "".join(masked)


def preview(project_dir: str, compose_files: Sequence[str]) -> Dict[str, Any]:
    """What ``migrate`` would change, as a unified diff. Writes nothing."""
    project_root = Path(project_dir).resolve()
    _sources_, _defining_, _state, updates = _plan(project_root, compose_files, strict=False)
    if not updates:
        return {"files": [], "diff": "", "note": ""}
    return {
        "files": sorted(str(path.relative_to(project_root)) for path in updates),
        "diff": _mask_password(preview_diff(project_root, updates)),
        "note": (
            "web and worker receive the SMTP relay's host, port, encryption and user, so DjangoLux's Email step "
            "can show them; the password stays on smtp-relay and web only learns whether one is set. The "
            "Compose configuration is validated first; running web, worker and smtp-relay are then recreated "
            "so they read it."
        ),
    }


def _environment(model: Mapping[str, Any], service: str) -> Optional[Dict[str, Any]]:
    services = model.get("services") if isinstance(model.get("services"), dict) else {}
    spec = services.get(service)
    if not isinstance(spec, dict):
        return None
    environment = spec.get("environment")
    if isinstance(environment, dict):
        return environment
    if isinstance(environment, list):
        pairs = (str(item).split("=", 1) for item in environment)
        return {pair[0]: (pair[1] if len(pair) > 1 else None) for pair in pairs}
    return {}


def _check_model(model: Mapping[str, Any]) -> None:
    """Refuse a candidate where web would not see what the relay uses, or anyone else gets the password."""
    services = model.get("services") if isinstance(model.get("services"), dict) else {}
    leaking = sorted(
        name for name in services
        if name != SERVICE and PASSWORD in (_environment(model, name) or {})
    )
    if leaking:
        raise RelayEnvError(
            "SMTP_RELAY_PASSWORD would reach " + ", ".join(leaking)
            + "; it must reach smtp-relay only. Remove it there by hand."
        )
    relay = _environment(model, SERVICE) or {}
    readers = [name for name in READERS if name in services]
    for name in readers:
        environment = _environment(model, name) or {}
        differ = [key for key in SHARED_KEYS if str(environment.get(key) or "") != str(relay.get(key) or "")]
        has_password = bool(relay.get(PASSWORD))
        if PASSWORD_SET not in environment or (str(environment.get(PASSWORD_SET) or "") == "yes") != has_password:
            differ.append(PASSWORD_SET)
        if differ:
            raise RelayEnvError(
                f"Another Compose file gives {name} different SMTP relay settings from smtp-relay "
                f"({', '.join(differ)}); share them by hand."
            )


def migrate(
    project_dir: str,
    compose_files: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
    command_runner=None,
) -> Dict[str, Any]:
    command_runner = command_runner or subprocess.run
    project_root = Path(project_dir).resolve()
    sources, _defining_, _state, updates = _plan(project_root, compose_files)
    result: Dict[str, Any] = {
        "applied": False,
        "files": sorted(str(path.relative_to(project_root)) for path in updates),
        "backup_root": "",
        "recreated": [],
    }
    if not updates:
        return result
    if not shutil.which("docker"):
        raise RelayEnvError("Docker is required to validate the updated Compose configuration.")

    staged: Dict[Path, Path] = {}
    try:
        for path in sources:
            if path in updates:
                staged[path] = _stage_candidate(path, updates[path])
        candidates = [staged.get(path, path) for path in sources]
        model = _compose_model(
            command_runner, project_root, candidates, environment, "The updated Compose configuration"
        )
        _check_model(model)
    finally:
        for candidate in staged.values():
            if candidate.exists():
                candidate.unlink()

    archive = _archive_root(project_root)
    result["backup_root"] = str(archive)
    write_updates(project_root, updates, archive / "relay-env")

    services = model.get("services") if isinstance(model.get("services"), dict) else {}
    running = _running_services(command_runner, project_root, sources, environment)
    recreate = [name for name in RECREATE if name in services and name in running]
    if recreate:
        outcome = _run(
            command_runner,
            _compose_args(project_root, sources, "up", "-d", "--no-deps", "--force-recreate", *recreate),
            project_root,
            environment,
        )
        if outcome.returncode != 0:
            detail = str(outcome.stderr or outcome.stdout or "").strip()[:500]
            raise RelayEnvError(f"Could not recreate {', '.join(recreate)}; restore from {archive}: {detail}")
        result["recreated"] = recreate
    result["applied"] = True
    return result
