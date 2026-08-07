import re

IDLE = "idle"
RUNNING = "running"
OK = "ok"
ERROR = "error"

SERVICE_NOT_SEEN = "not_seen"
SERVICE_STARTING = "starting"
SERVICE_HEALTHY = "healthy"
SERVICE_FAILED = "failed"
# In flight: the container is being replaced, so the health it reported before
# the update says nothing about the deployment being applied.
SERVICE_UPDATING = "updating"

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
DEFAULT_RESIDENT_SERVICE = "composer-updater"
# The privileged Docker-authority role; the network-facing agent holds none.
EXECUTOR_SERVICE = "composer-executor"
INHERITED_SECRET_KEYS_ENV = "COMPOSER_INHERITED_SECRET_KEYS"
# Service label naming a command composer runs once the stack is healthy. Compose
# ignores it, unlike a native post_start hook, so there is exactly one runner.
POST_START_LABEL = "org.dlux.post-start"
DEFAULT_MIGRATOR_SERVICE = "web"
DEFAULT_MIGRATOR_COMMAND = (
    "python -m dlux.updater.supervisor --no-watch -- python manage.py migrator"
)
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
