"""The DjangoLux maintenance page in a project's ``.proxy/``.

DjangoLux scaffolds ``.proxy/maintenance.html`` once; the proxy bind-mounts it,
so a DjangoLux release never reaches it. Pages scaffolded before DjangoLux
1.11.0b2 redirect to ``/`` only after seeing a progress phase, so a visitor whose
first status read is ``ready`` waits on the page forever.

``check`` compares the page with the stock pages DjangoLux has shipped. A stock
copy is replaced with the bundled current page (``assets/maintenance.html.tmpl``,
a mirror of DjangoLux's scaffold template); a customised page that still carries
the old logic is reported for a manual edit and never touched.
"""
import hashlib
import shutil
from pathlib import Path
from typing import Dict, Optional

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


def install(project_root: Path, schema: Optional[int], archive_dir: Path) -> None:
    """Archive the page, then rewrite it in place.

    In place, not ``os.replace``: the proxy bind-mounts this single file, and a
    file bind mount keeps the inode it was created with, so a renamed-over page
    would not be served until the proxy container was recreated.
    """
    path = project_root / PAGE
    destination = archive_dir / PAGE
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)
    with path.open("r+", encoding="utf-8", newline="\n") as stream:
        stream.seek(0)
        stream.write(render(schema))
        stream.truncate()
