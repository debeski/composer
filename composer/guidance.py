"""The questions ``check --fix`` asks an operator at a terminal, and the summary it ends with.

Some findings are the operator's choice, not a defect Composer can repair alone:
which front proxies Caddy trusts (``DLUX_FRONT_PROXY``), which updates the app may
request (``DLUX_UPDATE_POLICY``), whether Redis keeps an append-only file. With a
terminal on stdin and without ``-y`` / ``COMPOSER_ASSUME_YES``, ``check --fix``
asks each as a short question whose default is the recommended answer. Anything
else (a script, the resident pair, an Operations panel apply, ``--json``) is never
asked and keeps the WARN with its advice. A skipped question changes nothing.

An answer that sets a value is written to the secrets file in place, like the
stamp repair: only that line changes (added at the end when it is missing), the
file keeps its owner, mode and line endings, and it is never copied, so no second
copy of the secrets outlives their rotation.
"""

from __future__ import annotations

import os
import re
import select
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .confirmation import assume_yes_from_env
from .stack_schema import secrets_file, write_updates

SKIP = "s"
ASK_AGAIN = "./start.sh check --fix asks again"

# Lifecycle fixes are what was done to the stack, not a finding made healthy.
_LIFECYCLE = {"fix:recreate", "fix:recreate-resident", "fix:remove-services", "fix:secrets"}
_START_SH = re.compile(r"'(\./start\.sh[^']*)'")
_SERVICE_PROBLEM = re.compile(r"(?:^|; )([A-Za-z0-9_.-]+) (?:keeps restarting|is unhealthy)")
_UNSAFE_VALUE = re.compile(r"[\r\n\"'#\\]")
#: How long a question waits for an answer before it counts as skipped.
QUESTION_TIMEOUT_ENV = "COMPOSER_QUESTION_TIMEOUT_SECONDS"
DEFAULT_QUESTION_TIMEOUT = 600.0
#: Words accepted for a one-letter answer.
_ALIASES = {"yes": "y", "no": "n", "skip": SKIP}


class QuestionTimeout(EOFError):
    """Nobody answered in time: the terminal may be gone while its client still runs (R2-017)."""


def question_timeout() -> float:
    raw = os.environ.get(QUESTION_TIMEOUT_ENV, "").strip()
    try:
        value = float(raw) if raw else DEFAULT_QUESTION_TIMEOUT
    except ValueError:
        value = DEFAULT_QUESTION_TIMEOUT
    return max(1.0, value)


def timed_input(prompt: str = "", *, stdin=None, timeout: Optional[float] = None) -> str:
    """``input()`` that gives up after ``question_timeout()`` seconds.

    A terminal that goes away while ``docker run -t`` keeps running never sends
    end of input, so a plain ``input()`` waited for good and held the operation
    lock (R2-017, R2-078). Where stdin cannot be waited on (Windows consoles),
    it is a plain ``input()``.
    """
    stream = sys.stdin if stdin is None else stdin
    try:
        fd = stream.fileno()
        select.select([fd], [], [], 0)
    except (AttributeError, ValueError, OSError, TypeError):
        return input(prompt)
    sys.stdout.write(prompt)
    sys.stdout.flush()
    ready, _, _ = select.select([fd], [], [], question_timeout() if timeout is None else timeout)
    if not ready:
        raise QuestionTimeout()
    line = stream.readline()
    if not line:
        raise EOFError()
    return line.rstrip("\r\n")


def _reader(reader):
    return timed_input if reader is None or reader is input else reader


def _gave_up(error: BaseException, out) -> None:
    if isinstance(error, QuestionTimeout):
        print(f"\n  No answer in {question_timeout():g}s: skipped, nothing changed.", file=out, flush=True)
    else:
        print("", file=out)


def interactive(args, *, resident: bool = False, stdin=None) -> bool:
    """May this ``check --fix`` ask questions? Only a terminal, never ``-y``, JSON or the resident pair."""
    if not getattr(args, "fix", False) or resident or getattr(args, "yes", False) or getattr(args, "json", False):
        return False
    if assume_yes_from_env():
        return False
    stream = sys.stdin if stdin is None else stdin
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def ask(
    question: str,
    options: Sequence[Tuple[str, str]],
    default: str,
    *,
    intro: Sequence[str] = (),
    skip: bool = True,
    reader: Optional[Callable[[str], str]] = None,
    stream=None,
) -> Optional[str]:
    """The key of the chosen option, ``default`` on Enter, None for a skip, end of input, no answer
    within ``question_timeout()`` or three wrong answers. ``yes``/``no`` answer ``y``/``n``."""
    out = stream or sys.stderr
    reader = _reader(reader)
    keys = [key for key, _label in options]
    lines = ["", f"? {question}", *(f"  {line}" for line in intro)]
    lines += [f"  [{key}] {label}" for key, label in options]
    if skip:
        lines.append(f"  [{SKIP}] skip: change nothing")
    print("\n".join(lines), file=out, flush=True)
    for _attempt in range(3):
        try:
            answer = str(reader(f"  Choice [{default}]: ")).strip().lower()
        except (EOFError, KeyboardInterrupt) as error:
            _gave_up(error, out)
            return None
        if not answer:
            return default
        if answer not in keys and _ALIASES.get(answer) in keys + ([SKIP] if skip else []):
            answer = _ALIASES[answer]
        if skip and answer == SKIP:
            return None
        if answer in keys:
            return answer
        print(f"  Answer {', '.join(keys)}{' or ' + SKIP if skip else ''}.", file=out)
    return None


def ask_text(prompt: str, check: Callable[[str], str], *, reader=None, stream=None) -> Optional[str]:
    """A typed value that ``check`` accepts (it returns an error, or ``""``); None when left empty or unanswered."""
    out = stream or sys.stderr
    reader = _reader(reader)
    for _attempt in range(3):
        try:
            value = " ".join(str(reader(prompt)).split())
        except (EOFError, KeyboardInterrupt) as error:
            _gave_up(error, out)
            return None
        if not value:
            return None
        problem = check(value)
        if not problem:
            return value
        print(f"  {problem}", file=out)
    return None


def ask_front_proxy(*, reader=None, stream=None) -> Optional[str]:
    """The ``DLUX_FRONT_PROXY`` value for the operator's answer, or None when skipped."""
    from . import front_proxy

    choice = ask(
        "How do users reach this server?",
        [
            ("1", "through a front proxy (a load balancer, pfSense, another nginx): give its address"),
            ("2", "directly, with no proxy in front: Caddy records the address that connects to it"),
            ("3", "keep trusting forwarded addresses from every private network (how it works now)"),
        ],
        "3",
        intro=[
            "Caddy takes the client address DjangoLux records (login lockout, audit log) from",
            "X-Forwarded-For. Trusting every private network lets a client on one choose its address.",
        ],
        reader=reader,
        stream=stream,
    )
    if choice == "1":
        def check(value: str) -> str:
            _entries, invalid = front_proxy.parse_ranges(value)
            if invalid:
                return f"Not an IP address or CIDR range: {', '.join(invalid)}. Separate entries with spaces."
            return ""

        return ask_text(
            "  Front proxy address(es), space-separated (10.0.0.5 or 10.0.0.0/24): ", check,
            reader=reader, stream=stream,
        )
    if choice == "2":
        return ""
    if choice == "3":
        return front_proxy.PRIVATE_RANGES
    return None


def ask_update_policy(current: str, *, reader=None, stream=None) -> Optional[str]:
    """The ``DLUX_UPDATE_POLICY`` value for the operator's answer (default: the policy in force), or None."""
    from . import update_policy

    options = [
        ("1", "all: DjangoLux inline updates, application image and Composer updates"),
        ("2", "inline: DjangoLux inline updates only"),
        ("3", "none: no updates from the app; you update from this shell"),
    ]
    by_key = {"1": update_policy.ALL, "2": update_policy.INLINE, "3": update_policy.NONE}
    default = next((key for key, value in by_key.items() if value == current), "1")
    choice = ask(
        "Which updates may the app request from its Updates card?",
        options,
        default,
        intro=["Checks, rollbacks and your own './start.sh update' stay allowed whatever you choose."],
        reader=reader,
        stream=stream,
    )
    return by_key.get(choice) if choice else None


def ask_redis(*, reader=None, stream=None) -> bool:
    """True to switch Redis to snapshots (the default), False to keep its append-only file."""
    choice = ask(
        "Redis keeps an append-only file, which a hard stop can leave half-written. Switch it to snapshots?",
        [
            ("y", "yes: RDB snapshots; up to a minute of cache and sessions is lost after a crash"),
            ("n", "no: keep the append-only file"),
        ],
        "y",
        skip=False,
        reader=reader,
        stream=stream,
    )
    return choice == "y"


def secret_values(root) -> Optional[Dict[str, str]]:
    """The secrets file's ``KEY=value`` lines as written (quotes kept), or None when there is none."""
    path = secrets_file(root)
    if path is None:
        return None
    values: Dict[str, str] = {}
    try:
        text = path.read_bytes().decode("utf-8", "surrogateescape")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def set_secret_values(root, values: Mapping[str, str]) -> Path:
    """Set ``KEY=value`` lines in the secrets file, in place; return its path.

    The last line that sets a key is replaced, a missing key is added at the end,
    and every other byte stays as it was (line endings included). A value with a
    quote, ``#``, a backslash or a line break is refused: ``./start.sh`` hands
    the file to Docker's ``--env-file``, which keeps them as part of the value.
    """
    from .stack_schema import line_ending

    path = secrets_file(root)
    if path is None:
        raise OSError("There is no secrets file (.env, secrets/.env or .secrets/.env) to write to.")
    for key, value in values.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or _UNSAFE_VALUE.search(str(value)):
            raise ValueError(f"Composer does not write {key} with that value; set it by hand.")
    newline = line_ending(path)
    lines = path.read_bytes().decode("utf-8", "surrogateescape").splitlines(keepends=True)
    for key, value in values.items():
        pattern = re.compile(rf"^([ \t]*(?:export[ \t]+)?){re.escape(key)}[ \t]*=")
        index = next((i for i in range(len(lines) - 1, -1, -1) if pattern.match(lines[i])), None)
        if index is None:
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += newline
            lines.append(f"{key}={value}{newline}")
            continue
        line = lines[index]
        body = line.rstrip("\r\n")
        prefix = pattern.match(line).group(1)
        lines[index] = f"{prefix}{key}={value}{line[len(body):] or ''}"
    write_updates(Path(root).resolve(), {path: "".join(lines)}, Path(root).resolve() / ".xclude" / "composer-check")
    return path


# --- The end-of-run summary -------------------------------------------------------


def next_command(entry: Mapping[str, Any]) -> str:
    """The one ``./start.sh`` command that is the operator's next step for ``entry``."""
    message = str(entry.get("message") or "")
    if entry.get("name") == "services":
        services = _SERVICE_PROBLEM.findall(message)
        if services:
            return "./start.sh logs " + " ".join(dict.fromkeys(services))
    text = f"{entry.get('fix') or ''} {message}"
    found = [match for match in _START_SH.finditer(text) if "<" not in match.group(1)]
    repair = text.find("check --fix")
    # The first step the text names comes first: "Run 'composer check --fix' …, then
    # './start.sh'" needs the repair before the restart.
    if repair >= 0 and (not found or repair < found[0].start()):
        return "./start.sh check --fix"
    if found:
        return found[0].group(1)
    return "./start.sh check"


def summary(
    results: Sequence[Mapping[str, Any]],
    fixes: Sequence[Mapping[str, Any]],
    skipped: Sequence[Mapping[str, str]],
    *,
    before: Optional[Mapping[str, str]] = None,
) -> Dict[str, List]:
    """``{"healthy": [name], "still": [(name, command)], "skipped": [(name, command)]}`` for a ``--fix`` run."""
    before = before or {}
    now = {entry.get("name"): entry.get("level") for entry in results}
    # A repair counts once its finding reads OK (or has gone); one that still warns still needs you.
    healthy = [
        str(entry["name"])[len("fix:"):] for entry in fixes
        if entry.get("level") == "ok" and str(entry.get("name", "")).startswith("fix:")
        and entry["name"] not in _LIFECYCLE and now.get(str(entry["name"])[len("fix:"):], "ok") == "ok"
    ]
    healthy += [
        str(entry["name"]) for entry in results
        if entry.get("level") == "ok" and before.get(entry["name"]) not in (None, "ok")
    ]
    healthy = list(dict.fromkeys(healthy))
    skipped_names = {entry["name"] for entry in skipped}
    still: Dict[str, str] = {}
    for entry in results:
        name = str(entry.get("name") or "")
        if entry.get("level") != "ok" and name not in healthy and name not in skipped_names:
            still.setdefault(name, next_command(entry))
    for entry in fixes:
        name = str(entry.get("name") or "")
        if entry.get("level") != "ok" and name[len("fix:"):] not in skipped_names:
            still.setdefault(name, next_command(entry))
    return {
        "healthy": healthy,
        "still": list(still.items()),
        "skipped": [(entry["name"], entry["command"]) for entry in skipped],
    }


def print_summary(report: Optional[Mapping[str, List]], stream=None) -> None:
    out = stream or sys.stdout
    if not report:
        return
    lines: List[str] = []
    if report.get("healthy"):
        lines.append(f"Healthy now: {', '.join(report['healthy'])}.")
    if report.get("still"):
        lines.append("Still needs you:")
        lines += [f"  - {name}: {command}" for name, command in report["still"]]
    if report.get("skipped"):
        lines.append("Skipped by your choice:")
        lines += [f"  - {name}: {command}" for name, command in report["skipped"]]
    if lines:
        print("\n" + "\n".join(lines), file=out)
