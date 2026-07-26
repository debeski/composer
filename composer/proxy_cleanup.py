import re
from pathlib import Path
from typing import Dict


PROXY_ROUTE_FILES = {
    Path(".proxy/Caddyfile"): "caddy",
    Path(".proxy/default.conf.template"): "nginx",
    Path(".nginx/nginx.conf"): "nginx",
}

_HEADERS = {
    "caddy": re.compile(r"^\s*handle_path\s+/pgadmin4/\*\s*\{\s*$"),
    "nginx": re.compile(r"^\s*location(?:\s+\^~)?\s+/pgadmin4/\s*\{\s*$"),
}
_REQUIRED = {
    "caddy": ("reverse_proxy pgadmin:80", "x-script-name /pgadmin4"),
    "nginx": ("proxy_pass http://pgadmin:80", "x-script-name /pgadmin4"),
}


def _block_end(lines: list[str], start: int) -> int:
    depth = 0
    opened = False
    for index in range(start, len(lines)):
        code = lines[index].split("#", 1)[0]
        depth += code.count("{")
        if "{" in code:
            opened = True
        depth -= code.count("}")
        if opened and depth == 0:
            return index + 1
    return -1


def _comment_start(lines: list[str], block_start: int) -> int:
    start = block_start
    found = False
    for index in range(block_start - 1, max(-1, block_start - 5), -1):
        stripped = lines[index].strip()
        if not stripped:
            if found:
                start = index
            continue
        if not stripped.startswith("#"):
            break
        if "pgadmin" in stripped.lower():
            found = True
        if found or set(stripped) <= {"#", "-", " "}:
            start = index
    return start if found else block_start


def remove_pgadmin_proxy_route(contents: str, kind: str) -> tuple[str, bool, bool]:
    lines = contents.splitlines(keepends=True)
    matches = [
        index
        for index, line in enumerate(lines)
        if _HEADERS[kind].match(line.rstrip("\r\n"))
    ]
    if not matches:
        return contents, False, False

    ranges = []
    unsupported = False
    for start in matches:
        end = _block_end(lines, start)
        if end < 0:
            unsupported = True
            continue
        body = "".join(lines[start:end]).lower()
        if not all(marker in body for marker in _REQUIRED[kind]):
            unsupported = True
            continue
        ranges.append((_comment_start(lines, start), end))

    if unsupported or len(ranges) != len(matches):
        return contents, False, True

    skipped = {
        index
        for range_start, range_end in ranges
        for index in range(range_start, range_end)
    }
    updated = "".join(line for index, line in enumerate(lines) if index not in skipped)
    return updated, bool(ranges), False


def inspect_legacy_proxy_routes(project_dir: str = ".") -> Dict[str, list[str]]:
    project_root = Path(project_dir).resolve()
    recognized = []
    unsupported = []
    for relative, kind in PROXY_ROUTE_FILES.items():
        path = project_root / relative
        if not path.is_file():
            continue
        contents = path.read_text(encoding="utf-8")
        _, changed, unsafe = remove_pgadmin_proxy_route(contents, kind)
        if changed:
            recognized.append(str(relative))
        elif unsafe:
            unsupported.append(str(relative))
    return {
        "recognized": sorted(recognized),
        "unsupported": sorted(unsupported),
    }


def proxy_route_updates(project_root: Path) -> tuple[Dict[Path, str], list[str]]:
    updates = {}
    unsupported = []
    for relative, kind in PROXY_ROUTE_FILES.items():
        path = project_root / relative
        if not path.is_file():
            continue
        contents = path.read_text(encoding="utf-8")
        updated, changed, unsafe = remove_pgadmin_proxy_route(contents, kind)
        if changed:
            updates[path.resolve()] = updated
        elif unsafe:
            unsupported.append(str(relative))
    return updates, sorted(unsupported)
