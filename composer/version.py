import json
from pathlib import Path

from .constants import DEFAULT_COMPOSER_VERSION, VERSION_FILE_NAME

ROOT = Path(__file__).resolve().parent.parent
RELEASE_MANIFEST_NAME = "release-manifest.json"
RELEASE_MANIFEST_LABEL = "org.dlux.composer.release-manifest"


def read_release_manifest() -> dict:
    try:
        data = json.loads((ROOT / RELEASE_MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_composer_version() -> str:
    """The manifest's version; the image's derived VERSION file as a fallback."""
    version = str(read_release_manifest().get("version") or "").strip()
    if version:
        return version
    try:
        version = (ROOT / VERSION_FILE_NAME).read_text(encoding="utf-8").strip()
    except OSError:
        return DEFAULT_COMPOSER_VERSION
    return version or DEFAULT_COMPOSER_VERSION
