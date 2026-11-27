"""The maintenance page: published by Composer, or legacy in a project's ``.proxy/``.

Since Composer 1.7 (DjangoLux stack schema 3) the page ships in this image
(``assets/maintenance/``) and the agent publishes it to the runtime volume at
``proxy/maintenance/``, which the proxy already mounts read-only. A project that
wants its own page keeps it in ``.proxy/maintenance/``; ``publish_override``
copies it there and marks it so the agent leaves it alone.

Before that, DjangoLux scaffolded ``.proxy/maintenance.html`` once; the proxy bind-mounts it,
so a DjangoLux release never reaches it. Pages scaffolded before DjangoLux
1.11.0b2 redirect to ``/`` only after seeing a progress phase, so a visitor whose
first status read is ``ready`` waits on the page forever.

``check`` compares the page with the stock pages DjangoLux has shipped. A stock
copy is replaced with the bundled current page (``assets/maintenance.html.tmpl``,
a mirror of DjangoLux's scaffold template); a customised page that still carries
the old logic is reported for a manual edit and never touched.
"""
import hashlib
import os
import stat
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import request_files, safe_files
from .stack_schema import read_stamp

PAGE = Path(".proxy") / "maintenance.html"
TEMPLATE = Path(__file__).resolve().parent / "assets" / "maintenance.html.tmpl"
_STAMP_LINE = "<!-- dlux stack schema {{ stack_schema }} -->\n"

# sha256 of each stock page DjangoLux shipped before the fix, as deployed.
STALE_STOCK = frozenset({
    "7b8f7c617277dfd928944d8de1a9a6e0b89873cd8b5df7752b3f7b5cb55b102c",  # .nginx era
    "c7293a09c1fe995712a14c83b09e55b0cf08c31d903a2915b00b3f3ee9f092e6",
    "cf3723319f32de314ce0ad241698f7b121b185591fe2163c126a0f40d36d6ea9",
    "25851ae234f78122755d13ede132d5e00959e3e9b7c318eaf9bd276527129ffa",  # 1.11.0b1, schema 2
})

# The pre-fix end-state logic: wait for a progress phase, then go to "/".
_STUCK_MARKERS = ("sawProgress", "readyCount")

CURRENT = "current"
STALE_STOCK_PAGE = "stale_stock"
CUSTOM_STUCK = "custom_stuck"
CUSTOM = "custom"
MISSING = "missing"


def render(schema: Optional[int]) -> str:
    """The bundled page, stamped with ``schema``; unstamped when it is None, so
    replacing the page never makes an unstamped stack look half-stamped."""
    text = TEMPLATE.read_text(encoding="utf-8")
    if schema is None:
        return text.replace(_STAMP_LINE, "", 1)
    return text.replace("{{ stack_schema }}", str(schema))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def inspect(project_root: Path) -> Dict[str, str]:
    path = project_root / PAGE
    if not path.is_file():
        return {"state": MISSING, "path": str(PAGE)}
    text = path.read_text(encoding="utf-8", errors="replace")
    stamp = read_stamp(text)
    if text in (render(stamp), render(None)):
        return {"state": CURRENT, "path": str(PAGE)}
    if _sha256(text) in STALE_STOCK:
        return {"state": STALE_STOCK_PAGE, "path": str(PAGE)}
    if all(marker in text for marker in _STUCK_MARKERS):
        return {"state": CUSTOM_STUCK, "path": str(PAGE)}
    return {"state": CUSTOM, "path": str(PAGE)}


# --- Composer-owned page (stack schema 3) -------------------------------------

BUNDLE = Path(__file__).resolve().parent / "assets" / "maintenance"
PUBLISHED = Path("proxy") / "maintenance"
OVERRIDE = Path(".proxy") / "maintenance"
CUSTOM_MARKER = ".custom"
CUSTOM_MARKER_TEXT = b"project override\n"
BUNDLE_FILES = ("index.html", "maintenance.css", "maintenance.js")

#: The published page is served to anonymous visitors. Only Composer writes it:
#: the directory is 0755 and its files 0644, so a non-root app service, which
#: shares group 0 on the runtime volume, cannot change it.
PUBLISHED_DIR_MODE = 0o755
PUBLISHED_FILE_MODE = 0o644
#: What a project page may publish: the page, its styles, scripts, images, fonts.
OVERRIDE_SUFFIXES = frozenset({
    ".html", ".htm", ".css", ".js", ".mjs", ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".webp", ".avif", ".ico", ".woff", ".woff2", ".ttf", ".otf",
})
MAX_OVERRIDE_FILE_BYTES = 1024 * 1024
MAX_OVERRIDE_FILES = 64
MAX_OVERRIDE_BYTES = 8 * 1024 * 1024
#: A temp file another publisher is writing right now is left alone this long.
_TEMP_GRACE_SECONDS = 60.0
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
_logged_refusals: set = set()


class OverrideRefused(OSError):
    """A project page that cannot be published as it is; ``problems`` says why."""

    def __init__(self, problems: List[str]):
        self.problems = list(problems)
        super().__init__("The project maintenance page was not published: " + "; ".join(self.problems))


def published_dir(runtime_root: Path) -> Path:
    return Path(runtime_root) / PUBLISHED


def read_override(source: Path, project_root: Optional[Path] = None) -> Tuple[Dict[str, bytes], List[str]]:
    """``(files, skipped)`` of a project page in ``source``, read without following links.

    Raises ``OverrideRefused`` for a symlink, a FIFO or other special file, a file
    over 1 MiB, more than 64 files or 8 MiB in all, a page without
    ``index.html``, or a folder that is itself a link or (with ``project_root``)
    resolves outside the project. A refusal publishes nothing. Dotfiles,
    subfolders and other file types are skipped and named in ``skipped``.
    """
    source = Path(source)
    try:
        info = os.lstat(source)
    except OSError as exc:
        raise OverrideRefused([f"{source} cannot be read ({exc.strerror or exc})"]) from None
    if not stat.S_ISDIR(info.st_mode):
        raise OverrideRefused([f"{source} is not a folder (a link to one is refused)"])
    if project_root is not None:
        root = Path(project_root).resolve()
        resolved = source.resolve()
        if resolved != root and root not in resolved.parents:
            raise OverrideRefused([f"{source} resolves outside the project"])
    problems: List[str] = []
    skipped: List[str] = []
    files: Dict[str, bytes] = {}
    total = 0
    directory = os.open(source, _DIR_FLAGS)
    try:
        for name in sorted(os.listdir(directory)):
            try:
                entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(entry.st_mode):
                problems.append(f"{name} is a symbolic link")
                continue
            if name.startswith("."):
                skipped.append(name)
                continue
            if stat.S_ISDIR(entry.st_mode):
                skipped.append(f"{name}/")
                continue
            if not stat.S_ISREG(entry.st_mode):
                problems.append(f"{name} is not a regular file")
                continue
            if Path(name).suffix.lower() not in OVERRIDE_SUFFIXES:
                skipped.append(name)
                continue
            if entry.st_size > MAX_OVERRIDE_FILE_BYTES:
                problems.append(f"{name} is larger than {MAX_OVERRIDE_FILE_BYTES} bytes")
                continue
            try:
                descriptor = os.open(name, _FILE_FLAGS, dir_fd=directory)
            except OSError as exc:
                problems.append(f"{name} cannot be read ({exc.strerror or exc})")
                continue
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode):
                    problems.append(f"{name} is not a regular file")
                    continue
                data = stream.read(MAX_OVERRIDE_FILE_BYTES + 1)
            if len(data) > MAX_OVERRIDE_FILE_BYTES:
                problems.append(f"{name} is larger than {MAX_OVERRIDE_FILE_BYTES} bytes")
                continue
            total += len(data)
            files[name] = data
    finally:
        os.close(directory)
    if len(files) > MAX_OVERRIDE_FILES:
        problems.append(f"it has more than {MAX_OVERRIDE_FILES} files")
    if total > MAX_OVERRIDE_BYTES:
        problems.append(f"it is larger than {MAX_OVERRIDE_BYTES} bytes in all")
    if "index.html" not in files and not problems:
        problems.append("it has no index.html")
    if problems:
        raise OverrideRefused(problems)
    return files, skipped


def _bundle() -> Dict[str, bytes]:
    return {name: (BUNDLE / name).read_bytes() for name in BUNDLE_FILES}


def _log_once(message: str) -> None:
    if message in _logged_refusals:
        return
    _logged_refusals.add(message)
    print(f"⚠ {message}", flush=True)


def _set_aside(path: Path) -> None:
    """Move an entry Composer did not publish out of the served folder."""
    holding = path.parent.parent / f"{path.parent.name}.refused"
    try:
        holding.mkdir(mode=0o700, exist_ok=True)
        os.replace(path, holding / f"{path.name}-{time.time_ns()}")
    except OSError as exc:
        _log_once(f"could not move {path} out of the published maintenance page: {exc}")


def _owned_directory(target: Path) -> Path:
    """``target`` as a real directory this process owns, 0755.

    The parent (``proxy/``) is app-writable, so an app service can swap the
    folder for its own or for a link; that one is moved aside and replaced.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        info = None
    if info is not None and not (stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()):
        aside = target.with_name(f"{target.name}.refused-{time.time_ns()}")
        os.replace(target, aside)
        _log_once(f"{target} was not Composer's folder; moved to {aside.name}")
        info = None
    if info is None:
        os.mkdir(target, PUBLISHED_DIR_MODE)
        info = os.lstat(target)
    if stat.S_IMODE(info.st_mode) != PUBLISHED_DIR_MODE:
        os.chmod(target, PUBLISHED_DIR_MODE)
    return target


def _current(path: Path) -> Optional[bytes]:
    """A published file's bytes when it is ours as written, else None (rewrite it)."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != PUBLISHED_FILE_MODE):
        return None
    got = request_files.read_bytes(path, MAX_OVERRIDE_FILE_BYTES, aside=False)
    return got[0] if got else None


def _mirror(target: Path, files: Dict[str, bytes]) -> bool:
    """Make ``target`` hold exactly ``files``; True when anything changed."""
    changed = False
    for name, data in files.items():
        if _current(target / name) == data:
            continue
        safe_files.atomic_write_bytes(target / name, data, mode=PUBLISHED_FILE_MODE)
        changed = True
    now = time.time()
    for name in os.listdir(target):
        if name in files:
            continue
        path = target / name
        if name.startswith(".") and name.endswith(".tmp"):
            try:
                if now - os.lstat(path).st_mtime < _TEMP_GRACE_SECONDS:
                    continue
            except OSError:
                continue
        _set_aside(path)
        changed = True
    return changed


def _legacy_override_in_place(target: Path) -> bool:
    """A page published from a copy (``publish --override``) where the project is not visible.

    Trusted only when Composer wrote the marker (owned by this process's uid).
    """
    try:
        info = os.lstat(target / CUSTOM_MARKER)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()


def publish(runtime_root: Path, project_root: Optional[Path] = None) -> bool:
    """Publish the page the proxy serves; True when a file changed.

    The source is the project's ``.proxy/maintenance/`` when it exists
    (``project_root``, by default the working directory, which is the project in
    ``composer-agent``), else the bundled page. Nothing on the runtime volume
    decides that: the ``.custom`` marker is written for ``check`` to read, never
    trusted, except where the project is not visible here at all. Files Composer
    did not publish are moved out of the served folder.
    """
    project = Path(project_root) if project_root is not None else Path.cwd()
    target = _owned_directory(published_dir(runtime_root))
    override = project / OVERRIDE
    if os.path.lexists(override):
        try:
            files, _skipped = read_override(override, project)
        except OverrideRefused as exc:
            _log_once(f"{exc}; serving Composer's own page")
        else:
            return _mirror(target, {**files, CUSTOM_MARKER: CUSTOM_MARKER_TEXT})
    elif not (project / ".proxy").is_dir() and _legacy_override_in_place(target):
        return False
    return _mirror(target, _bundle())


def publish_override(source: Path, runtime_root: Path) -> Tuple[int, List[str]]:
    """Publish a project's page from ``source``: ``(files published, skipped names)``.

    Raises ``OverrideRefused`` and publishes nothing when the folder holds a
    link or another file that must not reach visitors (``read_override``).
    """
    files, skipped = read_override(Path(source))
    target = _owned_directory(published_dir(runtime_root))
    _mirror(target, {**files, CUSTOM_MARKER: CUSTOM_MARKER_TEXT})
    return len(files), skipped


def is_published(runtime_root: Path) -> bool:
    return (published_dir(runtime_root) / "index.html").is_file()
