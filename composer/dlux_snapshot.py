"""Pre-update snapshots for the operator's `dlux update` / `dlux rollback`.

DjangoLux takes its own backup before it hands an update to Composer, but an
operator typing `composer dlux update` (or `./start.sh dlux update`) goes
straight to the swap and never passes through DjangoLux's update path — so,
until this module, the CLI ran with no snapshot at all.

The snapshot is DjangoLux's own system backup, taken inside the stack: Composer
execs `manage.py dlux_backup` in a running service (under the runtime
supervisor, so it is the active release that takes it) and reads the JSON line
it prints. A DjangoLux too old to have that command gets the same backup
through `manage.py shell` and the backup API every 1.x release ships.
"""

import json
from typing import Optional, Tuple

BACKUP_SCOPES = ("data", "full", "skip")
# Celery first: the backup is long-running work and web is serving requests.
BACKUP_SERVICES = ("celery", "web")

_COMMAND = ["python", "manage.py", "dlux_backup", "--trigger", "update", "--requested-by", "composer"]

_FALLBACK = (
    "import json; from django.apps import apps; from dlux.backup import run_system_backup; "
    "SB = apps.get_model('dlux', 'SystemBackup'); "
    "b = SB.objects.create(requested_by_username='composer', trigger='update', media_included={media}); "
    "run_system_backup(b.pk); b.refresh_from_db(); "
    # A failure may have armed an automatic retry; the caller decides on this
    # answer, so make it final rather than leave a backup to run later.
    "SB.objects.filter(pk=b.pk, status='pending').update(status='failed', next_attempt_at=None) "
    "if b.status == 'pending' else None; b.refresh_from_db(); "
    "print(json.dumps({{'ok': b.status == 'completed', 'token': b.token, 'status': b.status, "
    "'rows': b.row_count, 'error': b.error}}))"
)


def _last_json(text: str) -> Optional[dict]:
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


def _backup_service(launcher) -> Optional[str]:
    config = launcher.compose_config_json() or {}
    services = config.get("services") if isinstance(config, dict) else None
    if not isinstance(services, dict):
        return None
    return next((name for name in BACKUP_SERVICES if name in services), None)


def _exec(launcher, service, command):
    argv = ["exec", "-T", service] + launcher.supervised_prefix(service, command) + command
    return launcher.run_docker_compose(argv, timeout=None)


def take_snapshot(launcher, scope: str = "data") -> Tuple[bool, str]:
    """Take a DjangoLux system backup in the stack; ``(ok, detail)``."""
    service = _backup_service(launcher)
    if not service:
        return False, "the stack has no celery or web service to take the backup in"
    ok, out, err = _exec(launcher, service, _COMMAND + ["--scope", scope])
    combined = f"{out}\n{err}"
    if not ok and "Unknown command" in combined and "dlux_backup" in combined:
        script = _FALLBACK.format(media="True" if scope == "full" else "False")
        ok, out, err = _exec(launcher, service, ["python", "manage.py", "shell", "-c", script])
    result = _last_json(out)
    if result and result.get("ok"):
        token = str(result.get("token") or "")
        rows = result.get("rows")
        return True, f"backup {token[:8]} in {service}" + (f" ({rows} rows)" if rows is not None else "")
    reason = (result or {}).get("error") or (err or out or "").strip().splitlines()[-1:] or ["no result"]
    if isinstance(reason, list):
        reason = reason[0]
    return False, f"the backup failed in {service}: {reason}"

