# composer

Env. Docker. Silence.

Composer resolves secrets from a plaintext env file and orchestrates Docker Compose. No local Python setup. Just Docker.

## setup
Put `start.sh` or `start.ps1` in your project root.

## deployment
Just start it.

```bash
./start.sh
```

Composer resolves secrets automatically. It looks for a plaintext env file —
`.env`, `secrets/.env`, then `.secrets/.env` — and uses the first one that
supplies every variable the compose file requires.

## the surface

`composer run [-m] [-s] [-F] [-f FILE] [-d] <service> <command...>` runs a command inside a service instead of typing `docker exec`/`docker run` by hand. Defaults to `docker compose exec <service> …`; `-m`/`--manage` prepends `python manage.py` (e.g. `./start.sh run -m web migrate --noinput`), `-s`/`--shell` runs the command via `sh -c` so pipes/`&&` work, and `-F`/`--fresh` uses a one-off `docker compose run --rm`. TTY is auto-detected. See `composer run --help`.

`composer watch --trigger-file PATH [--interval N]` runs composer as a resident, in-compose updater. It watches the trigger file and, on each new request (a changed `token`, or the file's `mtime`), runs a full update (`composer -uo`: pull → version gate → recreate → health → post_start). The processed token is recorded in `<trigger-file>.ack`, so a request is applied once and survives a restart. Add `--status-file PATH` to have each run publish [deploy status](#deploy-status). See `composer watch --help`.

| flag | result |
| :--- | :--- |
| `-d`, `--dev` | Development mode. Loads `compose.dev.yml` on top of the base compose file (two files) and forces `DEBUG=True` / `DEBUG_STATUS=True` into every service. |
| `-u`, `--update [service]` | Pull the latest image(s) then recreate immediately. Pass a service name to update and recreate only that service (Compose still starts its dependencies; dependents aren't auto-restarted unless their own image changed). |
| `-uo`, `--update-only [service]` | Pull the latest image(s) before the normal full startup, without scoping the recreate. Optionally a single service. |
| `-r`, `--restart [service]` | Restart running containers via `docker compose restart` instead of a `--down` + start. Containers are preserved, so baked-in env vars survive. Pass a service name to restart only that service. |
| `-b`, `--build` | Rebuild images during startup. |
| `--force` | Bypass the preflight version gate (allow updating onto an older image version). |
| `--status-file PATH` | Write a JSON deploy-status file to `PATH` (overrides `COMPOSER_STATUS_FILE`). |
| `--down` | Stop everything. |
| `-v`, `--volumes` | Remove volumes too. |
| `-p`, `--purge` | With `--down`: also remove built untagged images, volumes, networks, orphans, and dangling build cache. |

## deploy status

Pass `--status-file PATH` (or set `COMPOSER_STATUS_FILE`) and composer writes an
atomic JSON document as it works, so another process (a Django admin panel, a
dashboard) can watch a deploy:

```json
{ "status": "migrating", "updated_at": "2026-07-04T08:31:38+00:00",
  "composer_version": "1.1.5", "compose_files": ["compose.yml"],
  "target_images": ["debeski/app:latest"], "target_version": "1.2.10",
  "active_version": "1.2.9" }
```

States: `starting` → `pulling` → `recreating` → `migrating` → `ready`, or
`failed` (with an `error`). The restart flow reports `restarting`/`ready`/`failed`.
Nothing is written unless configured.

## version gate

When updating (`-u`/`-uo`), composer can refuse to recreate onto an image that
is **older** than the version already deployed — the one thing a pull-and-restart
can't safely undo when forward-only migrations have already run. It is opt-in and
generic: set `COMPOSER_ACTIVE_VERSION_FILE` (a JSON file, e.g. a runtime
`active.json`) and, if needed, `COMPOSER_ACTIVE_VERSION_KEY` (default `version`)
and `COMPOSER_VERSION_LABEL` (the image label to compare, default
`org.opencontainers.image.version`). With no active-version source configured the
gate is disabled. `--force` overrides a block.

## mechanics
- **Secrets**: Plaintext env file (`.env` → `secrets/.env` → `.secrets/.env`); the first that satisfies the compose's required vars wins.
- **Version**: Every service gets `COMPOSER_VERSION`.
- **UI**: Progress stays on one status line.
- **Image**: Wrapper scripts target `debeski/composer:latest`.

## why
Installing Python and a compose toolchain everywhere is friction. Composer keeps the toolchain inside the container and leaves the project root alone.
