"""Pass ``DLUX_UPDATE_POLICY`` to web, worker and the resident pair.

The generated ``compose.yml`` carries one line in the shared ``x-environment``
block, which ``web`` and ``worker`` merge, and one in each resident service::

  DLUX_UPDATE_POLICY: "${DLUX_UPDATE_POLICY:-all}"          # x-environment
      DLUX_UPDATE_POLICY: "${DLUX_UPDATE_POLICY:-all}"      # composer-agent, composer-executor

``missing_services`` reads the resolved Compose model; ``preview`` and ``migrate``
add the lines a stack lacks, archive the original under
``.xclude/composer-check/<UTC>/update-policy/`` and recreate nothing: the next
``./start.sh`` recreates what changed. A stack whose block has no line to anchor
on is left alone with a note to add it by hand.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

from . import update_policy
from .stack_cleanup import _archive_root, _project_file
from .stack_schema import ensure_writable, preview_diff, write_updates

KEY = update_policy.ENV_KEY
VALUE = "${DLUX_UPDATE_POLICY:-all}"
SHARED = ("web", "worker")
RESIDENTS = ("composer-executor", "composer-agent")
READERS = SHARED + RESIDENTS
_SHARED_ANCHORS = ("DLUX_INLINE_UPDATES_ENABLED", "DLUX_UPDATE_RUNTIME_ROOT", "DLUX_UPDATE_CHECK_INTERVAL")
_RESIDENT_ANCHORS = ("COMPOSER_REGISTRY_HOST", "WEB_IMAGE")
_TOP_LEVEL = re.compile(r"(?m)^\S")


def _environment(service: Any) -> Dict[str, Any]:
    environment = (service or {}).get("environment") if isinstance(service, dict) else None
    if isinstance(environment, list):
        return dict(str(item).split("=", 1) if "=" in str(item) else (str(item), "") for item in environment)
    return environment if isinstance(environment, dict) else {}


def missing_services(model: Mapping[str, Any]) -> List[str]:
    """The readers the resolved Compose model defines but does not pass the policy to."""
    services = model.get("services") if isinstance(model.get("services"), dict) else {}
    return [name for name in READERS if name in services and KEY not in _environment(services[name])]


def _insert_after(body: str, anchors: Sequence[str], indent: str) -> str:
    for anchor in anchors:
        match = re.search(rf"(?m)^{indent}{anchor}:.*\n", body)
        if match:
            line = f'{indent}{KEY}: "{VALUE}"\n'
            return body[:match.end()] + line + body[match.end():]
    return body


def _shared(contents: str) -> str:
    header = re.search(r"(?m)^x-environment:[^\n]*\n", contents)
    if not header:
        return contents
    end_match = _TOP_LEVEL.search(contents, header.end())
    end = end_match.start() if end_match else len(contents)
    body = contents[header.end():end]
    if re.search(rf"(?m)^  {KEY}:", body):
        return contents
    return contents[:header.end()] + _insert_after(body, _SHARED_ANCHORS, "  ") + contents[end:]


def _resident(contents: str, service: str) -> str:
    header = re.search(rf"(?m)^  {re.escape(service)}:[ \t]*\n", contents)
    if not header:
        return contents
    end_match = re.compile(r"(?m)^(?:  \S|\S)").search(contents, header.end())
    end = end_match.start() if end_match else len(contents)
    body = contents[header.end():end]
    if re.search(rf"(?m)^      {KEY}:", body):
        return contents
    updated = _insert_after(body, _RESIDENT_ANCHORS, "      ")
    if updated == body:
        environment = re.search(r"(?m)^    environment:[ \t]*\n", body)
        if environment:
            line = f'      {KEY}: "{VALUE}"\n'
            updated = body[:environment.end()] + line + body[environment.end():]
    return contents[:header.end()] + updated + contents[end:]


def transform(contents: str) -> str:
    """Add the policy line wherever a generated block lacks it. Idempotent."""
    updated = _shared(contents)
    for service in RESIDENTS:
        updated = _resident(updated, service)
    return updated


def _plan(project_root: Path, compose_files: Sequence[str]) -> Dict[Path, str]:
    updates: Dict[Path, str] = {}
    for value in compose_files:
        path = _project_file(project_root, value)
        original = path.read_text(encoding="utf-8")
        candidate = transform(original)
        if candidate != original:
            updates[path] = candidate
    return updates


def preview(project_root, compose_files: Sequence[str]) -> Dict[str, Any]:
    root = Path(project_root).resolve()
    updates = _plan(root, compose_files)
    return {
        "files": [str(path.relative_to(root)) for path in updates],
        "diff": preview_diff(root, updates) if updates else "",
        "note": (
            f"web, worker, composer-agent and composer-executor read {KEY} from the secrets file. "
            "Run './start.sh' afterwards to recreate them."
        ) if updates else "",
    }


def migrate(project_root, compose_files: Sequence[str]) -> Dict[str, Any]:
    root = Path(project_root).resolve()
    updates = _plan(root, compose_files)
    result: Dict[str, Any] = {
        "applied": False,
        "files": [str(path.relative_to(root)) for path in updates],
        "backup_root": "",
    }
    if not updates:
        return result
    ensure_writable(root, updates)
    archive = _archive_root(root)
    write_updates(root, updates, archive / "update-policy")
    result["backup_root"] = str(archive)
    result["applied"] = True
    return result
