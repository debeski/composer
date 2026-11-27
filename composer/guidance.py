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

import re
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
_SERVICE_PROBLEM = re.compile(
    r"(?:^|; )([A-Za-z0-9_.-]+) (?:keeps restarting|is unhealthy|never started|has stopped|has no container)"
)
_REPAIR = "./start.sh check --fix"
_DOCS = "docs/check.md"
_UNSAFE_VALUE = re.compile(r"[\r\n\"'#\\]")


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
    reader: Callable[[str], str] = input,
    stream=None,
) -> Optional[str]:
    """The key of the chosen option, ``default`` on Enter, None for a skip, end of input or three wrong answers."""
    out = stream or sys.stderr
    keys = [key for key, _label in options]
    lines = ["", f"? {question}", *(f"  {line}" for line in intro)]
    lines += [f"  [{key}] {label}" for key, label in options]
    if skip:
        lines.append(f"  [{SKIP}] skip: change nothing")
    print("\n".join(lines), file=out, flush=True)
    for _attempt in range(3):
        try:
            answer = str(reader(f"  Choice [{default}]: ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("", file=out)
            return None
        if not answer:
            return default
        if skip and answer == SKIP:
            return None
        if answer in keys:
            return answer
        print(f"  Answer {', '.join(keys)}{' or ' + SKIP if skip else ''}.", file=out)
    return None


def ask_text(prompt: str, check: Callable[[str], str], *, reader=input, stream=None) -> Optional[str]:
    """A typed value that ``check`` accepts (it returns an error, or ``""``); None when left empty."""
    out = stream or sys.stderr
    for _attempt in range(3):
        try:
            value = " ".join(str(reader(prompt)).split())
        except (EOFError, KeyboardInterrupt):
            print("", file=out)
            return None
        if not value:
            return None
        problem = check(value)
        if not problem:
            return value
        print(f"  {problem}", file=out)
    return None


def ask_front_proxy(*, reader=input, stream=None) -> Optional[str]:
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


def ask_update_policy(current: str, *, reader=input, stream=None) -> Optional[str]:
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


def ask_redis(*, reader=input, stream=None) -> bool:
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


ADMIN_KEY = "ADMIN_PASS"
ADMIN_DEFAULT = "admin"
ADMIN_WHY = (
    "It is used only to create the first admin account (user 'admin'); an admin that already exists is "
    "never changed."
)
ADMIN_MIN_LENGTH = 8


def admin_pass_unset(value: Optional[str]) -> bool:
    """True for an ``ADMIN_PASS`` with which DjangoLux's migrator creates no admin: empty or ``admin``."""
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return not text or text == ADMIN_DEFAULT


def _hidden(prompt: str) -> str:
    import getpass

    return getpass.getpass(prompt)


def ask_admin_pass(*, reader: Callable[[str], str] = _hidden, stream=None) -> Optional[str]:
    """A new ``ADMIN_PASS``, typed twice without echo; None on Enter, end of input or three failed tries.

    The value is never printed: not in the question, an error or the summary.
    """
    out = stream or sys.stderr
    print("\n".join([
        "",
        "? The admin account has no password yet (ADMIN_PASS is empty or 'admin'), so none will be created.",
        f"  {ADMIN_WHY}",
        "  Type one (it is not shown), or press Enter to skip.",
    ]), file=out, flush=True)
    for _attempt in range(3):
        try:
            first = str(reader("  Admin password: "))
            if not first:
                return None
            problem = ""
            if len(first) < ADMIN_MIN_LENGTH:
                problem = f"Use at least {ADMIN_MIN_LENGTH} characters."
            elif first.strip() != first or first == ADMIN_DEFAULT:
                problem = "Use a password without leading or trailing spaces, other than 'admin'."
            elif _UNSAFE_VALUE.search(first):
                problem = "Use a password without quotes, '#', backslashes or line breaks (the secrets file keeps them)."
            if problem:
                print(f"  {problem}", file=out)
                continue
            second = str(reader("  The same again: "))
        except (EOFError, KeyboardInterrupt):
            print("", file=out)
            return None
        if second == first:
            return first
        print("  The two entries differ.", file=out)
    return None


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
    path = secrets_file(root)
    if path is None:
        raise OSError("There is no secrets file (.env, secrets/.env or .secrets/.env) to write to.")
    text = _with_values(path, values)
    write_updates(Path(root).resolve(), {path: text}, Path(root).resolve() / ".xclude" / "composer-check")
    return path


def set_secret_atomically(root, values: Mapping[str, str]) -> Path:
    """Set ``KEY=value`` lines as ``set_secret_values`` does, through a new file renamed over the old one.

    For a password: a crash leaves the old file or the new one, never half of
    either. The new file is created 0600 (never readable by anyone else, even
    for a moment), takes the old file's owner and group where this process may
    give them, and stays 0600. Nothing is copied or logged.
    """
    import os
    import tempfile

    path = secrets_file(root)
    if path is None:
        raise OSError("There is no secrets file (.env, secrets/.env or .secrets/.env) to write to.")
    text = _with_values(path, values)
    before = path.stat()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.composer-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            try:
                os.fchown(stream.fileno(), before.st_uid, before.st_gid)
            except OSError:
                pass
            stream.write(text.encode("utf-8", "surrogateescape"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        # The half-written copy holds the secrets: it is not kept.
        if os.path.lexists(temporary):
            os.unlink(temporary)
        raise
    return path


def _with_values(path: Path, values: Mapping[str, str]) -> str:
    """The secrets file's text with ``values`` set; refuses a value ``--env-file`` would misread."""
    from .stack_schema import line_ending

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
    return "".join(lines)


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


def next_step(entry: Mapping[str, Any], *, ran: bool, manual: str = "") -> str:
    """The operator's next step for ``entry`` once a ``--fix`` run is over.

    After a run that was not declined, sending the operator back to ``check
    --fix`` for a finding it left goes in a circle: the repair ran and did not
    clear it, or there is none for this form of it. The step by hand is named
    instead: the finding's own ``manual``, or its repair's, or the doc section.
    A finding whose only advice is ``check`` gets its step by hand too.
    """
    command = next_command(entry)
    manual = str(entry.get("manual") or manual or "")
    if command == "./start.sh check" and manual:
        return manual
    if not ran or command != _REPAIR:
        return command
    if manual:
        return manual
    name = str(entry.get("name") or "")
    name = name[len("fix:"):] if name.startswith("fix:") else name
    return f"by hand, as {_DOCS} describes for {name}: check --fix cannot repair it"


def summary(
    results: Sequence[Mapping[str, Any]],
    fixes: Sequence[Mapping[str, Any]],
    skipped: Sequence[Mapping[str, str]],
    *,
    before: Optional[Mapping[str, str]] = None,
    declined: bool = False,
) -> Dict[str, List]:
    """``{"healthy": [name], "still": [(name, command)], "skipped": [(name, command)]}`` for a ``--fix`` run.

    ``declined``: the operator answered no, so ``check --fix`` is still the way forward.
    """
    before = before or {}
    ran = not declined
    manuals = {
        str(entry.get("name") or ""): str(entry.get("manual") or "")
        for entry in fixes if entry.get("manual")
    }
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
            manual = manuals.get(f"fix:{name}") or manuals.get(name, "")
            still.setdefault(name, next_step(entry, ran=ran, manual=manual))
    for entry in fixes:
        name = str(entry.get("name") or "")
        if entry.get("level") != "ok" and name[len("fix:"):] not in skipped_names:
            still.setdefault(name, next_step(entry, ran=ran))
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
