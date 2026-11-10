"""Give a stack DjangoLux's ``worker`` service (stack schema 5).

DjangoLux 1.16 runs its own tasks on its built-in queue with
``python manage.py dlux_worker``, in a ``worker`` service that takes over
everything ``celery`` did for DjangoLux: the ``pre_start`` init containers
(``dlux_reconcile``, then ``migrator``), ``DLUX_BOOT_GATE: apply``, the
read-write runtime mount and a place in ``COMPOSER_AGENT_RESTART_SERVICES``.
``celery`` stays, for the project's own tasks only.

``plan`` copies ``celery`` to ``worker`` with the project's extras (mounts,
environment anchors, networks), gives it the worker command, healthcheck and stop
grace period, and moves the init containers and the boot gate over. In
development it mirrors the ``compose.dev.yml`` celery override for ``worker``.
Files stamped 4 are restamped 5. An unstamped compose.yml is stamped 5 only when
nothing older is left in the stack (the packaged runtime, the maintenance page
Composer publishes, private stored files); otherwise it stays unstamped and the
result says why.

``migrate`` validates the merged configuration with ``docker compose config``
before writing anything and archives the originals under
``.xclude/composer-check/``. It recreates nothing: the next deploy starts
``worker``. Callers run it once the stack's DjangoLux has ``dlux_worker``
(``WORKER_FLOOR``); before that ``worker`` cannot start.
"""

from __future__ import annotations

import difflib
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import maintenance_migration, media_migration
from .stack_schema import STAMPED_FILES, read_stamp, restamp
from .stack_cleanup import (
    StackCleanupError,
    _archive_root,
    _compose_model,
    _project_file,
    _stage_candidate,
)

#: The first DjangoLux with ``manage.py dlux_worker``.
WORKER_FLOOR = (1, 16, 0)
LEGACY_SCHEMA = 4
TARGET_SCHEMA = 5
WORKER = "worker"
CELERY = "celery"
DEV_OVERRIDE = "compose.dev.yml"

# The generated schema 5 worker, as DjangoLux's scaffold writes it.
WORKER_COMMAND = """\
    # DjangoLux's task worker: queued tasks and DLUX_TASK_SCHEDULE, including
    # the write-side loop dlux.tasks.dlux_state_tick (small JSON writes on the
    # runtime volume, which is why this service mounts it rw).
    command: ["python", "-m", "dlux.updater.supervisor", "--", "python", "manage.py", "dlux_worker"]
    # dlux_worker lets running tasks finish for up to 25 s after SIGTERM.
    stop_grace_period: 30s
"""
WORKER_HEALTHCHECK = """\
    healthcheck:
      test: ["CMD", "python", "manage.py", "dlux_worker", "--check"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s
"""
BOOT_GATE = """\
      # This is the service that applies migrations. pre_start remains the fast
      # path; `apply` is what covers the restart-policy restart Compose skips it
      # on, where web and worker would otherwise both wait for a migration
      # nothing was left to run.
      DLUX_BOOT_GATE: "apply"
"""
CELERY_NOTE = """\
    # The project's own Celery tasks. DjangoLux runs its tasks in `worker` and
    # deprecates Celery for project tasks; the runtime mount stays read-write for
    # relay requests from project tasks.
"""
STAMP_LINES = """\
  # The stack contract schema this file follows (see DjangoLux docs/stack-schema.md).
  DLUX_STACK_SCHEMA: "5"
"""

_KEY = re.compile(r"^    ([A-Za-z0-9_.-]+):(?:[ \t]|$)")
_GATE = re.compile(r"""^      (?:-[ \t]*)?DLUX_BOOT_GATE[ \t]*[:=][ \t]*["']?([A-Za-z]*)["']?[ \t]*$""")
_RESTART_ENV = re.compile(
    r"""(?m)^([ \t]+COMPOSER_AGENT_RESTART_SERVICES:[ \t]*)(["']?)([^"'\n]*?)\2[ \t]*$"""
)
_X_ENVIRONMENT = re.compile(r"(?m)^x-environment:[ \t]*(&[\w-]+)?[ \t]*\n")


class WorkerMigrationError(StackCleanupError):
    """The stack is not in a shape this migration recognizes."""


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _services(contents: str) -> set:
    from .agent_installer import AgentInstallError, _service_names

    try:
        return _service_names(contents)
    except AgentInstallError:
        return set()


def _span(contents: str, service: str):
    from .agent_installer import _service_block_span

    return _service_block_span(contents, service)


def _split_block(block: str) -> Tuple[str, List[List[str]], str]:
    """``(header, entries, trailer)`` of one service block.

    An entry is ``[key, text]``: one key of the service with the comment lines
    right above it. The trailer is what follows the last entry: blank lines and
    the next service's leading comments.
    """
    lines = block.splitlines(keepends=True)
    header, entries, pending = lines[0], [], []
    for line in lines[1:]:
        if not line.endswith("\n"):
            line += "\n"
        match = _KEY.match(line)
        stripped = line.strip()
        if match:
            entries.append([match.group(1), "".join(pending) + line])
            pending = []
        elif not stripped or (stripped.startswith("#") and _indent(line) <= 4):
            pending.append(line)
        elif entries:
            entries[-1][1] += "".join(pending) + line
            pending = []
        else:
            raise WorkerMigrationError(f"Unrecognized line in the service block: {stripped[:80]}")
    return header, entries, "".join(pending)


def _leading_comments_start(contents: str, start: int) -> int:
    """Where the service-level comments right above ``start`` begin."""
    index = start
    for line in reversed(contents[:start].splitlines(keepends=True)):
        if not line.startswith("  #"):
            break
        index -= len(line)
    return index


def _inline_value(entry: str) -> str:
    """The value written on an entry's key line (``*de``, ``{…}``), or ``""``."""
    line = next(line for line in entry.splitlines() if _KEY.match(line))
    return line.split(":", 1)[1].split(" #", 1)[0].strip()


def _read_write_runtime(text: str) -> str:
    text = text.replace("dlux_runtime:/opt/dlux-runtime:ro", "dlux_runtime:/opt/dlux-runtime:rw")
    return re.sub(r"(?m)^(\s+- static:/app/staticfiles):ro$", r"\1:rw", text)


def _with_boot_gate(text: str) -> str:
    """The environment entry, with ``DLUX_BOOT_GATE`` set to ``apply``."""
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        match = _GATE.match(line.rstrip("\n"))
        if match:
            if match.group(1) != "apply":
                lines[index] = (
                    "      - DLUX_BOOT_GATE=apply\n" if "=" in line.split("#")[0]
                    else '      DLUX_BOOT_GATE: "apply"\n'
                )
            return "".join(lines).replace(
                "web and celery would otherwise", "web and worker would otherwise"
            )
    key_index = next(i for i, line in enumerate(lines) if _KEY.match(line))
    if _inline_value(text):
        raise WorkerMigrationError(
            "celery's environment is not a block mapping or list; add the worker service by hand."
        )
    body = lines[key_index + 1:]
    if any(line.startswith("      - ") for line in body):
        return "".join(lines[: key_index + 1]) + "      - DLUX_BOOT_GATE=apply\n" + "".join(body)
    insert_at = key_index + 1
    while insert_at < len(lines) and lines[insert_at].startswith("      <<:"):
        insert_at += 1
    return "".join(lines[:insert_at]) + BOOT_GATE + "".join(lines[insert_at:])


def _without_apply_gate(text: str) -> str:
    """The environment entry minus ``DLUX_BOOT_GATE: apply`` and its comment."""
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        match = _GATE.match(line.rstrip("\n"))
        if match and match.group(1) == "apply":
            start = index
            while start > 0 and lines[start - 1].startswith("      #"):
                start -= 1
            return "".join(lines[:start] + lines[index + 1:])
    return text


def _with_restart_label(entries: List[List[str]]) -> None:
    for entry in entries:
        if entry[0] != "labels":
            continue
        if "org.dlux.restart" in entry[1] or _inline_value(entry[1]):
            return
        if "\n      - " in entry[1]:
            entry[1] += "      - org.dlux.restart=safe\n"
        else:
            entry[1] += '      org.dlux.restart: "safe"\n'
        return
    after = next((i + 1 for i, entry in enumerate(entries) if entry[0] == "restart"), 1)
    entries.insert(after, ["labels", '    labels:\n      org.dlux.restart: "safe"\n'])


def _worker_block(entries: List[List[str]]) -> str:
    """``worker``, copied from celery's entries."""
    applies = any(key == "pre_start" for key, _ in entries)
    worker: List[List[str]] = []
    for key, text in entries:
        # A container name must be unique; the others are replaced below.
        if key in {"container_name", "hostname", "healthcheck", "stop_grace_period"}:
            continue
        if key == "command":
            text = WORKER_COMMAND
        elif key == "volumes":
            text = _read_write_runtime(text)
            if "dlux_runtime" not in text:
                text += "      - dlux_runtime:/opt/dlux-runtime:rw\n"
        elif key == "environment" and applies:
            text = _with_boot_gate(text)
        worker.append([key, text])
    if not any(key == "volumes" for key, _ in worker):
        raise WorkerMigrationError("celery declares no volumes; add the worker service by hand.")
    _with_restart_label(worker)
    at = next((i for i, (key, _) in enumerate(worker) if key in {"depends_on", "networks"}), len(worker))
    worker.insert(at, ["healthcheck", WORKER_HEALTHCHECK])
    return f"  {WORKER}:\n" + "".join(text for _, text in worker)


def _project_celery(entries: List[List[str]]) -> List[List[str]]:
    """celery for project tasks: no init containers, gated like web."""
    kept: List[List[str]] = []
    for key, text in entries:
        if key == "pre_start":
            continue
        if key == "environment":
            text = _without_apply_gate(text)
        elif key == "command" and "dlux_state_tick" not in text:
            text = CELERY_NOTE + text
        elif key == "command":
            text = CELERY_NOTE + "".join(
                line for line in text.splitlines(keepends=True) if not line.startswith("    #")
            )
        kept.append([key, text])
    return kept


def with_restart_target(contents: str) -> str:
    """``worker`` in ``COMPOSER_AGENT_RESTART_SERVICES``, right after ``web``."""

    def add(match):
        names = [name.strip() for name in match.group(3).split(",") if name.strip()]
        if WORKER in names:
            return match.group(0)
        names.insert(names.index("web") + 1 if "web" in names else 0, WORKER)
        quote = match.group(2)
        return f"{match.group(1)}{quote}{','.join(names)}{quote}"

    return _RESTART_ENV.sub(add, contents)


def migrate_compose(contents: str) -> str:
    """The base compose file with ``worker`` added; unchanged when it already has one."""
    services = _services(contents)
    if WORKER in services or CELERY not in services:
        return contents
    if "dlux-updater" in services:
        raise WorkerMigrationError(
            "'dlux-updater' must be retired into init containers before 'worker' is added."
        )
    start, end = _span(contents, CELERY)
    header, entries, trailer = _split_block(contents[start:end])
    if not any(key == "command" for key, _ in entries):
        raise WorkerMigrationError("celery declares no command; add the worker service by hand.")
    worker = _worker_block(entries)
    celery = header + "".join(text for _, text in _project_celery(entries))
    lead = _leading_comments_start(contents, start)
    updated = (
        contents[:lead] + worker + "\n" + contents[lead:start] + celery + trailer + contents[end:]
    )
    return with_restart_target(updated)


def migrate_dev_override(contents: str) -> str:
    """compose.dev.yml with celery's override mirrored for ``worker``."""
    services = _services(contents)
    if WORKER in services or CELERY not in services:
        return contents
    start, end = _span(contents, CELERY)
    _header, entries, _trailer = _split_block(contents[start:end])
    worker = f"  {WORKER}:\n" + "".join(
        _read_write_runtime(text) if key == "volumes" else text for key, text in entries
    )
    worker = worker.replace(
        "celery hosts the update worker, so it needs", "worker runs the update tasks, so it needs"
    )
    lead = _leading_comments_start(contents, start)
    return contents[:lead] + worker + "\n" + contents[lead:]


def _with_stamp(contents: str) -> Optional[str]:
    """``DLUX_STACK_SCHEMA: "5"`` added to the shared environment, or None."""
    match = _X_ENVIRONMENT.search(contents)
    if not match:
        return None
    position = match.end()
    if not match.group(1):
        anchor = re.match(r"(?:[ \t]*#[^\n]*\n|[ \t]*\n)*  &[\w-]+[ \t]*\n", contents[position:])
        if not anchor:
            return None
        position += anchor.end()
    return contents[:position] + STAMP_LINES + contents[position:]


_REQUIREMENT = re.compile(
    r"django-lux(?:\[[^\]]+\])?\s*(?:==|>=|~=)\s*[\"']?(\d+)\.(\d+)(?:\.(\d+))?", re.IGNORECASE
)


def declared_dlux(project_root: Path) -> Optional[Tuple[int, int, int]]:
    """The highest DjangoLux floor the project's requirements name, or None."""
    found = []
    for name in ("requirements.txt", "pyproject.toml"):
        try:
            text = (project_root / name).read_text(encoding="utf-8")
        except OSError:
            continue
        found += [tuple(int(part or 0) for part in match) for match in _REQUIREMENT.findall(text)]
    return max(found) if found else None


def older_layout(project_root: Path, compose_files: Sequence[str], contents: str) -> str:
    """Why the stack does not meet stack schemas 2-4, or ``""`` when it does."""
    from .agent_installer import _runtime_migration_floor

    if _runtime_migration_floor(contents) != (0, 0, 0):
        return "it still runs the retired tools/ runtime modules (stack schema 2)"
    if maintenance_migration.inspect(project_root, compose_files)["legacy"]:
        return "the proxy still serves the project's maintenance page (stack schema 3)"
    media = media_migration.inspect(project_root)
    if media["exposed"]:
        return "the proxy still serves stored files without authorization (stack schema 4)"
    if media["custom"]:
        return "the proxy has its own /media/ rules, which Composer cannot verify (stack schema 4)"
    return ""


def _sources(project_root: Path, compose_files: Sequence[str]) -> Tuple[Path, Optional[Path]]:
    paths = [_project_file(project_root, value) for value in compose_files]
    dev = next((path for path in paths if path.name == DEV_OVERRIDE), None)
    base = next((path for path in paths if path != dev), None)
    if base is None:
        raise WorkerMigrationError("No base Compose file to add the worker service to.")
    return base, dev


def pending(project_dir: str, compose_files: Sequence[str]) -> Dict[str, bool]:
    """What is left to do, as ``{"compose", "dev", "stamp"}``. Writes nothing.

    ``compose``: the base file has celery and no worker. ``dev``: the active
    development override has a celery override and no worker one. ``stamp``: the
    base file has worker but its stamps can still be brought to 5.
    """
    project_root = Path(project_dir).resolve()
    nothing = {"compose": False, "dev": False, "stamp": False}
    try:
        base, dev = _sources(project_root, compose_files)
    except StackCleanupError:
        return nothing
    base_services = _services(base.read_text(encoding="utf-8"))
    dev_services = _services(dev.read_text(encoding="utf-8")) if dev else set()
    result = {
        "compose": CELERY in base_services and WORKER not in base_services,
        "dev": CELERY in dev_services and WORKER not in dev_services,
        "stamp": False,
    }
    if WORKER in base_services and not result["dev"]:
        try:
            result["stamp"] = bool(_plan(project_root, compose_files)[1])
        except StackCleanupError:
            pass
    return result


def _stamps(project_root, compose_files, sources, base, updates, notes) -> None:
    """Bring the stack's stamps to 5 once its compose file has worker."""
    base_text = updates.get(base) or base.read_text(encoding="utf-8")
    if not updates and read_stamp(base_text) not in (None, LEGACY_SCHEMA):
        return
    # compose.dev.yml keeps its stamp while it still lacks the worker override.
    dev_file = (project_root / DEV_OVERRIDE).resolve()
    dev_waits = False
    if dev_file.is_file() and dev_file not in updates:
        dev_services = _services(dev_file.read_text(encoding="utf-8"))
        dev_waits = CELERY in dev_services and WORKER not in dev_services
    for path in (*sources, *(project_root / relative for relative in STAMPED_FILES)):
        path = path.resolve()
        if not path.is_file() or (path == dev_file and dev_waits):
            continue
        text = updates.get(path, path.read_text(encoding="utf-8"))
        if read_stamp(text) == LEGACY_SCHEMA:
            updates[path] = restamp(text, TARGET_SCHEMA)
    if dev_waits:
        notes.append(
            f"{DEV_OVERRIDE} overrides celery but not worker yet; run './start.sh -d check --fix' "
            "to mirror it."
        )

    base_after = updates.get(base, base_text)
    stamp = read_stamp(base_after)
    if stamp is not None:
        if stamp < TARGET_SCHEMA:
            notes.append(
                f"{base.name} stays stamped {stamp}: bring it to stack schema {LEGACY_SCHEMA} "
                "first, then run 'composer check --fix' again."
            )
        return
    reason = older_layout(project_root, compose_files, base_after)
    lagging = [
        relative for relative in STAMPED_FILES
        if (project_root / relative).is_file()
        and (read_stamp(updates.get((project_root / relative).resolve())
                        or (project_root / relative).read_text(encoding="utf-8")) or TARGET_SCHEMA)
        < TARGET_SCHEMA
    ]
    if not reason and lagging:
        reason = "older schema stamps remain on " + ", ".join(lagging)
    stamped = None if reason else _with_stamp(base_after)
    if stamped is not None:
        updates[base] = stamped
    else:
        notes.append(
            f"{base.name} stays unstamped: "
            + (reason or "it has no x-environment anchor to carry DLUX_STACK_SCHEMA")
            + "."
        )


def _plan(project_root: Path, compose_files: Sequence[str]):
    """``(sources, updates, notes)``: every rewrite ``migrate`` makes, and caveats."""
    project_root = Path(project_root).resolve()
    base, dev = _sources(project_root, compose_files)
    sources = [base] + ([dev] if dev else [])
    updates: Dict[Path, str] = {}
    notes: List[str] = []
    base_text = base.read_text(encoding="utf-8")
    migrated = migrate_compose(base_text)
    if migrated != base_text:
        updates[base] = migrated
    if WORKER not in _services(migrated):
        return sources, updates, notes
    if dev is not None:
        dev_text = dev.read_text(encoding="utf-8")
        mirrored = migrate_dev_override(dev_text)
        if mirrored != dev_text:
            updates[dev] = mirrored
    _stamps(project_root, compose_files, sources, base, updates, notes)
    return sources, updates, notes


def preview(project_dir: str, compose_files: Sequence[str]) -> Dict[str, Any]:
    """What ``migrate`` would change, as unified diffs. Writes nothing."""
    project_root = Path(project_dir).resolve()
    _sources_, updates, notes = _plan(project_root, compose_files)
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
        "note": " ".join(
            ["The Compose configuration is validated first; the next deploy starts worker."] + notes
        ),
    }


def _validate(command_runner, project_root, sources, updates, environment) -> None:
    staged: Dict[Path, Path] = {}
    try:
        for path in sources:
            if path in updates:
                staged[path] = _stage_candidate(path, updates[path])
        model = _compose_model(
            command_runner, project_root, [staged.get(path, path) for path in sources],
            environment, "The migrated Compose configuration",
        )
    finally:
        for candidate in staged.values():
            if candidate.exists():
                candidate.unlink()
    services = model.get("services") if isinstance(model.get("services"), dict) else {}
    worker = services.get(WORKER)
    if not isinstance(worker, dict):
        raise WorkerMigrationError("The migrated Compose configuration has no worker service.")
    command = worker.get("command")
    command = " ".join(map(str, command)) if isinstance(command, list) else str(command or "")
    if "dlux_worker" not in command:
        raise WorkerMigrationError(
            "Another Compose file overrides the worker command; check it, then run check --fix again."
        )
    celery = services.get(CELERY)
    if isinstance(celery, dict) and celery.get("pre_start"):
        raise WorkerMigrationError(
            "Another Compose file still gives celery init containers; move them to worker by hand."
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
    sources, updates, notes = _plan(project_root, compose_files)
    result: Dict[str, Any] = {
        "applied": False,
        "files": sorted(str(path.relative_to(project_root)) for path in updates),
        "backup_root": "",
        "notes": notes,
    }
    if not updates:
        return result
    if not shutil.which("docker"):
        raise WorkerMigrationError("Docker is required to validate the migrated Compose configuration.")
    _validate(command_runner, project_root, sources, updates, environment)

    archive = _archive_root(project_root)
    result["backup_root"] = str(archive)
    for path in updates:
        destination = archive / "worker-migration" / path.relative_to(project_root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    for path, text in updates.items():
        with path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
    result["applied"] = True
    return result
