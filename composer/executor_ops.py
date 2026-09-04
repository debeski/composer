"""The executor's real operation handler.

Runs ``restart`` / ``recovery_deploy`` with Docker authority, mirroring the
agent's existing child-op invocation exactly — only the process performing it
moves. Restart re-enforces the protected/allowlist policy here (the executor is
the authority; it never trusts the agent to have checked).

``dlux_package_apply`` / ``dlux_package_rollback`` are the inline DjangoLux swap.
They run entirely offline: the agent staged the wheel on the runtime volume and
sent the digest it must hash to (see composer/dlux_package_stage.py).
"""

import os
import subprocess
import sys
from typing import Dict, Tuple

from . import executor_protocol as proto
from .service_selection import (
    PROTECTED_RESTART_SERVICES,
    join_service_list,
    parse_service_list,
)

# Resident/self services the executor always excludes from an op it drives.
_SELF_EXCLUDED = (
    "composer-agent",
    "composer-executor",
    "composer-updater",
    "docker-socket-proxy",
)


def _op_env(operation_id: str) -> dict:
    env = os.environ.copy()
    env["COMPOSER_OPERATION_ID"] = operation_id
    excluded = parse_service_list(env.get("COMPOSER_EXCLUDE_SERVICES"))
    for service in _SELF_EXCLUDED:
        if service not in excluded:
            excluded.append(service)
    env["COMPOSER_EXCLUDE_SERVICES"] = join_service_list(excluded)
    return env


def _run(argv, env) -> Tuple[int, str]:
    try:
        return subprocess.run(argv, env=env).returncode, ""
    except OSError as exc:
        return 127, f"Composer process could not start: {exc}"


def _run_restart(operation_id: str, service: str) -> Tuple[int, str]:
    env = _op_env(operation_id)
    protected = PROTECTED_RESTART_SERVICES | set(
        parse_service_list(env.get("COMPOSER_EXCLUDE_SERVICES"))
    )
    allowed = [
        item
        for item in parse_service_list(os.environ.get("COMPOSER_AGENT_RESTART_SERVICES"))
        if item not in protected
    ]
    if service in protected:
        return 2, f"Service '{service}' is protected from remote restart."
    if service and service not in allowed:
        return 2, f"Service '{service}' is not in COMPOSER_AGENT_RESTART_SERVICES."
    if not service and not allowed:
        return 2, "Project restart is disabled because no restart allowlist is configured."
    argv = [sys.executable, "-m", "composer", "restart"]
    if service:
        argv.append(service)
    else:
        env["COMPOSER_RESTART_SERVICES"] = join_service_list(allowed)
    return _run(argv, env)


def _run_recovery(operation_id: str, force: bool) -> Tuple[int, str]:
    # Mirrors the agent's current recovery path (a scoped update pipeline; the
    # version gate still rejects downgrades). Recovery semantics beyond parity
    # are out of scope for this security relocation.
    env = _op_env(operation_id)
    argv = [sys.executable, "-m", "composer", "update"]
    if force:
        argv.append("--force")
    return _run(argv, env)


def _run_dlux_package_apply(operation_id: str, payload: Dict) -> Tuple[int, str]:
    """Swap in a release the agent already fetched and verified.

    Nothing here reaches the network: the wheel is on the runtime volume and the
    digest it must hash to arrived over the socket. Exit 3 (rollback also
    unhealthy) travels back to the agent unchanged — a caller that retries on
    failure must not retry that one.
    """
    argv = [
        sys.executable, "-m", "composer", "dlux-update", "apply",
        "--version", payload["version"],
        "--staged-wheel", payload["filename"],
        "--staged-sha256", payload["sha256"],
    ]
    return _run(argv, _op_env(operation_id))


def _run_dlux_package_rollback(operation_id: str) -> Tuple[int, str]:
    argv = [sys.executable, "-m", "composer", "dlux-update", "rollback"]
    return _run(argv, _op_env(operation_id))


def default_operation_handler(request: Dict) -> Dict:
    """Map a validated executor request to a redacted typed result."""
    operation_id = request["operation_id"]
    op = request["op"]
    payload = request.get("payload", {})
    if op == "restart":
        exit_code, detail = _run_restart(operation_id, payload.get("service", ""))
    elif op == "recovery_deploy":
        exit_code, detail = _run_recovery(operation_id, bool(payload.get("force")))
    elif op == "dlux_package_apply":
        exit_code, detail = _run_dlux_package_apply(operation_id, payload)
    elif op == "dlux_package_rollback":
        exit_code, detail = _run_dlux_package_rollback(operation_id)
    else:  # unreachable: validate_executor_request already rejected unknown ops
        return proto.build_result(operation_id, "rejected", exit_code=2, detail=f"Unsupported op: {op}")
    state = "succeeded" if exit_code == 0 else "failed"
    return proto.build_result(operation_id, state, exit_code=exit_code, detail=detail)
