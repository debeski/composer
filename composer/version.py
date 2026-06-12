from pathlib import Path

from .constants import DEFAULT_COMPOSER_VERSION, VERSION_FILE_NAME


def read_composer_version() -> str:
    version_path = Path(__file__).resolve().parent.parent / VERSION_FILE_NAME
    try:
        version = version_path.read_text(encoding="utf-8").strip()
    except OSError:
        return DEFAULT_COMPOSER_VERSION
    return version or DEFAULT_COMPOSER_VERSION
