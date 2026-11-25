"""The stack schema a stack's files really meet, and repairing stamps that disagree.

A stamp says which stack schema a file was written for; the layout says which
schema the stack actually follows. They part when a migration restamps half a
file (Composer 1.10 and earlier rewrote only the first stamp, so Decrees'
compose.yml kept ``DLUX_STACK_SCHEMA: "3"`` under a ``# dlux stack schema 5``
header after two steps). ``layout_schema`` reads the layout from the files, step
by step, and ``plan_repair`` restamps every file whose own stamps disagree to
that schema.

Each schema step adds its test to ``layout_schema``. A file stamped newer than
the newest step this Composer knows is never touched: a newer Composer wrote it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import maintenance_migration, media_migration, worker_migration
from .stack_cleanup import (
    StackCleanupError,
    _archive_root,
    _compose_model,
    _project_file,
    _stage_candidate,
)
from .stack_schema import (
    STAMPED_FILES,
    ensure_writable,
    has_env_stamp,
    preview_diff,
    read_stamps,
    restamp,
    secrets_file,
    write_updates,
)

#: The newest stack schema whose layout this Composer can recognise.
NEWEST_KNOWN = worker_migration.TARGET_SCHEMA


def layout_schema(project_root: Path, compose_files: Sequence[str]) -> Tuple[Optional[int], str]:
    """``(schema, why)``: the schema the stack's layout meets, or ``(None, why not)``.

    Read from the files alone, newest step last: a step counts only when every
    earlier one is done.
    """
    project_root = Path(project_root).resolve()
    try:
        base, _dev = worker_migration._sources(project_root, compose_files)
    except StackCleanupError as exc:
        return None, str(exc)
    contents = base.read_text(encoding="utf-8")
    from .agent_installer import _runtime_migration_floor

    if _runtime_migration_floor(contents) != (0, 0, 0):
        return 1, "it still runs the retired tools/ runtime modules"
    if maintenance_migration.inspect(project_root, compose_files)["legacy"]:
        return 2, "the proxy still serves the project's maintenance page"
    media = media_migration.inspect(project_root)
    if media["exposed"]:
        return 3, "the proxy still serves stored files without authorization"
    if media["custom"]:
        return None, "the proxy has its own /media/ rules, which Composer cannot place in a schema"
    if worker_migration.WORKER not in worker_migration._services(contents):
        return 4, "it has no worker service"
    return 5, ""


def _candidates(project_root: Path, compose_files: Sequence[str]) -> List[Path]:
    paths: List[Path] = []
    for value in compose_files:
        try:
            paths.append(_project_file(project_root, value))
        except StackCleanupError:
            continue
    paths += [(project_root / relative).resolve() for relative in STAMPED_FILES]
    secrets = secrets_file(project_root)
    if secrets is not None:
        paths.append(secrets)
    seen, unique = set(), []
    for path in paths:
        if path not in seen and path.is_file():
            seen.add(path)
            unique.append(path)
    return unique


def _read(path: Path, secrets: Optional[Path]) -> str:
    if path == secrets:
        # Byte for byte, like restamp_secrets: only the stamp may change.
        return path.read_bytes().decode("utf-8", "surrogateescape")
    return path.read_text(encoding="utf-8")


def disagreeing_files(project_root: Path, compose_files: Sequence[str]) -> Dict[Path, List[int]]:
    """``{file: its stamps}`` for every stamped file whose own stamps disagree."""
    project_root = Path(project_root).resolve()
    secrets = secrets_file(project_root)
    found: Dict[Path, List[int]] = {}
    for path in _candidates(project_root, compose_files):
        try:
            stamps = read_stamps(_read(path, secrets))
        except OSError:
            continue
        if len(set(stamps)) > 1:
            found[path] = stamps
    return found


def plan_repair(project_root: Path, compose_files: Sequence[str]) -> Tuple[Dict[Path, str], List[str], Optional[int]]:
    """``(updates, notes, schema)``: each disagreeing file restamped to the layout's schema.

    Nothing is planned when the layout cannot be read, or when a file carries a
    stamp newer than any schema this Composer knows; ``notes`` says why.
    """
    project_root = Path(project_root).resolve()
    broken = disagreeing_files(project_root, compose_files)
    if not broken:
        return {}, [], None
    names = ", ".join(sorted(str(path.relative_to(project_root)) for path in broken))
    schema, why = layout_schema(project_root, compose_files)
    if schema is None:
        return {}, [f"Cannot tell which stack schema {names} should carry: {why}."], None
    newer = sorted(str(path.relative_to(project_root)) for path, stamps in broken.items() if max(stamps) > NEWEST_KNOWN)
    if newer:
        return {}, [
            f"{', '.join(newer)} carries a stack schema newer than {NEWEST_KNOWN}, the newest this "
            "Composer knows; update Composer ('./start.sh update self') and run 'composer check --fix' again."
        ], None
    secrets = secrets_file(project_root)
    updates = {path: restamp(_read(path, secrets), schema) for path in broken}
    notes = []
    for path in broken:
        if path.name in ("compose.yml", "docker-compose.yml") and not has_env_stamp(updates[path]):
            notes.append(f"{path.name} still has no DLUX_STACK_SCHEMA value; add it to its x-environment block.")
    return updates, notes, schema


def preview(project_dir: str, compose_files: Sequence[str]) -> Dict[str, Any]:
    project_root = Path(project_dir).resolve()
    updates, notes, schema = plan_repair(project_root, compose_files)
    return {
        "files": sorted(str(path.relative_to(project_root)) for path in updates),
        "diff": preview_diff(project_root, updates),
        "schema": schema,
        "notes": notes,
    }


def repair(
    project_dir: str,
    compose_files: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
    command_runner=None,
) -> Dict[str, Any]:
    """Restamp the disagreeing files: validated, the originals archived, then written."""
    command_runner = command_runner or subprocess.run
    project_root = Path(project_dir).resolve()
    updates, notes, schema = plan_repair(project_root, compose_files)
    result: Dict[str, Any] = {
        "applied": False,
        "files": sorted(str(path.relative_to(project_root)) for path in updates),
        "schema": schema,
        "backup_root": "",
        "notes": notes,
    }
    if not updates:
        return result
    ensure_writable(project_root, updates)
    sources = [_project_file(project_root, value) for value in compose_files]
    if any(path in updates for path in sources):
        if not shutil.which("docker"):
            raise StackCleanupError("Docker is required to validate the restamped Compose configuration.")
        staged: Dict[Path, Path] = {}
        try:
            for path in sources:
                if path in updates:
                    staged[path] = _stage_candidate(path, updates[path])
            _compose_model(
                command_runner, project_root, [staged.get(path, path) for path in sources],
                environment, "The restamped Compose configuration",
            )
        finally:
            for candidate in staged.values():
                if candidate.exists():
                    candidate.unlink()
    archive = _archive_root(project_root)
    result["backup_root"] = str(archive)
    write_updates(project_root, updates, archive / "stack-schema")
    result["applied"] = True
    return result
