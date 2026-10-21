"""`composer maintenance` — publish the proxy maintenance page to the runtime volume."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List

from . import maintenance_page

COMMANDS = ["publish"]
DEFAULT_RUNTIME_ROOT = "/opt/dlux-runtime"


def parse_maintenance_args(argv: List[str], *, action: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=f"composer maintenance {action}",
        description="Publish the maintenance page the proxy serves while the application is down.",
    )
    parser.add_argument(
        "--runtime-root",
        default=os.environ.get("DLUX_UPDATE_RUNTIME_ROOT") or DEFAULT_RUNTIME_ROOT,
        help="Runtime volume mount point (default: /opt/dlux-runtime)",
    )
    parser.add_argument(
        "--override",
        default="",
        help="Publish this directory (a project's .proxy/maintenance/) instead of the bundled page",
    )
    return parser.parse_args(argv)


def run_maintenance(action: str, argv: List[str]) -> int:
    args = parse_maintenance_args(argv, action=action)
    runtime_root = Path(args.runtime_root)
    try:
        if args.override:
            count = maintenance_page.publish_override(Path(args.override), runtime_root)
            print(f"✔ Published the project maintenance page ({count} file(s)).")
            return 0
        changed = maintenance_page.publish(runtime_root)
    except OSError as exc:
        print(f"✖ Could not publish the maintenance page: {exc}", file=sys.stderr)
        return 1
    if (maintenance_page.published_dir(runtime_root) / maintenance_page.CUSTOM_MARKER).exists():
        print("✔ A project maintenance page is published; left in place.")
    else:
        print("✔ Maintenance page published." if changed else "✔ Maintenance page is current.")
    return 0
