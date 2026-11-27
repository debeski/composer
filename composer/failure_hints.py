"""Name the likely cause of a service that failed a deploy, and the next step.

A failed health check used to end with a state line and raw log lines, which
left the operator to work out why. The patterns here cover the failures seen on
DjangoLux stacks; anything else gets only the generic next steps.
"""
import re
from typing import Iterable, List, Optional

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
#: Compose's log prefix, ``worker-1  | ``.
_LOG_PREFIX = re.compile(r"^[A-Za-z0-9_.-]+-\d+\s+\|\s?")
#: Python's traceback marker lines (``~~~~^^``): they point at text, they say nothing.
_CARET = re.compile(r"^[\s~^]*$")
_FRAME = re.compile(r'^\s*(File "|Traceback \(most recent call last\)|\.\.\.)')
_EXCEPTION = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Exit|Interrupt|Warning|Failure)\b")
#: Compose's message for a failed lifecycle hook; the tail is the hook's last output line.
_HOOK = re.compile(r'service "([^"]+)" (pre_start|post_start)\[\d+\] exited with code (\d+)(?::[ \t]*(.*))?$')

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


def _clean(line: str) -> str:
    return _LOG_PREFIX.sub("", _ANSI.sub("", line)).strip()


def uninformative(line: str) -> bool:
    """A blank line, a traceback caret line or a traceback frame line."""
    line = _clean(line)
    return not line or bool(_CARET.match(line)) or bool(_FRAME.match(line))


def meaningful_line(text: str) -> str:
    """The last line of ``text`` that says what went wrong (R2-002, R2-126).

    The last exception line (``AttributeError: …``) when there is one, else the
    last line that is neither blank, a caret line (``~~~~^^``) nor a frame line.
    """
    lines = [_clean(line) for line in (text or "").splitlines()]
    for line in reversed(lines):
        if _EXCEPTION.match(line):
            return line
    for line in reversed(lines):
        if not uninformative(line):
            return line
    return ""


def hook_failures(text: str) -> List[str]:
    """Services whose Compose ``pre_start``/``post_start`` hook failed, in order."""
    found: List[str] = []
    for line in (text or "").splitlines():
        match = _HOOK.search(_clean(line))
        if match and match.group(1) not in found:
            found.append(match.group(1))
    return found


def clarify_hook_lines(text: str) -> str:
    """Give each failed-hook line the hook's meaningful line instead of a caret or nothing.

    Compose quotes only the hook's last output line, which for a Python
    traceback is the caret line under the failing call. The meaningful line is
    taken from what follows in ``text`` (Compose and the logs print it there).
    """
    lines = (text or "").splitlines()
    result = []
    for index, line in enumerate(lines):
        match = _HOOK.search(_clean(line))
        if match and uninformative(match.group(4) or ""):
            reason = meaningful_line("\n".join(lines[index + 1:index + 40]))
            head = line[:line.find(match.group(0)) + len(match.group(0)) - len(match.group(4) or "")].rstrip()
            head = head[:-1] if head.endswith(":") else head
            line = f"{head}: {reason}" if reason else head
        result.append(line)
    return "\n".join(result)
