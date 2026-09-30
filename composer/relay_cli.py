"""`composer relay` — review and approve the outbound operations a project declares.

A project lists the calls it needs in ``relay/operations.json``. Nothing runs until
an operator approves them here, which pins each operation's exact digest in
``relay/operations.lock`` (commit it). Editing an operation, even one character
of its host, unapproves it until this is run again, so a reviewer sees every new
host in the diff of the lock file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List

from .relay import (
    BUILTIN_OPERATIONS, LOCK_FILE, OPERATIONS_FILE, SCHEMA_VERSION,
    Operation, load_declared, parse_operation, read_lock,
)

COMMANDS = ["list", "approve"]


def _describe(operation: Operation) -> str:
    extras = [operation.response_type + (f" ≤{operation.max_bytes // 1024} KiB" if operation.response_type == "text" else "")]
    if operation.auth:
        extras.append(f"secret via {operation.auth['placement']}")
    extras.append(f"{operation.per_minute}/min")
    return f"https://{operation.host}{operation.path}  ({', '.join(extras)})"


def parse_relay_args(argv: List[str], *, action: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=f"composer relay {action}",
        description={
            "list": "Show the declared and built-in outbound operations and whether each is approved.",
            "approve": "Approve every valid declared operation by pinning its digest in relay/operations.lock.",
        }[action],
    )
    parser.add_argument(
        "--dir", default=os.environ.get("COMPOSER_RELAY_DIR") or "relay",
        help="Directory holding operations.json and operations.lock (default: ./relay)",
    )
    return parser.parse_args(argv)


def run_relay(action: str, argv: List[str]) -> int:
    args = parse_relay_args(argv, action=action)
    directory = Path(args.dir)
    declared, problems = load_declared(directory)
    lock = read_lock(directory)

    if action == "list":
        for raw in BUILTIN_OPERATIONS:
            operation = parse_operation(raw, builtin=True)
            print(f"  built-in    {operation.name}  {_describe(operation)}")
        for name, operation in sorted(declared.items()):
            state = "approved" if lock.get(name) == operation.digest else (
                "CHANGED" if name in lock else "unapproved")
            print(f"  {state:<11} {name}  {_describe(operation)}")
        for name in sorted(set(lock) - set(declared)):
            print(f"  stale-lock  {name}  (in {LOCK_FILE}, no longer declared)")
        for problem in problems:
            print(f"  INVALID     {problem['name'] or OPERATIONS_FILE}: {problem['reason']}")
        if not (BUILTIN_OPERATIONS or declared or problems):
            print(f"  No operations. Declare them in {directory / OPERATIONS_FILE}.")
        return 2 if problems else 0

    if problems:
        for problem in problems:
            print(f"✗ {problem['name'] or OPERATIONS_FILE}: {problem['reason']}", file=sys.stderr)
        print("Nothing approved: fix the declarations above first.", file=sys.stderr)
        return 2
    if not declared:
        print(f"Nothing to approve: {directory / OPERATIONS_FILE} declares no operations.")
        return 0
    new_lock = {name: operation.digest for name, operation in sorted(declared.items())}
    if new_lock == lock:
        print("Already approved: the lock matches the declarations.")
        return 0
    for name, operation in sorted(declared.items()):
        if name not in lock:
            print(f"+ new      {name}  {_describe(operation)}")
        elif lock[name] != operation.digest:
            print(f"~ changed  {name}  {_describe(operation)}")
    for name in sorted(set(lock) - set(declared)):
        print(f"- removed  {name}")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / LOCK_FILE
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "operations": new_lock}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    print(f"Approved {len(new_lock)} operation(s) in {target}. Commit it with the declarations.")
    return 0
