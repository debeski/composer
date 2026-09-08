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

New deployments run the hardened resident pair: `composer agent run` plus
`composer executor run`. Legacy `composer-updater` stacks still deploy, but
`composer check` reports them as a legacy topology and `check --fix` (or
`composer agent enable --apply`) migrates them to `composer-agent`.

## the surface

`composer run [-m] [-s] [-F] [-f FILE] [-d] <service> <command...>` runs a command inside a service instead of typing `docker exec`/`docker run` by hand. Defaults to `docker compose exec <service> …`; `-m`/`--manage` prepends `python manage.py` (e.g. `./start.sh run -m web migrate --noinput`), `-s`/`--shell` runs the command via `sh -c` so pipes/`&&` work, and `-F`/`--fresh` uses a one-off `docker compose run --rm`. TTY is auto-detected: a terminal gets an interactive session, a pipe gets `-T` and reads your piped stdin (`echo yes | ./start.sh run -m web collectstatic`). Note the wrapper nests two ptys, so type-ahead into an interactive prompt is dropped until the inner container attaches — prefer `--noinput` in scripts. See `composer run --help`.

`composer migrate [-s SERVICE] [-f FILE] [-d] [MIGRATOR_ARGS...]` runs DjangoLux's supervised migrator directly in the running `web` service. `--service` selects another service, and every unrecognized argument is passed to the migrator: `./start.sh migrate -mm -a documents`, `./start.sh migrate -nm`, or `./start.sh migrate -d --service web -- -mm`. The command uses the service's `org.dlux.post-start` command when present, otherwise it matches the existing `dlux-updater` supervisor and defaults to the packaged supervisor; its output and exit code remain attached to the terminal. See `composer migrate --help`.

`composer restart [-f FILE] [-d] [--status-file PATH] [service]` restarts running containers through `docker compose restart`, then waits for their health checks. Containers are preserved and post-start tasks are skipped. Pass a service to restart only that service. `composer -r ...` and `composer --restart ...` remain short leading aliases. See `composer restart --help`.

`composer update [-b] [--force] [-nm] [-mm] [-a APP] [--status-file PATH] [-f FILE] [-d] [service...]` pulls the latest application image(s), recreates the affected containers, waits for health checks, and runs post-start tasks. Name services to scope both the pull and recreate; the resident agent/executor and the watcher use this same subcommand internally. `-u [service]` remains the compact argument form. See `composer update --help`.

### Post-start tasks and the migrator contract

A service opts into a post-start task with an `org.dlux.post-start` label naming the command:

```yaml
  web:
    labels:
      org.dlux.post-start: "python -m dlux.updater.supervisor --no-watch -- python manage.py migrator"
```

Composer runs it once, after the stack is healthy, appending whichever flags the run was given. A label is used rather than Compose's native `post_start` hook because Compose runs that hook itself, on container start, without composer's flags — which meant two migrators running at the same time. Deployments still carrying a native `post_start` block keep working (composer execs it and says so); `composer check --fix` folds it into the label. Existing DLUX updater stacks missing both declarations use a narrowly gated compatibility migrator instead of silently doing nothing, and `check --fix` installs the missing label. Post-start execution is non-interactive (`compose exec -T`), and a failed or unhealthy hook now fails the Composer run rather than ending with a false “Environment ready.”

When the command is DjangoLux's `migrator`, `-a APP`, `-mm`, and `-nm` are forwarded and mean:

| flags | makemigrations | migrate | collectstatic |
| --- | --- | --- | --- |
| *(none)* | only apps with no `migrations/0001_*.py` | yes | yes |
| `-mm` | **all** target apps, forced | yes | yes |
| `-nm` | no | no | **yes** |

`-mm` and `-nm` are mutually exclusive. `-a APP` scopes which apps are considered; without it the migrator auto-discovers local apps. `-mm` writes migrations into the container filesystem, so they are lost on recreate unless the app source is bind-mounted — it is a development affordance.

`composer pull [--status-file PATH] [-f FILE] [-d] [service...]` only downloads images. It never recreates containers, runs health checks, or executes post-start tasks. This replaces the retired `-uo` and `update -o` forms.

`composer self update` pulls `debeski/composer:latest`, the Composer deployer used by `start.sh`. Set `COMPOSER_SELF_IMAGE` only when using a compatible Composer image mirror.

`composer check [--fix] [-y] [--deep] [--json] [--beta|--stable] [-f FILE] [-d]` is the doctor for the *outside* of a DLUX stack. It verifies Docker + Compose v2, that the compose config resolves, that a secrets source is present/readable/non-empty, that every externally-required compose variable has a value, the topology mode (hardened `composer-agent` + `composer-executor`, agent-only, legacy `composer-updater`, or unmanaged), obsolete DLUX services and proxy routes, and version drift between the deploying composer and the resident `composer-agent`. A mixed `composer-agent` + `composer-updater` topology fails closed. It warns when `pgadmin`, `db-backup`, or the legacy `db_backup` spelling is present, and independently detects recognized pgAdmin routes in `.proxy/Caddyfile`, `.proxy/default.conf.template`, and `.nginx/nginx.conf`. Guarded `--fix` validates the complete cleaned Compose candidate, archives every changed deployment/proxy file under `.xclude/composer-check/`, validates each changed proxy mounted by Caddy or Nginx, stops and removes only detected obsolete containers with targeted `docker compose rm -sf`, applies the service and route cleanup, rediscovers services, and verifies every pre-existing named Docker volume remains. A running Caddy or direct-config Nginx service is reloaded; template-backed Nginx is restarted and checked so its live rendered configuration is regenerated. This proxy-only path also repairs projects whose obsolete services were removed by v1.2.6. Unrecognized customized pgAdmin routes fail closed for manual review. The fix never uses a broad orphan-cleaning redeploy and never removes volumes; it also migrates legacy `composer-updater` stacks straight to the hardened `composer-agent` + `composer-executor` topology, hardens agent-only stacks, normalizes generated resident commands to `agent run`/`executor run`, rewrites retired local DLUX tools commands (`tools.dlux_runtime_supervisor`, `tools.smtp_relay`) to package-owned modules, removes generated local `tools/` bind mounts, adds missing `org.dlux.restart` labels, strips updater-era `web.post_start` hooks when `dlux-updater` is retired into `celery.pre_start`, and installs or normalizes the `org.dlux.post-start` migrator label only for stacks that still need that compatibility path. Runtime-command rewrites are gated on the project image shipping the needed DLUX module (`1.6.2+` for `dlux.updater.supervisor`, `1.7.0+` for `dlux.smtp_relay`). With `-d`, `--fix` also validates and normalizes `compose.dev.yml` against the merged base/dev config: it removes dev `dlux-updater` overrides, keeps Celery's runtime mount writable, and disables inline updates in dev containers. `--deep` relays the in-container app-level doctor — `python manage.py dlux_doctor` in the `web` service by default, overridable with `--deep-service`/`--deep-command` — and prints its output. `--json` emits structured results; exit is non-zero on a blocking problem or failed fix/postflight. This is the single evolving check that replaces per-change `enable-*` one-offs: composer owns the outside and hands the rest to the container(s). See `composer check --help`.

`composer agent check [--json] [--availability-file PATH] [IMAGE ...]` performs the image-availability half of the inline DLUX update flow without pulling, recreating, or enabling maintenance. It compares each remote tag digest with the locally pulled digest and reuses the resident agent's JSON contract, including optional candidate `version` and release `manifest` metadata. Pass tagged image references explicitly, or omit them to use `COMPOSER_CHECK_IMAGE`, then `WEB_IMAGE`, and finally the images the compose file's composer-agent/executor/updater block watches (`--check-image` entries and its `WEB_IMAGE` value, with `${VAR:-default}` interpolation resolved against the environment); `-f FILE` selects an alternate compose file for that discovery. `--json` is intended for DLUX; `--availability-file` atomically publishes the same document. Exit `0` means every registry lookup was definitive, even when an update is available; exit `1` means at least one lookup was unknown or the output file could not be written; exit `2` means invalid input. Digest-pinned references are rejected because they cannot represent a moving update channel. `COMPOSER_REGISTRY_TOKEN`, `COMPOSER_VERSION_LABEL`, and `COMPOSER_RELEASE_MANIFEST_LABEL` retain their existing meanings.

The resident services have three host-side lifecycle commands: `composer agent update` pulls and recreates, `composer agent restart` restarts and health-checks, and `composer agent off` stops — without touching the application or Compose project. When the compose file defines `composer-executor`, all three target the `composer-agent` + `composer-executor` pair together (both run the same image, so they can never drift to different versions); legacy stacks resolve to just `composer-agent`. The dedicated commands ignore the resident services' normal self-exclusion only for those explicit targets. `agent update` does not run application version gates or post-start hooks.

`composer dlux check [--version V] [--availability-file PATH] [-f FILE] [-d]` publishes what is available to `state/package-available.json`, the document DjangoLux reads instead of polling PyPI itself. `composer dlux update [--dry-run] [--version V] [--restart-service SERVICE] [--status-file PATH] [-f FILE] [-d]` applies an inline DjangoLux release on the runtime volume: it resolves the newest (or pinned) wheel from PyPI, verifies its Trusted Publisher attestation and SHA-256, honours the manifest's `inline_safe`, activates it under `state/active.json`, and restarts the services that load DjangoLux (`web`, `celery` by default; `dlux-updater` is deliberately excluded) with the same health wait `composer update` uses. `composer dlux rollback [--restart-service SERVICE] [--status-file PATH] [-f FILE] [-d]` restores the previous release. Exit `3` means the rollback was also unhealthy — a deployment that needs a human, and one a caller must not retry. In the hardened topology the two halves split the work: `composer-agent` owns the package trigger and stages the verified wheel on the runtime volume (it has the egress), then hands `composer-executor` the version, filename and digest over the private socket, and the executor applies it offline (`--staged-wheel`/`--staged-sha256`, the same flags an air-gapped operator can use by hand). The runtime root (`/opt/dlux-runtime`, `DLUX_UPDATE_RUNTIME_ROOT`, or `--runtime-root`) is a container path, so run from the project root composer locates the project's runtime volume in the compose config and re-runs itself in a sibling container with that volume attached; it refuses when no service mounts that path or when Docker does not have the volume yet. See `composer dlux --help`.

`composer dlux channel [stable|beta] [-f FILE] [-d]` reports or requests the **DjangoLux** release channel for this deployment. Stable is the default and admits no prerelease; beta is an explicit opt-in that widens eligibility only — every digest, attestation, manifest and migration check is identical on both. Composer *reads* the channel from `state/channel-policy.json` and never writes it: the DjangoLux worker owns that file because it owns the database row it mirrors, and two writers of one policy is how a deployment ends up disagreeing with itself. So `composer dlux channel beta` leaves a token-carrying request beside the policy and reports "requested, not yet applied" until that worker acknowledges it. A missing, unreadable, malformed or future-schema policy reads as **stable** — a deployment that never opted in is never handed a beta because a file was missing.

`composer check --beta` / `--stable` switch **Composer's own** channel, which is independent of the DjangoLux one: running a Composer beta to test Composer is not a reason to start feeding a production deployment beta framework releases. One operation moves both halves together — the persisted `.composer-channel` file that the wrappers read before any Composer code runs, and the image the resident `composer-agent`/`composer-executor` pair run inside the stack — because a deployer on `:beta` talking to an agent on `:latest` is a configuration nobody chose. The retag is scoped to `debeski/composer:*` images inside the generated block, so the third-party socket proxy and anything outside the markers are untouched. Both flags are confirmation-gated (`-y` honoured), mutually exclusive, and dry-run first: the channel file is written only after the Compose edit validates. Plain `composer check` reports drift between the two halves without changing anything, and `check --fix` preserves the channel. An explicit `COMPOSER_SELF_IMAGE` pin always wins over the channel — an operator who pinned an image asked for that image.

Composer 1.3.10 accepts DjangoLux release-manifest schemas 1 and 2. Schema-2 package updates enforce `install.inline`, migration rollback safety, and `requires.services.composer`; update both the deployer (`self update`) and resident pair (`agent update`) before applying a release whose manifest raises that floor. `check --fix` continues to normalize deployment topology, retired local DLUX tools wiring, restart labels, dev overrides, and deployer/resident version drift.

`composer stop [-v] [-p] [-y] [-f FILE] [-d] [service...]` stops and removes the whole Compose project when unscoped. Pass service names to use `docker compose stop` for only those containers. Secrets, health checks, and post-start tasks are skipped. `-v`/`--volumes` and `-p`/`--purge` are destructive and project-wide, so they cannot be combined with service names and require a typed confirmation (see below). `composer down ...` is an alias and `composer --down ...` remains a flat-flag equivalent. See `composer stop --help`.

`composer log [-n N|all] [-F] [-t] [--since TIME] [-f FILE] [-d] [service...]` reads Compose logs for the whole stack (interleaved, one color per service) or for the services you name. Defaults to the last 50 lines per service; `-n all` (or `-n 0`) lifts the limit, `-F`/`--follow` streams, `-t`/`--timestamps` prefixes each line, and `--since`/`--until` bound the window. `composer logs` is an alias. See `composer log --help`.

`composer agent watch --trigger-file PATH [--interval N]` runs composer as a resident, in-compose updater. It watches the trigger file and, on each new request (a changed `token`, or the file's `mtime`), runs the full `composer update` pipeline (pull → version gate → recreate → health → post_start). The processed token and child exit code are recorded in `<trigger-file>.ack`, so a request is applied once and survives a restart. Add `--status-file PATH` to have each run publish [deploy status](#deploy-status); if the child exits before publishing its own terminal failure, the watcher guarantees a token-matched `failed` status so maintenance consumers are never left waiting on a dead process. See `composer agent watch --help`.

`composer agent run` is the durable successor to `agent watch`. It preserves the same local trigger/status/ack and registry-availability files, adds a typed DLUX spool, SQLite command/outbox replay, operation correlation, safe restart allowlisting, and optional outbound HTTPS long polling. Configure `COMPOSER_CONTROL_URL`, a 15-minute one-use `COMPOSER_ENROLLMENT_TOKEN`, and `COMPOSER_AGENT_STATE_DIR` (default `/var/lib/composer-agent`). Enrollment pins the normalized control URL in local state; changing panels requires revocation and re-enrollment or an explicit local state reset. Control requests reject redirects. Pending snapshots coalesce to the newest state, while commands and events retain durable replay. Local DLUX-triggered updates continue while the control plane is unavailable or the machine credential is revoked. See [Agent Protocol v1](docs/agent-protocol-v1.md).

`composer executor run` runs the privileged half of the hardened topology. The executor is the sole holder of Docker write authority: it owns the real `docker.sock`, runs the trigger-watched image-update loop, and serves typed restart/recovery operations to the agent over a private Unix socket (`COMPOSER_EXECUTOR_SOCKET`). The network-facing `composer-agent` keeps only read-only Docker access through a `docker-socket-proxy` with POST and exec disabled, and delegates every write. The generated Compose block starts it; it is not for interactive use. See [docs/executor-hardening.md](docs/executor-hardening.md).

Migrate an existing generated DLUX project with Composer itself:

```bash
./start.sh self update
./start.sh agent enable
./start.sh agent enable --apply
./start.sh executor enable
./start.sh executor enable --apply
```

The default dry run prints the exact Compose diff. Apply accepts only a recognized
DLUX-generated updater block, validates the candidate with `docker compose config`
before writing, preserves the original under
`.xclude/dlux-agent-bootstrap/<timestamp>/`, and replaces it atomically. The
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
`composer executor enable` takes the second step: it hardens an existing
`composer-agent` stack by moving Docker write authority into a new
`composer-executor` service, demoting `docker-socket-proxy` to read-only, and
leaving the agent to observe and delegate. It uses the same guardrails —
dry-run by default, `.xclude/` backup, `docker compose config` validation,
atomic write, idempotent re-runs — and `composer check --fix` runs whichever
migration applies automatically.

A locally installed binary may instead run `composer agent enable --project-dir
/path/to/project` (or `executor enable --project-dir ...`); Composer is the sole
transformer.

When `watch` runs inside the same Compose project it is updating, it excludes the resident services from child runs by default (`composer-updater` and `composer-agent`), so pull/config/up/health/post-start operate on the application services and do not recreate the container that is supervising the update. Override the excluded name(s) with `COMPOSER_WATCH_SELF_SERVICE`, disable the default with `COMPOSER_WATCH_SELF_SERVICE=""`, or set additional exclusions with `COMPOSER_EXCLUDE_SERVICES`.

`watch` can also **detect a newer image** and publish availability for another process to act on: `--check-image IMAGE` (repeatable) + `--availability-file PATH` (and `--check-interval SECONDS`, default 3600) poll the registry's tag digest vs the locally-pulled one and write `{ "available": …, "images": [ … ] }`. It only reports *readable* differences (an unreachable registry is "unknown", never a false positive), needs no registry access from the consumer, and re-checks right after an applied update. `COMPOSER_REGISTRY_TOKEN` covers private repositories.

The published document also self-corrects after an update applied by **someone else** — `composer update` run from the project root, the executor in the hardened topology, a manual `docker compose pull`. Between scheduled checks the watched images' local digests are polled every 30s, and a moved digest re-publishes immediately, so an already-installed update stops being advertised within seconds instead of lingering until the next `--check-interval`. An unreadable local digest is "unknown" and never triggers a re-publish.

For an available image, Composer also reads remote image labels once and publishes two independent, optional fields. `version` comes from `COMPOSER_VERSION_LABEL` (default `org.opencontainers.image.version`). `manifest` comes from `COMPOSER_RELEASE_MANIFEST_LABEL` (default `org.dlux.project.release-manifest`) when that label contains a schema-1 JSON object with any of `version`, `summary`, up to eight `highlights`, or an HTTPS `release_url`. Raw JSON remains supported, but CI/build pipelines should use `base64:<URL-safe-base64-JSON>` so quoting cannot corrupt the label while it crosses YAML, action inputs, and Docker build arguments. Missing or malformed metadata is omitted without affecting digest detection or deployment.

With `--status-file` (or `--log-file PATH`), each update run also writes a clean, ANSI-free **console log** (`deploy-log.txt` beside the status file, fresh per run). Together with the deploy status, a proxy can render a live progress page + console while the app is being recreated and unreachable.

| flag | result |
| :--- | :--- |
| `-d`, `--dev` | Development mode. Loads `compose.dev.yml` on top of the base compose file (two files) and forces `DEBUG=True` / `DEBUG_STATUS=True` into every service. |
| `-u [service]` | Compact form of `update`: pull the latest image(s), then recreate immediately. Pass a service name to scope it. |
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

## surviving the terminal

Closing the terminal (or dropping an SSH session) no longer aborts a run. On the
hangup composer keeps going in the background, finishes the deploy — recreate,
health checks, post-start hooks — and moves its console output to
`composer-detached.log` in the deployment root (`COMPOSER_DETACH_LOG` overrides
the path; an already-configured `COMPOSER_LOG_FILE` is reused). Compose itself
runs in its own session, so the hangup never reaches it either.

**Ctrl+C is still the way out**: an explicit interrupt is relayed to the running
Compose command and exits `130`. `run`, `migrate`, `log`, and `logs` stay bound to the
terminal — they are the terminal — and end with it.

```bash
./start.sh update            # close the terminal: the update finishes anyway
tail -f composer-detached.log
```

## wrapper versioning

Composer owns `start.sh` and `start.ps1`. Every line in them is composer's own
invocation contract — the self image, `-i`/`-t`, the `--env-file` secrets
handoff, the `self update` route — and composer is the only component still
running in a project after it is created: the DLUX scaffold writes the wrappers
once and then refuses to overwrite them. DLUX's `scaffold_templates/project/`
copies are mirrors of the files here, not the source.

Both carry a marker on line 2:

```bash
# composer-wrapper: 2
```

It is a plain integer, bumped only when the wrapper bytes change — deliberately
**not** composer's release version. Composer ships far more often than the
wrapper does, and a marker that tracked it would report every project stale
after every release until nobody read the warning.

`composer check` compares the project's wrappers against the copies baked into
`/app/wrappers/` in the image it is running, so verification needs no registry
and works air-gapped. `wrappers-history.json` records the sha256 of every
published version, which is what separates *old but pristine* from *edited
locally*:

| Reported | Meaning |
| --- | --- |
| `is at wrapper version N` | matches the image byte for byte |
| `is wrapper version N, this composer ships M` | stale; `check --fix` updates it |
| `predates wrapper versioning` | older than the marker itself; `check --fix` updates it |
| `contents do not match what that version shipped` | local edits — diff before replacing |
| `newer than the M this composer ships` | the **image** is behind; run `self update`, not `--fix` |

`check --fix` archives the current file under `.xclude/composer-check/<stamp>/`
and then swaps it via `os.replace`, so a `start.sh` that is executing the very
check that replaces it keeps reading the file it was launched from.

```bash
./start.sh check          # report drift
./start.sh check --fix    # update, archiving the old copy first
```

To publish a new wrapper version: edit both files, bump the marker in both, run
the test suite (it prints the sha256 to record), add those to
`wrappers-history.json`, and re-copy both into DLUX's
`scaffold_templates/project/`.

## deploy status

Pass `--status-file PATH` (or set `COMPOSER_STATUS_FILE`) and composer writes an
atomic JSON document as it works, so another process (a Django admin panel, a
dashboard) can watch a deploy:

```json
{ "status": "migrating", "updated_at": "2026-07-04T08:31:38+00:00",
  "composer_version": "1.3.1", "compose_files": ["compose.yml"],
  "target_images": ["debeski/app:latest"], "target_version": "1.2.10",
  "active_version": "1.2.9" }
```

States: `starting` → `pulling` → `recreating` → `migrating` → `ready`, or
`failed` (with an `error`). `pull` reports `starting` → `pulling` → `pulled`.
The restart flow reports `restarting`/`ready`/`failed`.
Nothing is written unless configured.

## version gate

When deploying through `update` or `-u`, composer can refuse to recreate onto an image that
is **older** than the version already deployed — the one thing a pull-and-restart
can't safely undo when forward-only migrations have already run. It is opt-in and
generic: set `COMPOSER_ACTIVE_VERSION_FILE` (a JSON file, e.g. a runtime
`active.json`) and, if needed, `COMPOSER_ACTIVE_VERSION_KEY` (default `version`)
and `COMPOSER_VERSION_LABEL` (the image label to compare, default
`org.opencontainers.image.version`). With no active-version source configured the
gate is disabled. `--force` overrides a block.

Since Composer 1.3.13, an older image can also pass when the running DjangoLux
deployment confirms it will keep its newer runtime-volume release. Composer runs
`python manage.py dlux_image_gate --baked-dlux-version VERSION` in `web`, or the
service selected by `COMPOSER_RUNTIME_GATE_SERVICE`. The command ships in Dlux
1.8.12+ and checks the active release's `requires.baked_image` floor. Only a
`keep` verdict permits this exception; an abort, unavailable service, missing
command or invalid response still blocks. `--force` bypasses the check explicitly.

## mechanics
- **Secrets**: Plaintext env file (`.env` → `secrets/.env` → `.secrets/.env`); the first that satisfies the compose's required vars wins.
- **Version**: Every service gets `COMPOSER_VERSION`.
- **Runtime override**: The generated Compose override is a private system-temp file, not a project-root file, so Composer supports host-owned and read-only project mounts without extra Linux capabilities.
- **Service exclusions**: `COMPOSER_EXCLUDE_SERVICES` is a comma/space-separated service list omitted from generated runtime overrides, bulk pulls, bulk `up -d`, health checks, and diagnostics. Explicit `update SERVICE`, `pull SERVICE`, and `-u SERVICE` still target the named service.
- **UI**: Progress stays on one status line. Image pulls (`self update`, `update`/`-u`, and any `up` that has to fetch an image) draw an aggregated bar — `web ████████░░░░ 62% · 2/4 layers (2 cached) · 144MB/240MB` — built from the per-layer phases Docker reports. It names every image still in flight and drops each one as Compose reports it pulled, because Compose interleaves layers without saying which service they belong to; `(n cached)` counts the layers already on the host, so a full re-download is visible as `0 cached`. Progress only moves forward and reaches 100% when the pull actually reports it. Non-pull output keeps the plain status line, and a detached run logs coarse summaries instead of redrawing.
- **Service circles**: ⚪ not seen · 🔵 updating · 🟡 starting · 🟢 healthy · 🔴 failed. A service stops showing 🟢 the moment its pull/recreate/restart starts — the health it last reported belongs to the container being replaced — and health monitoring resolves it back.
- **Image**: Wrapper scripts target `debeski/composer:latest`, overridable with `COMPOSER_SELF_IMAGE`.

## why
Installing Python and a compose toolchain everywhere is friction. Composer keeps the toolchain inside the container and leaves the project root alone.
