"""Name the likely cause of a service that failed a deploy, and the next step.

A failed health check used to end with a state line and raw log lines, which
left the operator to work out why. The patterns here cover the failures seen on
DjangoLux stacks; anything else gets only the generic next steps.
"""
import re
from typing import Iterable, List, Optional

_CAUSES = (
    (
        re.compile(r"No module named '?([A-Za-z0-9_.]+)'?"),
        lambda m, svc: (
            f"its image has no Python module '{m.group(1)}'. If {svc} is left over from "
            "an older release (Decrees 2.2 dropped Celery), remove it from compose.yml or "
            "run `./start.sh check --fix`; otherwise rebuild the image with that module."
        ),
    ),
    (
        re.compile(r"exec format error", re.I),
        lambda m, svc: "its image was built for another CPU architecture than this host.",
    ),
    (
        re.compile(r"executable file not found|exec: .*: not found|No such file or directory: '?/", re.I),
        lambda m, svc: "its command or entrypoint does not exist in the image.",
    ),
    (
        re.compile(r"address already in use", re.I),
        lambda m, svc: "a port it listens on is already taken by another process or container.",
    ),
    (
        re.compile(r"(could not connect to server|connection to server at .* failed|OperationalError).*", re.I),
        lambda m, svc: "it cannot reach the database: check that `db` is healthy and the POSTGRES_* values match.",
    ),
    (
        re.compile(r"(Error \d+ connecting to|ConnectionError).*redis|redis.*(Connection refused|ConnectionError)", re.I),
        lambda m, svc: "it cannot reach Redis: check that `redis` is healthy and running.",
    ),
    (
        re.compile(r"relation \"[^\"]+\" does not exist|no such table|column .* does not exist", re.I),
        lambda m, svc: (
            "the database schema does not match this code: migrations have not run, or the "
            "image is older than the database."
        ),
    ),
    (
        re.compile(r"ImproperlyConfigured|SECRET_KEY", re.I),
        lambda m, svc: "its settings are incomplete: check the secrets file (.secrets/.env).",
    ),
    (
        re.compile(r"Permission denied", re.I),
        lambda m, svc: (
            "it cannot read or write a file it needs. For a non-root DjangoLux image, "
            "`./start.sh check --fix` grants access (app-permissions)."
        ),
    ),
)


def likely_cause(service: str, logs: str, exit_code: Optional[str] = None) -> str:
    """One sentence on why ``service`` failed, or "" when nothing matches."""
    for pattern, explain in _CAUSES:
        match = pattern.search(logs or "")
        if match:
            return f"Likely cause: {explain(match, service)}"
    if str(exit_code or "").strip() == "137":
        return "Likely cause: it was killed (exit 137), usually for running out of memory."
    return ""


def next_steps(services: Iterable[str]) -> str:
    """What the operator can do now about the failed ``services``."""
    names: List[str] = [name for name in dict.fromkeys(services) if name]
    if not names:
        return ""
    joined = ",".join(names)
    return (
        "Next steps:\n"
        f"- read more of the log: ./start.sh logs {' '.join(names)}\n"
        f"- to deploy without {'it' if len(names) == 1 else 'them'} while you fix it, add "
        f"COMPOSER_EXCLUDE_SERVICES={joined} to the secrets file (.secrets/.env); Composer then "
        "leaves it out of `up` and the health check"
    )
