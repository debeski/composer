"""Redis keeps RDB snapshots, not an append-only file.

The DjangoLux scaffold ran Redis with ``--appendonly yes --appendfsync everysec``.
A hard power-off or a killed Docker Desktop can leave the append-only file
half-written; Redis then refuses to start ("Bad file format reading the append
only file"), restarts for good, and the app cannot reach its cache or sessions.
An RDB snapshot is written to a temporary file and renamed only when complete, so
a crash leaves the previous good one. The cost is up to about a minute of cache
and sessions after a crash; the application's data is in Postgres.

``check`` reads the resolved ``redis`` command (``classify``); ``check --fix``
rewrites it in the Compose file that sets it (``--appendonly no``, ``--save 60 1``
unless the command already has a ``--save``, every other flag kept), validated
and archived, and the lifecycle step recreates ``redis``. A Redis already
crash-looping on a damaged file has its append-only files moved aside, on its own
volume, by a one-off container with no network (``move_aside_argv``).
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from .stack_cleanup import (
    StackCleanupError,
    _archive_root,
    _compose_model,
    _project_file,
    _stage_candidate,
)
from .stack_schema import ensure_writable, preview_diff, write_updates
from .worker_migration import WorkerMigrationError, _KEY, _span, _split_block

SERVICE = "redis"
AOF, RDB, CUSTOM, DEFAULT = "aof", "rdb", "custom", "default"
SNAPSHOT = ("--save", "60", "1")
#: Flags that only mean something with an append-only file; dropped with it.
AOF_FLAGS = ("--appendfsync",)
ARCHIVE_DIR = "aof-archive"
DATA_DIR = "/data"

#: Redis's own words for an append-only file it cannot load (7.x and older).
AOF_ERROR = re.compile(
    r"bad file format reading the append only file"
    r"|unrecoverable error reading the append only file"
    r"|\baof\b[^\n]*\bis not valid"
    r"|invalid aof manifest|aof manifest[^\n]*(?:invalid|malformed|corrupt)"
    r"|redis-check-aof",
    re.IGNORECASE,
)

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
_PLAIN = re.compile(r"^[A-Za-z/_.-][A-Za-z0-9_./:=@+-]*$")
_YAML_WORDS = {"yes", "no", "y", "n", "true", "false", "on", "off", "null", "~"}


class RedisPersistenceError(StackCleanupError):
    """The redis command is not in a shape this rewrite recognizes."""


def command_tokens(command) -> Optional[List[str]]:
    """The command as a token list (a string is split like Compose splits it), or None."""
    if command is None:
        return None
    if isinstance(command, str):
        try:
            return shlex.split(command)
        except ValueError:
            return command.split()
    if isinstance(command, (list, tuple)):
        return [str(token) for token in command]
    return None


def _option(tokens: Sequence[str], name: str) -> Optional[str]:
    """The value after the last ``name`` in ``tokens``, or None."""
    value = None
    for index, token in enumerate(tokens):
        if token == name and index + 1 < len(tokens):
            value = tokens[index + 1]
    return value


def classify(command) -> str:
    """``aof``, ``rdb``, ``custom`` (a configuration file or another program) or ``default`` (no command)."""
    tokens = command_tokens(command)
    if not tokens:
        return DEFAULT
    if Path(tokens[0]).name != "redis-server":
        return CUSTOM
    value = _option(tokens, "--appendonly")
    if value is None:
        # redis-server's own default is off, but a configuration file can turn it on.
        return CUSTOM if any(token.endswith(".conf") for token in tokens[1:]) else RDB
    return AOF if value.strip().lower() == "yes" else RDB


def rdb_tokens(tokens: Sequence[str]) -> List[str]:
    """``tokens`` with the append-only file off and a snapshot every minute, every other flag kept."""
    result: List[str] = []
    skip = 0
    has_save = "--save" in tokens
    for index, token in enumerate(tokens):
        if skip:
            skip -= 1
            continue
        if token in AOF_FLAGS and index + 1 < len(tokens):
            skip = 1
            continue
        if token == "--appendonly" and index + 1 < len(tokens):
            result += ["--appendonly", "no"]
            if not has_save:
                result += list(SNAPSHOT)
                has_save = True
            skip = 1
            continue
        result.append(token)
    return result


def aof_names(tokens: Optional[Sequence[str]]) -> Tuple[str, str]:
    """``(append-only directory, append-only file)`` names under Redis's data directory."""
    tokens = list(tokens or [])
    names = (_option(tokens, "--appenddirname") or "appendonlydir", _option(tokens, "--appendfilename") or "appendonly.aof")
    for name in names:
        if not _SAFE_NAME.fullmatch(name) or name in (".", ".."):
            raise RedisPersistenceError(f"Composer does not move an append-only file named {name!r}; move it by hand.")
    return names


def data_dir(tokens: Optional[Sequence[str]]) -> str:
    return _option(list(tokens or []), "--dir") or DATA_DIR


# --- The Compose file -----------------------------------------------------------


def _quote(token: str) -> str:
    if _PLAIN.fullmatch(token) and token.lower() not in _YAML_WORDS:
        return token
    return json.dumps(token, ensure_ascii=False)


def _render(style: str, key_line: str, body: List[str], tokens: Sequence[str]) -> str:
    prefix = key_line[: key_line.index("command:")] + "command:"
    if style == "flow":
        return f"{prefix} [" + ", ".join(json.dumps(token, ensure_ascii=False) for token in tokens) + "]\n"
    if style == "string":
        text = shlex.join(tokens)
        quoted = key_line.split("command:", 1)[1].lstrip()[:1] in ("'", '"')
        return f"{prefix} {json.dumps(text) if quoted or ': ' in text or ' #' in text else text}\n"
    item = next((line for line in body[1:] if line.lstrip().startswith("-")), "      - ")
    indent = item[: len(item) - len(item.lstrip())]
    return f"{prefix}\n" + "".join(f"{indent}- {_quote(token)}\n" for token in tokens)


def _scalar(text: str) -> Tuple[str, str]:
    """``(value, rest)`` for one YAML scalar at the start of ``text``: quoted or plain."""
    text = text.lstrip()
    if text.startswith('"'):
        index, escaped = 1, False
        while index < len(text):
            char = text[index]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                return json.loads(text[: index + 1]), text[index + 1:]
            index += 1
        raise ValueError("an unterminated double-quoted string")
    if text.startswith("'"):
        index, out = 1, []
        while index < len(text):
            if text[index] == "'":
                if text[index + 1: index + 2] == "'":
                    out.append("'")
                    index += 2
                    continue
                return "".join(out), text[index + 1:]
            out.append(text[index])
            index += 1
        raise ValueError("an unterminated single-quoted string")
    return text, ""


def _strip_comment(text: str) -> str:
    match = re.search(r"(^|\s)#", text)
    return text[: match.start()].rstrip() if match else text.rstrip()


def parse_command(body: Sequence[str]) -> Union[str, List[str]]:
    """The ``command:`` value of a Compose service, without a YAML library.

    ``body`` starts with the ``command:`` line. Compose files write it as a flow
    list (``["redis-server", "--appendonly", "yes"]``, on one or more lines), a
    block list (``- redis-server`` items) or a plain or quoted string; anything
    else (block scalars, anchors, nested values) raises ``ValueError``. Every
    item comes back as a string, as YAML's base loader reads it.
    """
    after_key = body[0].split("command:", 1)[1].strip()
    if after_key.startswith(("|", ">", "&", "*", "!")):
        raise ValueError(f"an unsupported YAML form after 'command:' ({after_key[:1]})")
    if after_key.startswith("["):
        text = " ".join([after_key[1:]] + [line.strip() for line in body[1:]])
        items: List[str] = []
        while True:
            text = text.lstrip()
            if text.startswith("]"):
                return items
            if text.startswith(("'", '"')):
                value, text = _scalar(text)
            else:
                match = re.match(r"([^,\]]*)", text)
                value, text = match.group(1).strip(), text[match.end():]
                if not value:
                    raise ValueError("an empty item in the flow list")
            items.append(value)
            text = text.lstrip()
            if text.startswith(","):
                text = text[1:]
            elif not text.startswith("]"):
                raise ValueError("a flow list without its closing ']'")
    if after_key and not after_key.startswith("#"):
        stripped = after_key if after_key.startswith(("'", '"')) else _strip_comment(after_key)
        value, rest = _scalar(stripped)
        if rest.strip() and not rest.strip().startswith("#"):
            raise ValueError("text after the quoted command")
        return value
    items = []
    for line in body[1:]:
        content = line.strip()
        if not content or content.startswith("#"):
            continue
        if not content.startswith("- ") and content != "-":
            raise ValueError(f"an unexpected line in the command list: {content[:40]!r}")
        item = content[1:].strip()
        if item.startswith(("'", '"')):
            value, rest = _scalar(item)
            if rest.strip() and not rest.strip().startswith("#"):
                raise ValueError("text after a quoted list item")
        else:
            value = _strip_comment(item)
        items.append(value)
    if not items:
        raise ValueError("an empty command")
    return items


def rewrite_block(block: str) -> str:
    """One ``redis`` service block with its command on RDB snapshots, or the block unchanged."""
    try:
        header, entries, trailer = _split_block(block)
    except WorkerMigrationError as exc:
        raise RedisPersistenceError(f"Composer cannot edit the redis service's layout ({exc}).") from exc
    entry = next((item for item in entries if item[0] == "command"), None)
    if entry is None:
        return block
    lines = entry[1].splitlines(keepends=True)
    key_index = next(index for index, line in enumerate(lines) if _KEY.match(line))
    body = lines[key_index:]
    try:
        value = parse_command(body)
    except ValueError as exc:
        raise RedisPersistenceError(f"Composer cannot read the redis command ({exc}).") from exc
    tokens = command_tokens(value)
    if classify(tokens) != AOF:
        return block
    after_key = body[0].split("command:", 1)[1].strip()
    style = "string" if isinstance(value, str) else ("flow" if after_key.startswith("[") else "block")
    entry[1] = "".join(lines[:key_index]) + _render(style, body[0], body, rdb_tokens(tokens))
    return header + "".join(text for _key, text in entries) + trailer


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


def plan(project_root: Path, compose_files: Sequence[str], strict: bool = True):
    """``(sources, updates, notes)``: each Compose file whose redis command turns the append-only file on."""
    project_root = Path(project_root).resolve()
    sources = _sources(project_root, compose_files, strict)
    updates: Dict[Path, str] = {}
    notes: List[str] = []
    for path in sources:
        text = path.read_text(encoding="utf-8")
        span = _span(text, SERVICE)
        if span is None:
            continue
        block = text[span[0]:span[1]]
        try:
            rewritten = rewrite_block(block)
        except RedisPersistenceError as exc:
            notes.append(f"{path.relative_to(project_root)}: {exc}")
            continue
        if rewritten != block:
            updates[path] = text[:span[0]] + rewritten + text[span[1]:]
    return sources, updates, notes


NOTE = (
    "Redis then keeps RDB snapshots (--appendonly no --save 60 1, its other flags kept): a hard stop can no longer "
    "leave a half-written append-only file it refuses to start from. Up to about a minute of cache and sessions "
    "can be lost after a crash, so a few users may sign in again. redis is recreated after the repairs."
)


def preview(project_dir: str, compose_files: Sequence[str]) -> Dict[str, Any]:
    """What ``migrate`` would change, as unified diffs. Writes nothing."""
    project_root = Path(project_dir).resolve()
    _sources_, updates, notes = plan(project_root, compose_files, strict=False)
    if not updates:
        return {"files": [], "diff": "", "note": " ".join(notes)}
    return {
        "files": sorted(str(path.relative_to(project_root)) for path in updates),
        "diff": preview_diff(project_root, updates),
        "note": " ".join([NOTE] + notes),
    }


def migrate(
    project_dir: str,
    compose_files: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
    command_runner=None,
) -> Dict[str, Any]:
    """Rewrite the redis command: validated, the originals archived, then written. Recreates nothing."""
    command_runner = command_runner or subprocess.run
    project_root = Path(project_dir).resolve()
    sources, updates, notes = plan(project_root, compose_files)
    result: Dict[str, Any] = {
        "applied": False,
        "files": sorted(str(path.relative_to(project_root)) for path in updates),
        "backup_root": "",
        "notes": notes,
    }
    if not updates:
        return result
    ensure_writable(project_root, updates)
    if not shutil.which("docker"):
        raise RedisPersistenceError("Docker is required to validate the Compose configuration with the new redis command.")
    staged: Dict[Path, Path] = {}
    try:
        for path in sources:
            if path in updates:
                staged[path] = _stage_candidate(path, updates[path])
        model = _compose_model(
            command_runner, project_root, [staged.get(path, path) for path in sources],
            environment, "The Compose configuration with the new redis command",
        )
    finally:
        for candidate in staged.values():
            if candidate.exists():
                candidate.unlink()
    services = model.get("services") if isinstance(model.get("services"), dict) else {}
    redis = services.get(SERVICE) if isinstance(services.get(SERVICE), dict) else {}
    if classify(redis.get("command")) == AOF:
        raise RedisPersistenceError(
            "Another Compose file still turns redis's append-only file on; change its command there by hand."
        )
    archive = _archive_root(project_root)
    result["backup_root"] = str(archive)
    write_updates(project_root, updates, archive / "redis-persistence")
    result["applied"] = True
    return result


# --- A damaged append-only file -------------------------------------------------

#: Run as ``sh -c SCRIPT sh DATA STAMP DIRNAME FILENAME``: moves what exists into
#: ``DATA/aof-archive/STAMP/``, on the same volume. Nothing is deleted.
MOVE_ASIDE = """set -e
dest="$1/aof-archive/$2"
mkdir -p "$dest"
moved=""
for name in "$3" "$4"; do
  if [ -e "$1/$name" ]; then mv "$1/$name" "$dest/"; moved="$moved $name"; fi
done
echo "moved:$moved"
"""


def move_aside_argv(volume: str, image: str, stamp: str, target: str, tokens) -> List[str]:
    """``docker run`` argv that moves the append-only files on ``volume`` aside, with no network."""
    directory, filename = aof_names(command_tokens(tokens))
    if not _SAFE_NAME.fullmatch(stamp):
        raise RedisPersistenceError(f"Unexpected archive name {stamp!r}.")
    return [
        "docker", "run", "--rm", "--network", "none", "--pull", "never", "--entrypoint", "sh",
        "-v", f"{volume}:{target}", image,
        "-c", MOVE_ASIDE, "sh", target, stamp, directory, filename,
    ]


def moved_names(output: str) -> List[str]:
    for line in reversed(str(output or "").splitlines()):
        if line.startswith("moved:"):
            return line[len("moved:"):].split()
    return []


def data_volume(model: Mapping[str, Any], tokens) -> Tuple[str, str]:
    """``(Docker volume name, mount target)`` of the named volume redis keeps its data on, or ``("", "")``."""
    services = model.get("services") if isinstance(model.get("services"), dict) else {}
    redis = services.get(SERVICE) if isinstance(services.get(SERVICE), dict) else {}
    directory = data_dir(command_tokens(tokens))
    declared = model.get("volumes") if isinstance(model.get("volumes"), dict) else {}
    for mount in redis.get("volumes") or []:
        if not isinstance(mount, dict) or mount.get("type") != "volume" or mount.get("target") != directory:
            continue
        source = str(mount.get("source") or "")
        definition = declared.get(source) if isinstance(declared.get(source), dict) else {}
        return str(definition.get("name") or source), directory
    return "", ""
