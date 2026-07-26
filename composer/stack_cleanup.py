import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from .proxy_cleanup import (
    PROXY_ROUTE_FILES,
    inspect_legacy_proxy_routes,
    proxy_route_updates,
)


OBSOLETE_SERVICES = frozenset({"db-backup", "db_backup", "pgadmin"})


class StackCleanupError(RuntimeError):
    pass


def _services_section(contents: str) -> tuple[list[str], int, int, int]:
    lines = contents.splitlines(keepends=True)
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if re.fullmatch(r"services:\s*(?:#.*)?(?:\r?\n)?", line)
        ),
        -1,
    )
    if start < 0:
        raise StackCleanupError("Could not find the top-level Compose services block.")

    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.strip() and not line.lstrip().startswith("#") and not line[0].isspace():
            end = index
            break

    indents = []
    for line in lines[start + 1 : end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"^( +)[A-Za-z0-9_.-]+:", line)
        if match:
            indents.append(len(match.group(1)))
    if not indents:
        return lines, start, end, 0
    return lines, start, end, min(indents)


def remove_obsolete_service_blocks(contents: str) -> tuple[str, set[str]]:
    lines, section_start, section_end, service_indent = _services_section(contents)
    if not service_indent:
        return contents, set()

    targets: list[tuple[int, int, str]] = []
    header = re.compile(
        rf"^ {{{service_indent}}}({'|'.join(re.escape(name) for name in sorted(OBSOLETE_SERVICES))}):(?:\s.*)?(?:\r?\n)?$"
    )
    for index in range(section_start + 1, section_end):
        match = header.match(lines[index])
        if not match:
            continue
        block_end = index + 1
        while block_end < section_end:
            line = lines[block_end]
            if line.strip():
                indent = len(line) - len(line.lstrip())
                if indent <= service_indent and (
                    line.lstrip().startswith("#") or not line[0].isspace() or ":" in line
                ):
                    break
            block_end += 1
        targets.append((index, block_end, match.group(1)))

    if not targets:
        return contents, set()

    removed = {name for _, _, name in targets}
    skipped = {
        index
        for block_start, block_end, _ in targets
        for index in range(block_start, block_end)
    }
    return "".join(line for index, line in enumerate(lines) if index not in skipped), removed


def _top_level_mapping_keys(contents: str, section_name: str) -> set[str]:
    lines = contents.splitlines()
    header = next(
        (
            index
            for index, line in enumerate(lines)
            if re.fullmatch(rf"{re.escape(section_name)}:\s*(?:#.*)?", line)
        ),
        -1,
    )
    if header < 0:
        return set()
    section = []
    for line in lines[header + 1 :]:
        if line.strip() and not line.lstrip().startswith("#") and not line[0].isspace():
            break
        if line.strip() and not line.lstrip().startswith("#"):
            section.append(line)
    indents = [
        len(match.group(1))
        for line in section
        if (match := re.match(r"^( +)[A-Za-z0-9_.-]+:", line))
    ]
    if not indents:
        return set()
    indent = min(indents)
    return {
        match.group(1)
        for line in section
        if (
            match := re.match(
                rf"^ {{{indent}}}([A-Za-z0-9_.-]+):",
                line,
            )
        )
    }


def _archive_root(project_root: Path) -> Path:
    base = project_root / ".xpose" / "composer-check"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = base / stamp
    suffix = 1
    while destination.exists():
        suffix += 1
        destination = base / f"{stamp}-{suffix}"
    destination.mkdir(parents=True)
    return destination


def _project_file(project_root: Path, value: str) -> Path:
    path = Path(value)
    path = (path if path.is_absolute() else project_root / path).resolve()
    if not path.is_relative_to(project_root) or not path.is_file():
        raise StackCleanupError(
            f"The Compose file must exist inside the project directory: {value}"
        )
    return path


def _stage_candidate(source: Path, contents: str) -> Path:
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{source.name}.composer-check-",
        suffix=".tmp",
        dir=source.parent,
        text=True,
    )
    path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(stat.S_IMODE(source.stat().st_mode))
    except Exception:
        path.rename(path.with_name(f"{path.name}.failed"))
        raise
    return path


def _archive_staged(
    project_root: Path,
    staged: Mapping[Path, Path],
    archive_root: Path,
    directory: str,
) -> None:
    for source, candidate in staged.items():
        if not candidate.exists():
            continue
        relative = source.relative_to(project_root)
        destination = archive_root / directory / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        candidate.replace(destination)


def _compose_args(project_root: Path, compose_files: Sequence[Path], *action: str) -> list[str]:
    args = ["docker", "compose", "--project-directory", str(project_root)]
    for path in compose_files:
        args.extend(["-f", str(path)])
    args.extend(action)
    return args


def _run(
    command_runner,
    args: Sequence[str],
    project_root: Path,
    environment: Mapping[str, str] | None,
):
    try:
        return command_runner(
            list(args),
            cwd=str(project_root),
            check=False,
            capture_output=True,
            text=True,
            env=dict(environment) if environment is not None else None,
        )
    except OSError as exc:
        raise StackCleanupError(f"Could not execute {' '.join(args[:3])}: {exc}") from exc


def _compose_model(
    command_runner,
    project_root: Path,
    compose_files: Sequence[Path],
    environment: Mapping[str, str] | None,
    context: str,
) -> Dict[str, Any]:
    result = _run(
        command_runner,
        _compose_args(project_root, compose_files, "config", "--format", "json"),
        project_root,
        environment,
    )
    if result.returncode != 0:
        detail = str(result.stderr or "").strip()[:1000]
        suffix = f": {detail}" if detail else ""
        raise StackCleanupError(f"{context} failed docker compose config{suffix}")
    try:
        payload = json.loads(result.stdout or "")
    except (TypeError, ValueError) as exc:
        raise StackCleanupError(f"{context} returned invalid Compose JSON.") from exc
    if not isinstance(payload, dict):
        raise StackCleanupError(f"{context} returned an invalid Compose model.")
    return payload


def _declared_volumes(model: Mapping[str, Any]) -> Dict[str, str]:
    declared = model.get("volumes")
    if not isinstance(declared, dict):
        return {}
    result = {}
    for logical_name, definition in declared.items():
        runtime_name = definition.get("name") if isinstance(definition, dict) else ""
        result[str(logical_name)] = str(runtime_name or logical_name)
    return result


def _existing_docker_volumes(
    command_runner,
    project_root: Path,
    environment: Mapping[str, str] | None,
) -> set[str]:
    result = _run(
        command_runner,
        ["docker", "volume", "ls", "--format", "{{.Name}}"],
        project_root,
        environment,
    )
    if result.returncode != 0:
        detail = str(result.stderr or "").strip()[:1000]
        suffix = f": {detail}" if detail else ""
        raise StackCleanupError(f"Could not inspect Docker volumes{suffix}")
    return {line.strip() for line in str(result.stdout or "").splitlines() if line.strip()}


def _active_proxy_mounts(
    model: Mapping[str, Any],
    project_root: Path,
    proxy_updates: Mapping[Path, str],
) -> list[tuple[str, Path, str, str]]:
    services = model.get("services")
    if not isinstance(services, dict):
        return []
    kinds = {
        (project_root / relative).resolve(): kind
        for relative, kind in PROXY_ROUTE_FILES.items()
    }
    mounts = []
    for service_name in ("caddy", "nginx"):
        service = services.get(service_name)
        if not isinstance(service, dict):
            continue
        volumes = service.get("volumes")
        if not isinstance(volumes, list):
            continue
        for volume in volumes:
            if not isinstance(volume, dict) or volume.get("type") != "bind":
                continue
            source = Path(str(volume.get("source") or "")).resolve()
            target = str(volume.get("target") or "")
            if source in proxy_updates and source in kinds and target:
                mounts.append((service_name, source, target, kinds[source]))
    return mounts


def _validate_proxy_candidates(
    command_runner,
    project_root: Path,
    compose_files: Sequence[Path],
    environment: Mapping[str, str] | None,
    mounts: Sequence[tuple[str, Path, str, str]],
    staged: Mapping[Path, Path],
) -> list[str]:
    validated = []
    for service, source, target, kind in mounts:
        candidate = staged[source]
        action = [
            "run",
            "--rm",
            "--no-deps",
            "-v",
            f"{candidate}:{target}:ro",
            service,
        ]
        if kind == "caddy":
            action.extend(
                ["caddy", "validate", "--config", target, "--adapter", "caddyfile"]
            )
        else:
            action.extend(["nginx", "-t"])
        result = _run(
            command_runner,
            _compose_args(project_root, compose_files, *action),
            project_root,
            environment,
        )
        if result.returncode != 0:
            detail = str(result.stderr or result.stdout or "").strip()[:1000]
            suffix = f": {detail}" if detail else ""
            raise StackCleanupError(
                f"{service} rejected the cleaned proxy configuration{suffix}"
            )
        validated.append(service)
    return sorted(set(validated))


def _running_services(
    command_runner,
    project_root: Path,
    compose_files: Sequence[Path],
    environment: Mapping[str, str] | None,
) -> set[str]:
    result = _run(
        command_runner,
        _compose_args(
            project_root,
            compose_files,
            "ps",
            "--services",
            "--status",
            "running",
        ),
        project_root,
        environment,
    )
    if result.returncode != 0:
        detail = str(result.stderr or "").strip()[:1000]
        suffix = f": {detail}" if detail else ""
        raise StackCleanupError(f"Could not inspect running proxy services{suffix}")
    return {line.strip() for line in str(result.stdout or "").splitlines() if line.strip()}


def _reload_proxies(
    command_runner,
    project_root: Path,
    compose_files: Sequence[Path],
    environment: Mapping[str, str] | None,
    mounts: Sequence[tuple[str, Path, str, str]],
    running: set[str],
) -> tuple[list[str], list[str]]:
    reloaded = []
    restarted = []
    for service, source, target, kind in mounts:
        if service not in running:
            continue
        template_backed_nginx = kind == "nginx" and (
            source.name.endswith(".template") or "/templates/" in target
        )
        if template_backed_nginx:
            action = ["restart", service]
        elif kind == "caddy":
            action = [
                "exec",
                "-T",
                service,
                "caddy",
                "reload",
                "--config",
                target,
                "--adapter",
                "caddyfile",
            ]
        else:
            action = ["exec", "-T", service, "nginx", "-s", "reload"]
        result = _run(
            command_runner,
            _compose_args(project_root, compose_files, *action),
            project_root,
            environment,
        )
        if result.returncode != 0:
            detail = str(result.stderr or result.stdout or "").strip()[:1000]
            suffix = f": {detail}" if detail else ""
            verb = "restart" if template_backed_nginx else "reload"
            raise StackCleanupError(f"Could not {verb} {service}{suffix}")
        if template_backed_nginx:
            verification = _run(
                command_runner,
                _compose_args(
                    project_root,
                    compose_files,
                    "exec",
                    "-T",
                    service,
                    "nginx",
                    "-t",
                ),
                project_root,
                environment,
            )
            if verification.returncode != 0:
                detail = str(
                    verification.stderr or verification.stdout or ""
                ).strip()[:1000]
                suffix = f": {detail}" if detail else ""
                raise StackCleanupError(
                    f"{service} failed its post-restart configuration check{suffix}"
                )
            restarted.append(service)
        else:
            reloaded.append(service)
    return sorted(set(reloaded)), sorted(set(restarted))


def remove_obsolete_services(
    project_dir: str,
    compose_files: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
    command_runner=subprocess.run,
) -> Dict[str, Any]:
    project_root = Path(project_dir).resolve()
    sources = [_project_file(project_root, value) for value in compose_files]
    originals: Dict[Path, str] = {}
    compose_updates: Dict[Path, str] = {}
    removed: set[str] = set()
    for source in sources:
        contents = source.read_text(encoding="utf-8")
        originals[source] = contents
        updated, file_removed = remove_obsolete_service_blocks(contents)
        if file_removed:
            compose_updates[source] = updated
            removed.update(file_removed)
    proxy_updates, unsupported_proxy_files = proxy_route_updates(project_root)
    if unsupported_proxy_files:
        raise StackCleanupError(
            "Refusing to rewrite unrecognized pgAdmin proxy routes: "
            + ", ".join(unsupported_proxy_files)
        )
    for path in proxy_updates:
        originals[path] = path.read_text(encoding="utf-8")
    updates = {**compose_updates, **proxy_updates}

    result: Dict[str, Any] = {
        "applied": False,
        "files": [str(path.relative_to(project_root)) for path in updates],
        "removed_services": sorted(removed),
        "proxy_files": sorted(
            str(path.relative_to(project_root)) for path in proxy_updates
        ),
        "backup_root": "",
        "container_cleanup_applied": False,
        "postflight_verified": False,
        "preserved_volumes": [],
        "proxy_candidates_validated": [],
        "proxy_services_reloaded": [],
        "proxy_services_restarted": [],
    }
    if not updates:
        return result
    if not shutil.which("docker"):
        raise StackCleanupError("Docker is required to validate the cleaned Compose configuration.")

    probe = _run(
        command_runner,
        ["docker", "compose", "version"],
        project_root,
        environment,
    )
    if probe.returncode != 0:
        raise StackCleanupError("Docker Compose v2 is required to remove obsolete services.")

    original_model = _compose_model(
        command_runner,
        project_root,
        sources,
        environment,
        "Original configuration",
    )
    original_services = original_model.get("services")
    original_service_names = (
        set(original_services) if isinstance(original_services, dict) else set()
    )
    missing_original = sorted(removed - original_service_names)
    if missing_original:
        raise StackCleanupError(
            "The original Compose model does not contain: " + ", ".join(missing_original)
        )
    original_volumes = _declared_volumes(original_model)
    original_volume_declarations = set().union(
        *(
            _top_level_mapping_keys(originals[source], "volumes")
            for source in sources
        )
    )
    volumes_before = _existing_docker_volumes(
        command_runner,
        project_root,
        environment,
    )
    proxy_mounts = _active_proxy_mounts(
        original_model,
        project_root,
        proxy_updates,
    )

    staged: Dict[Path, Path] = {}
    try:
        for source, contents in updates.items():
            staged[source] = _stage_candidate(source, contents)
        candidate_files = [staged.get(source, source) for source in sources]
        candidate_model = _compose_model(
            command_runner,
            project_root,
            candidate_files,
            environment,
            "Cleaned candidate",
        )
        result["proxy_candidates_validated"] = _validate_proxy_candidates(
            command_runner,
            project_root,
            candidate_files,
            environment,
            proxy_mounts,
            staged,
        )
        running_services = (
            _running_services(
                command_runner,
                project_root,
                sources,
                environment,
            )
            if proxy_mounts
            else set()
        )
    except (OSError, StackCleanupError) as exc:
        archive = _archive_root(project_root)
        _archive_staged(project_root, staged, archive, "rejected")
        raise StackCleanupError(
            f"Could not validate the cleaned Compose configuration; no project files were changed: {exc}"
        ) from exc

    candidate_services = candidate_model.get("services")
    candidate_service_names = (
        set(candidate_services) if isinstance(candidate_services, dict) else set()
    )
    remaining = sorted(removed.intersection(candidate_service_names))
    candidate_volume_declarations = set().union(
        *(
            _top_level_mapping_keys(
                compose_updates.get(source, originals[source]),
                "volumes",
            )
            for source in sources
        )
    )
    missing_volume_declarations = sorted(
        original_volume_declarations - candidate_volume_declarations
    )
    if remaining or missing_volume_declarations:
        archive = _archive_root(project_root)
        _archive_staged(project_root, staged, archive, "rejected")
        details = []
        if remaining:
            details.append("obsolete services remain: " + ", ".join(remaining))
        if missing_volume_declarations:
            details.append(
                "named volume declarations would be removed: "
                + ", ".join(missing_volume_declarations)
            )
        raise StackCleanupError(
            "Cleaned candidate failed safety checks; no project files were changed: "
            + "; ".join(details)
        )

    archive = _archive_root(project_root)
    result["backup_root"] = str(archive)
    for source in updates:
        relative = source.relative_to(project_root)
        backup = archive / "original" / relative
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, backup)

    if removed:
        try:
            cleanup = _run(
                command_runner,
                _compose_args(
                    project_root,
                    sources,
                    "rm",
                    "-s",
                    "-f",
                    *sorted(removed),
                ),
                project_root,
                environment,
            )
        except StackCleanupError:
            _archive_staged(project_root, staged, archive, "unapplied")
            raise
        if cleanup.returncode != 0:
            _archive_staged(project_root, staged, archive, "unapplied")
            detail = str(cleanup.stderr or cleanup.stdout or "").strip()[:1000]
            suffix = f": {detail}" if detail else ""
            raise StackCleanupError(
                f"Targeted obsolete-container removal failed; project files were not changed{suffix}"
            )
        result["container_cleanup_applied"] = True

    try:
        for source, candidate in staged.items():
            candidate.replace(source)
    except OSError as exc:
        _archive_staged(project_root, staged, archive, "unapplied")
        raise StackCleanupError(
            f"Could not replace every Compose file; originals are preserved under {archive}: {exc}"
        ) from exc

    try:
        post_model = _compose_model(
            command_runner,
            project_root,
            sources,
            environment,
            "Post-fix configuration",
        )
        post_services = post_model.get("services")
        post_service_names = set(post_services) if isinstance(post_services, dict) else set()
        remaining = sorted(removed.intersection(post_service_names))
        post_volume_declarations = set().union(
            *(
                _top_level_mapping_keys(
                    source.read_text(encoding="utf-8"),
                    "volumes",
                )
                for source in sources
            )
        )
        missing_volume_declarations = sorted(
            original_volume_declarations - post_volume_declarations
        )
        volumes_after = _existing_docker_volumes(
            command_runner,
            project_root,
            environment,
        )
        proxy_inspection = inspect_legacy_proxy_routes(str(project_root))
    except (OSError, StackCleanupError) as exc:
        raise StackCleanupError(
            f"Post-fix verification failed; restore from {archive}: {exc}"
        ) from exc
    expected_existing = set(original_volumes.values()).intersection(volumes_before)
    missing_runtime_volumes = sorted(expected_existing - volumes_after)
    remaining_proxy_files = (
        proxy_inspection["recognized"] + proxy_inspection["unsupported"]
    )
    if (
        remaining
        or missing_volume_declarations
        or missing_runtime_volumes
        or remaining_proxy_files
    ):
        details = []
        if remaining:
            details.append("obsolete services remain: " + ", ".join(remaining))
        if missing_volume_declarations:
            details.append(
                "named volume declarations disappeared: "
                + ", ".join(missing_volume_declarations)
            )
        if missing_runtime_volumes:
            details.append(
                "existing Docker volumes disappeared: " + ", ".join(missing_runtime_volumes)
            )
        if remaining_proxy_files:
            details.append(
                "pgAdmin proxy routes remain: " + ", ".join(remaining_proxy_files)
            )
        raise StackCleanupError(
            "Post-fix verification failed; restore from "
            + str(archive)
            + ": "
            + "; ".join(details)
        )

    try:
        reloaded, restarted = _reload_proxies(
            command_runner,
            project_root,
            sources,
            environment,
            proxy_mounts,
            running_services,
        )
        result["proxy_services_reloaded"] = reloaded
        result["proxy_services_restarted"] = restarted
    except StackCleanupError as exc:
        raise StackCleanupError(
            f"Post-fix proxy reload failed; restore from {archive}: {exc}"
        ) from exc

    result["applied"] = True
    result["postflight_verified"] = True
    result["preserved_volumes"] = sorted(expected_existing)
    return result
