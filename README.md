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

### resident agent secret access

A resident `composer-agent` must retain the same values for later image-update
runs. The `start.sh`/`start.ps1` wrappers pass the selected file to the one-shot
Composer container with Docker's `--env-file`; when Composer creates or recreates
`composer-agent`, its private mode-`0600` runtime override forwards those values
and a key manifest only to that service. The agent and its update children then
use the inherited environment instead of reopening the host bind-mounted file.
Host mode-`0600` secrets therefore need no ACL or capability exception.

Legacy `composer-updater` services remain supported through `composer watch` for
one migration cycle. New deployments use `composer agent`.

## the surface

`composer run [-m] [-s] [-F] [-f FILE] [-d] <service> <command...>` runs a command inside a service instead of typing `docker exec`/`docker run` by hand. Defaults to `docker compose exec <service> …`; `-m`/`--manage` prepends `python manage.py` (e.g. `./start.sh run -m web migrate --noinput`), `-s`/`--shell` runs the command via `sh -c` so pipes/`&&` work, and `-F`/`--fresh` uses a one-off `docker compose run --rm`. TTY is auto-detected. See `composer run --help`.

`composer restart [-f FILE] [-d] [--status-file PATH] [service]` restarts running containers through `docker compose restart`, then waits for their health checks. Containers are preserved and post-start tasks are skipped. Pass a service to restart only that service. `composer -r ...` and `composer --restart ...` remain short leading aliases. See `composer restart --help`.

`composer update [-o] [-b] [--force] [-nm] [-mm] [-a APP] [--status-file PATH] [-f FILE] [-d] [service...]` pulls the latest image(s), then recreates the affected containers, waits for health checks, and runs post-start tasks. Name services to scope both the pull and the recreate (Compose still starts their dependencies). `-o`/`--only` stops right after the pull — no version gate, recreate, health checks, or post-start tasks. The flat `composer -u [service]` / `-uo [service]` flags remain, and `./start.sh --update` on its own still means "update the Composer tool image", not the app. See `composer update --help`.

`composer check [--fix] [-y] [--deep] [--json] [-f FILE] [-d]` is the doctor for the *outside* of a DLUX stack. It verifies Docker + Compose v2, that the compose config resolves, that a secrets source is present/readable/non-empty, that every externally-required compose variable has a value, the topology mode (composer-agent vs legacy composer-updater vs unmanaged), obsolete DLUX services, and version drift between the deploying composer and the resident `composer-agent`. It warns when `pgadmin`, `db-backup`, or the legacy `db_backup` spelling is present. Guarded `--fix` validates the complete cleaned candidate, preserves original Compose files under `.xpose/composer-check/`, stops and removes only the detected obsolete containers with targeted `docker compose rm -sf`, applies the service-block removal, then rediscovers services and verifies every pre-existing named Docker volume remains. It never uses a broad orphan-cleaning redeploy and never removes volumes; it also migrates legacy `composer-updater` → `composer-agent` through `enable-agent`. `--deep` relays the in-container app-level doctor — `python manage.py dlux_doctor` in the `web` service by default, overridable with `--deep-service`/`--deep-command` — and prints its output. `--json` emits structured results; exit is non-zero on a blocking problem or failed fix/postflight. This is the single evolving check that replaces per-change `enable-*` one-offs: composer owns the outside and hands the rest to the container(s). See `composer check --help`.

`composer stop [-v] [-p] [-y] [-f FILE] [-d] [service...]` stops and removes this project's containers. Secrets, health checks, and post-start tasks are skipped. Pass service names to stop only those services instead of the whole stack. `-v`/`--volumes` and `-p`/`--purge` are destructive and project-wide, so they cannot be combined with service names and they require a typed confirmation (see below). `composer down ...` is an alias and `composer --down ...` remains a flat-flag equivalent. See `composer stop --help`.

`composer log [-n N|all] [-F] [-t] [--since TIME] [-f FILE] [-d] [service...]` reads Compose logs for the whole stack (interleaved, one color per service) or for the services you name. Defaults to the last 50 lines per service; `-n all` (or `-n 0`) lifts the limit, `-F`/`--follow` streams, `-t`/`--timestamps` prefixes each line, and `--since`/`--until` bound the window. `composer logs` is an alias. See `composer log --help`.

`composer watch --trigger-file PATH [--interval N]` runs composer as a resident, in-compose updater. It watches the trigger file and, on each new request (a changed `token`, or the file's `mtime`), runs a full update (`composer -u`: pull → version gate → recreate → health → post_start). The processed token and child exit code are recorded in `<trigger-file>.ack`, so a request is applied once and survives a restart. Add `--status-file PATH` to have each run publish [deploy status](#deploy-status); if the child exits before publishing its own terminal failure, the watcher guarantees a token-matched `failed` status so maintenance consumers are never left waiting on a dead process. See `composer watch --help`.

`composer agent` is the durable successor to `watch`. It preserves the same local trigger/status/ack and registry-availability files, adds a typed DLUX spool, SQLite command/outbox replay, operation correlation, safe restart allowlisting, and optional outbound HTTPS long polling. Configure `COMPOSER_CONTROL_URL`, a 15-minute one-use `COMPOSER_ENROLLMENT_TOKEN`, and `COMPOSER_AGENT_STATE_DIR` (default `/var/lib/composer-agent`). Local DLUX-triggered updates continue while the control plane is unavailable or the machine credential is revoked. See [Agent Protocol v1](docs/agent-protocol-v1.md).

Migrate an existing generated DLUX project with Composer itself:

```bash
./start.sh --update
./start.sh enable-agent
./start.sh enable-agent --apply
```

The default dry run prints the exact Compose diff. Apply accepts only a recognized
DLUX-generated updater block, validates the candidate with `docker compose config`
before writing, preserves the original under
`.xpose/dlux-agent-bootstrap/<timestamp>/`, and replaces it atomically. The
replacement keeps the deployment's own topology: the networks attached to the
outgoing `docker-socket-proxy`/`composer-updater` services, that block's
`COMPOSER_VERSION_LABEL`, and its `WEB_IMAGE` reference are carried forward
verbatim, so projects generated before the DjangoLux 1.5 scaffold (`egress` /
`docker_proxy`) migrate without renaming networks or losing the baked-version
gate. A network the project never declares is reported by name before any write.
Only the Compose file is read and rewritten, so this runs in a deployment
directory that holds nothing but `compose.yml`, `.proxy/`, and `.secrets/`. When a
`requirements.txt`/`pyproject.toml` is present it must declare DjangoLux 1.5.0+
or apply refuses without `--allow-unverified-dlux`; with no manifest to read the
check is reported as an advisory warning instead.
A locally installed binary may instead run `composer enable-agent --project-dir
/path/to/project`. The deprecated `python -m dlux enable-agent` route forwards to
this command for one migration cycle; Composer is the sole transformer.

When `watch` runs inside the same Compose project it is updating, it excludes the resident updater service from child runs by default (`composer-updater`). The child receives `COMPOSER_EXCLUDE_SERVICES=composer-updater`, so pull/config/up/health/post-start operate on the application services and do not recreate the container that is supervising the update. Override the service name with `COMPOSER_WATCH_SELF_SERVICE`, disable the default with `COMPOSER_WATCH_SELF_SERVICE=""`, or set additional exclusions with `COMPOSER_EXCLUDE_SERVICES`.

`watch` can also **detect a newer image** and publish availability for another process to act on: `--check-image IMAGE` (repeatable) + `--availability-file PATH` (and `--check-interval SECONDS`, default 3600) poll the registry's tag digest vs the locally-pulled one and write `{ "available": …, "images": [ … ] }`. It only reports *readable* differences (an unreachable registry is "unknown", never a false positive), needs no registry access from the consumer, and re-checks right after an applied update. `COMPOSER_REGISTRY_TOKEN` covers private repositories.

For an available image, Composer also reads remote image labels once and publishes two independent, optional fields. `version` comes from `COMPOSER_VERSION_LABEL` (default `org.opencontainers.image.version`). `manifest` comes from `COMPOSER_RELEASE_MANIFEST_LABEL` (default `org.dlux.project.release-manifest`) when that label contains a schema-1 JSON object with any of `version`, `summary`, up to eight `highlights`, or an HTTPS `release_url`. Raw JSON remains supported, but CI/build pipelines should use `base64:<URL-safe-base64-JSON>` so quoting cannot corrupt the label while it crosses YAML, action inputs, and Docker build arguments. Missing or malformed metadata is omitted without affecting digest detection or deployment.

With `--status-file` (or `--log-file PATH`), each update run also writes a clean, ANSI-free **console log** (`deploy-log.txt` beside the status file, fresh per run). Together with the deploy status, a proxy can render a live progress page + console while the app is being recreated and unreachable.

| flag | result |
| :--- | :--- |
| `-d`, `--dev` | Development mode. Loads `compose.dev.yml` on top of the base compose file (two files) and forces `DEBUG=True` / `DEBUG_STATUS=True` into every service. |
| `-u`, `--update [service]` | Pull the latest image(s) then recreate immediately. Pass a service name to update and recreate only that service (Compose still starts its dependencies; dependents aren't auto-restarted unless their own image changed). Same as the `update` subcommand. |
| `-uo`, `--update-only [service]` | Pull the latest image(s) only, then exit. Pass a service name to pull only that service. Does not run `up`, health checks, or post-start tasks. Same as `update -o`. |
| `-b`, `--build` | Rebuild images during startup. |
| `--force` | Bypass the preflight version gate (allow updating onto an older image version). |
| `--status-file PATH` | Write a JSON deploy-status file to `PATH` (overrides `COMPOSER_STATUS_FILE`). |
| `--down` | Stop everything (see the `stop` subcommand). |
| `-v`, `--volumes` | With `--down`: Remove volumes too. Confirmation required. |
| `-p`, `--purge` | With `--down`: also remove built untagged images, volumes, networks, orphans, and dangling build cache. Confirmation required. |
| `-y`, `--yes` | Answer the confirmation prompt in advance. |

## confirmation guard

Actions that destroy data — `-v`/`--volumes` and `-p`/`--purge` — stop and print
exactly what will be removed, then wait for you to type `y` or `yes`. Anything
else aborts before Docker is called.

Pass `-y`/`--yes` (or set `COMPOSER_ASSUME_YES=1`) to confirm up front. The guard
fails **closed**: when stdin is not a terminal (CI, cron, a piped script) and
neither the flag nor the variable is set, the run is refused instead of silently
proceeding.

```bash
./start.sh stop            # no prompt, nothing is destroyed
./start.sh stop web        # only the web service
./start.sh stop -v         # prompts: type y or yes
./start.sh stop -p -y      # no prompt, purges immediately
```

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
`failed` (with an `error`). Pull-only reports `starting` → `pulling` → `pulled`.
The restart flow reports `restarting`/`ready`/`failed`.
Nothing is written unless configured.

## version gate

When deploying an update (`-u`), composer can refuse to recreate onto an image that
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
- **Runtime override**: The generated Compose override is a private system-temp file, not a project-root file, so Composer supports host-owned and read-only project mounts without extra Linux capabilities.
- **Service exclusions**: `COMPOSER_EXCLUDE_SERVICES` is a comma/space-separated service list omitted from generated runtime overrides, bulk pulls, bulk `up -d`, health checks, and diagnostics. Explicit `-u SERVICE`/`-uo SERVICE` still targets the named service.
- **UI**: Progress stays on one status line.
- **Image**: Wrapper scripts target `debeski/composer:latest`.

## why
Installing Python and a compose toolchain everywhere is friction. Composer keeps the toolchain inside the container and leaves the project root alone.
