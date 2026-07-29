from typing import List, Optional


# Services that must never be remotely restarted (data stores, the updater, and
# the resident composer roles that manage their own lifecycle). Shared by the
# agent and the executor so both enforce the same policy independently.
PROTECTED_RESTART_SERVICES = frozenset(
    {
        "db",
        "database",
        "postgres",
        "postgresql",
        "redis",
        "backup",
        "db-backup",
        "db_backup",
        "pgadmin",
        "dlux-updater",
        "composer-agent",
        "composer-executor",
        "composer-updater",
        "docker-socket-proxy",
    }
)


def parse_service_list(raw: Optional[str]) -> List[str]:
    names: List[str] = []
    for item in str(raw or "").replace(",", " ").split():
        name = item.strip()
        if name and name not in names:
            names.append(name)
    return names


def scoped_service_list(value) -> List[str]:
    """Normalize a service scope that may be a single name or a list of names."""
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [str(name) for name in value if name]
    return []


def join_service_list(names: List[str]) -> str:
    return ",".join(name for name in names if name)
