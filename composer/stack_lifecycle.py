"""What ``check --fix`` does to the running stack after it changes the files.

A repair rewrites the Compose files; the containers keep running the definition
they were created from. ``check --fix`` compares the resolved Compose model
before and after its repairs, one service at a time: a service the repairs
dropped is stopped and removed, one they changed is recreated. A service left on
its old definition (``--no-recreate``, the resident pair during an Operations
panel apply) is recorded in ``.xclude/composer-check/pending-recreate.json`` with
the container it ran in, so ``check`` keeps reporting it until that container is
replaced.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set

from . import safe_files

PENDING_FILE = Path(".xclude") / "composer-check" / "pending-recreate.json"

#: Recreated last, in this order: composer-agent depends on composer-executor.
RESIDENT_SERVICES = ("composer-executor", "composer-agent")

#: Container states that mean the service is meant to be up.
LIVE_STATES = frozenset({"running", "restarting"})

#: The command that applies changed Compose files to a running stack. A plain
#: deploy runs `up -d`: Compose recreates exactly the services whose definition
#: changed, with the local images, and runs their start-up steps.
APPLY_COMMAND = "./start.sh"


def definitions(model: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    """``{service: canonical JSON of its resolved definition}`` (key order does not count)."""
    services = (model or {}).get("services") if isinstance(model, Mapping) else None
    if not isinstance(services, Mapping):
        return {}
    return {
        str(name): json.dumps(definition, sort_keys=True, separators=(",", ":"), default=str)
        for name, definition in services.items()
    }


def changes(before: Mapping[str, str], after: Mapping[str, str]) -> Dict[str, List[str]]:
    """``{"dropped", "changed", "added"}`` service names between two ``definitions``."""
    return {
        "dropped": sorted(set(before) - set(after)),
        "changed": sorted(name for name in set(before) & set(after) if before[name] != after[name]),
        "added": sorted(set(after) - set(before)),
    }


def images(model: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    """``{service: image reference}`` from a resolved model."""
    services = (model or {}).get("services") if isinstance(model, Mapping) else None
    if not isinstance(services, Mapping):
        return {}
    return {
        str(name): str(definition.get("image"))
        for name, definition in services.items()
        if isinstance(definition, Mapping) and definition.get("image")
    }


def ordered(services: Iterable[str]) -> List[str]:
    """``services`` with the resident pair split off and put last, executor first."""
    names = set(services)
    return sorted(names - set(RESIDENT_SERVICES)) + [name for name in RESIDENT_SERVICES if name in names]


def join(names: Iterable[str]) -> str:
    items = list(names)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def read_pending(root) -> Dict[str, Dict[str, str]]:
    """``{service: {"container", "since"}}`` recorded as still on an old definition."""
    path = Path(root) / PENDING_FILE
    try:
        data = json.loads(safe_files.read_bounded(path).decode("utf-8"))
    except (OSError, ValueError):
        return {}
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict):
        return {}
    return {
        str(name): {"container": str(entry.get("container") or ""), "since": str(entry.get("since") or "")}
        for name, entry in services.items()
        if isinstance(entry, dict) and entry.get("container")
    }


def record_pending(root, containers: Mapping[str, str], now: Optional[datetime] = None) -> Path:
    """Record ``{service: container id}`` as still running a definition the files no longer hold.

    Entries for other services are kept; ``still_pending`` drops the ones whose
    container has since been replaced.
    """
    path = Path(root) / PENDING_FILE
    since = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    services = read_pending(root)
    for name, container in containers.items():
        if container:
            services[str(name)] = {"container": str(container), "since": since}
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_files.atomic_write_json(path, {"schema_version": 1, "services": services})
    return path


def still_pending(recorded: Mapping[str, Mapping[str, str]], current: Mapping[str, Set[str]]) -> List[str]:
    """Services whose recorded container still runs (a restart keeps it; a recreate replaces it)."""
    return sorted(
        name for name, entry in recorded.items()
        if entry.get("container") and entry["container"] in (current.get(name) or set())
    )
