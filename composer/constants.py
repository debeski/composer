import re

IDLE = "idle"
RUNNING = "running"
OK = "ok"
ERROR = "error"

SERVICE_NOT_SEEN = "not_seen"
SERVICE_STARTING = "starting"
SERVICE_HEALTHY = "healthy"
SERVICE_FAILED = "failed"

ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
ERROR_KEYWORDS = (
    "error",
    "failed",
    "denied",
    "exception",
    "traceback",
    "invalid",
    "not found",
    "exit code",
    "exited with code",
    "no such",
    "unhealthy",
    "permission",
)
PROGRESS_KEYWORDS = (
    "building",
    "pulling",
    "creating",
    "created",
    "starting",
    "started",
    "waiting",
    "healthy",
    "built",
    "loaded",
    "exporting",
    "extracting",
    "downloading",
    "transferring",
)

VERSION_FILE_NAME = "VERSION"
DEFAULT_COMPOSER_VERSION = "0.0.0"
