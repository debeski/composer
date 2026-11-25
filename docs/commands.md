# Commands

Every command runs the same way through the wrappers (`./start.sh …`,
`./start.ps1 …`) or a native `python -m composer …`. `composer --help` lists them,
and `composer <command> --help` gives each one's options. What a deploy does is in
[deploying](deploying.md); what each update path does is in
[updates and channels](updates.md).

## Global flags

`./start.sh` with no command brings the stack up. These flags apply to it and,
where they make sense, to the commands below.

| flag | result |
| :--- | :--- |
| `-f FILE`, `--file FILE` | Use an alternate Compose file. |
| `-d`, `--dev` | Development mode. Loads `compose.dev.yml` on top of the base compose file (two files) and forces `DEBUG=True` / `DEBUG_STATUS=True` into every service. |
| `-b`, `--build` | Rebuild images during startup. The image ships `docker-buildx-plugin`, so builds run on BuildKit instead of the classic builder and its per-step containers. |
| `--skip-config` | Leave DjangoLux's initial setup wizard available even when the image contains `config.json` (DjangoLux 1.9.4+): [manual first-deploy setup](first-deploy.md). |
| `-nm`, `-mm`, `-a APP` | Passed to DjangoLux's migrator: [the migrator contract](deploying.md#the-migrator-contract). |
| `-u [service]` | Compact form of `update`: pull the latest image(s), then recreate immediately. Pass a service name to scope it. |
| `--force` | Deploy an older image anyway: bypasses the preflight [version gate](updates.md#version-gate) and the [downgrade guard](deploying.md#downgrade-guard). Restore a matching database backup first. |
| `--status-file PATH` | Write a JSON [deploy-status](deploying.md#deploy-status) file to `PATH` (overrides `COMPOSER_STATUS_FILE`). |
| `--down` | Stop everything (see [`stop`](#stop)). |
| `-v`, `--volumes` | With `--down`: remove volumes too. Confirmation required. |
| `-p`, `--purge` | With `--down`: also remove built untagged images, volumes, networks, orphans, and dangling build cache. Confirmation required. |
| `-y`, `--yes` | Answer the confirmation prompt in advance. |
| `--version` | Print Composer's version and exit. |

## Deploying and running

### `update`

`composer update [-b] [--force] [--skip-config] [-nm|-mm] [-a APP] [--status-file PATH] [-f FILE] [-d] [service...]`

Updates the Composer deployer first (wrapper 5+), then pulls the latest image(s),
recreates the affected containers, waits for health checks, and runs post-start
tasks. Name services to scope both the pull and the recreate; naming them leaves the
deployer as it is. `-u [service]` is the compact form. Details:
[image updates](updates.md#image-updates).

### `pull`

`composer pull [--status-file PATH] [-f FILE] [-d] [service...]`

Only downloads images. It never recreates containers, runs health checks, or
executes post-start tasks. It replaces the retired `-uo` and `update -o` forms.

### `restart`

`composer restart [-f FILE] [-d] [--status-file PATH] [service]`

Restarts running containers through `docker compose restart`, then waits for their
health checks. Containers are preserved and post-start tasks are skipped. Pass a
service to restart only that service. `composer -r ...` and `composer --restart ...`
are short leading aliases.

### `stop`

`composer stop [-v] [-p] [-y] [-f FILE] [-d] [service...]`

Stops and removes the whole Compose project when unscoped. Pass service names to use
`docker compose stop` for only those containers. Secrets, health checks, and
post-start tasks are skipped. `-v`/`--volumes` and `-p`/`--purge` are destructive and
project-wide, so they cannot be combined with service names and require a typed
[confirmation](#confirmation-guard). `composer down ...` is an alias and
`composer --down ...` a flat-flag equivalent.

### `run`

`composer run [-m] [-s] [-F] [-f FILE] [-d] <service> <command...>`

Runs a command inside a service instead of typing `docker exec`/`docker run` by
hand. Defaults to `docker compose exec <service> …`.

- `-m`/`--manage` prepends `python manage.py` (e.g. `./start.sh run -m web migrate
  --noinput`), run under DjangoLux's runtime supervisor when the service itself is, so
  it uses the active release even with a `manage.py` too old to resolve it;
- `-s`/`--shell` runs the command via `sh -c` so pipes/`&&` work;
- `-F`/`--fresh` uses a one-off `docker compose run --rm`.

TTY is auto-detected: a terminal gets an interactive session, a pipe gets `-T` and
reads your piped stdin (`echo yes | ./start.sh run -m web collectstatic`). The
wrapper nests two ptys, so type-ahead into an interactive prompt is dropped until the
inner container attaches: prefer `--noinput` in scripts.

### `migrate`

`composer migrate [-s SERVICE] [-f FILE] [-d] [MIGRATOR_ARGS...]`

Runs DjangoLux's supervised migrator directly in the running `web` service.
`--service` selects another service, and every unrecognized argument is passed to
the migrator: `./start.sh migrate -mm -a documents`, `./start.sh migrate -nm`, or
`./start.sh migrate -d --service web -- -mm`. The command uses the service's
`org.dlux.post-start` command when present, otherwise it matches the existing
`dlux-updater` supervisor and defaults to the packaged supervisor; its output and
exit code remain attached to the terminal. Flags:
[the migrator contract](deploying.md#the-migrator-contract).

### `log`

`composer log [-n N|all] [-F] [-t] [--since TIME] [--until TIME] [--no-color] [-f FILE] [-d] [service...]`

Reads Compose logs for the whole stack (interleaved, one color per service) or for
the services you name. Defaults to the last 50 lines per service; `-n all` (or `-n
0`) lifts the limit, `-F`/`--follow` streams, `-t`/`--timestamps` prefixes each line,
and `--since`/`--until` bound the window. `composer logs` is an alias.

## Checking and repairing

### `check`

`composer check [--fix] [-y] [--no-recreate] [--deep] [--deep-service S] [--deep-command CMD] [--json] [--beta|--stable] [--allow-unverified-composer] [-f FILE] [-d]`

Diagnoses the outside of a stack, and with `--fix` repairs it, then stops and removes
the services the repairs drop and recreates the running services they change
(`--no-recreate` leaves that to `./start.sh`). Reference, with every finding:
[`composer check`](check.md).

### `app-permissions revert`

`composer app-permissions revert [-f FILE] [-d] MANIFEST`

Undoes a group-access grant `check --fix` made on app data, from the
`app-permissions.tsv.gz` it wrote: [app data permissions](app-permissions.md#undoing-a-grant).

## Composer itself and the resident pair

| Command | Does |
| --- | --- |
| `composer update self [--pin] [--allow-downgrade] [--allow-unverified-composer]` | pins the deployer to the channel's newest signed release (`.composer-image`), pulls it and leaves the stack alone; `--pin` only writes the pin, which the wrapper then pulls; `self update` is a deprecated alias ([the Composer pin](updates.md#the-composer-pin)) |
| `composer agent update [--allow-downgrade] [--allow-unverified-composer]` | pins the same release, pulls the resident pair's image and recreates `composer-agent` and `composer-executor` |
| `composer agent restart` | restarts the pair and waits for health |
| `composer agent off` | stops the pair |
| `composer agent enable [--apply]` | migrates a legacy or agent-only stack to the hardened pair; dry run by default ([topology migrations](stack-migrations.md#topology-migrations)) |
| `composer executor enable [--apply]` | hardens an agent-only stack; dry run by default |
| `composer agent run` | the resident agent; started by the generated Compose block ([the resident pair](resident-pair.md#composer-agent)) |
| `composer executor run` | the resident executor; started by the generated Compose block, not for interactive use |
| `composer agent watch --trigger-file PATH [--interval N]` | the plain watcher ([`agent watch`](resident-pair.md#agent-watch)) |
| `composer maintenance publish [--override DIR]` | republishes the maintenance page, or a project's own ([maintenance page](maintenance-page.md)) |
| `composer relay list`, `composer relay approve` | reviews and approves the project's declared outbound operations ([egress relay](relay.md)) |

The lifecycle commands act on the pair together and leave the application alone:
[lifecycle commands](resident-pair.md#lifecycle-commands).

### `agent check`

`composer agent check [--json] [--no-publish] [--availability-file PATH] [-f FILE] [-d] [IMAGE ...]`

Checks image availability without pulling, recreating, or enabling maintenance, and
publishes the result for DjangoLux ([refreshing it now](updates.md#refreshing-it-now-agent-check)).
`--json` emits the availability document, including optional candidate `version`
and release `manifest` metadata.

- Exit `0`: every registry lookup and any requested publication succeeded, even when
  an update exists.
- Exit `1`: a lookup was unknown, configuration discovery failed, or publication
  failed (for example, the resident service is stopped). An unknown lookup prints its
  reason, such as a registry that needs a login.
- Exit `2`: invalid images, or no image was found. Digest-pinned references are
  rejected.

The [registry login](private-registry.md) (`COMPOSER_REGISTRY_USERNAME`,
`COMPOSER_REGISTRY_PASSWORD`), `COMPOSER_REGISTRY_TOKEN`, `COMPOSER_VERSION_LABEL`,
and `COMPOSER_RELEASE_MANIFEST_LABEL` apply as in the agent.

## Inline DjangoLux releases

What each step does: [inline DjangoLux updates](updates.md#inline-djangolux-updates).

| Command | Does |
| --- | --- |
| `composer dlux check [--version V] [--availability-file PATH] [-f FILE] [-d]` | publishes what is available to `state/package-available.json` |
| `composer dlux update [--dry-run] [--version V] [--retry-quarantined] [--backup data\|full\|skip] [--restart-service SERVICE] [--status-file PATH] [-f FILE] [-d]` | applies the newest release on the channel, or `--version V` |
| `composer dlux rollback [--backup data\|full\|skip] [--restart-service SERVICE] [--status-file PATH] [-f FILE] [-d]` | restores the previous release |
| `composer dlux channel [stable\|beta] [-f FILE] [-d]` | reports or requests DjangoLux's channel ([channels](updates.md#djangoluxs-channel)) |

`dlux update` and `dlux rollback` exit `0` on success (and when a rollback has
nothing older to return to); `1` when refused (another operation is running, a
quarantined release without `--retry-quarantined`) or failed; `2` on invalid
arguments (`--retry-quarantined` without `--version`) or when no runtime volume can
be found; and `3` when the rollback was also unhealthy: a deployment that needs a
human, which a caller must not retry.

## Confirmation guard

Actions that destroy data (`-v`/`--volumes` and `-p`/`--purge`) stop and print
exactly what will be removed, then wait for you to type `y` or `yes`. Anything else
aborts before Docker is called.

Pass `-y`/`--yes` (or set `COMPOSER_ASSUME_YES=1`) to confirm up front. The guard
fails **closed**: when stdin is not a terminal (CI, cron, a piped script) and neither
the flag nor the variable is set, the run is refused instead of silently proceeding.
`composer check --fix` and `check --beta`/`--stable` ask the same way before they
apply. At a terminal, `check --fix` also asks a few short operator-choice questions
first ([questions](check.md#questions-at-a-terminal)); with `-y`,
`COMPOSER_ASSUME_YES` or no terminal it asks none. The variable reaches a native `python -m composer` run only: the
`start.sh`/`start.ps1` wrappers do not pass it into the deployer container, so a
non-interactive run through them needs the flag (`./start.sh check --fix -y`).

```bash
./start.sh stop            # no prompt, nothing is destroyed
./start.sh stop web        # only the web service
./start.sh stop -v         # prompts: type y or yes
./start.sh stop -p -y      # no prompt, purges immediately
```

## Closing the terminal

Closing the terminal (or dropping an SSH session) does not abort a run. On the
hangup Composer keeps going in the background, finishes the deploy (recreate, health
checks, post-start hooks) and moves its console output to `composer-detached.log` in
the deployment root (`COMPOSER_DETACH_LOG` overrides the path; an already-configured
`COMPOSER_LOG_FILE` is reused). Compose itself runs in its own session, so the hangup
never reaches it either.

**Ctrl+C is still the way out**: an explicit interrupt is relayed to the running
Compose command and exits `130`. `run`, `migrate`, `log`, and `logs` stay bound to
the terminal (they are the terminal) and end with it.

```bash
./start.sh update            # close the terminal: the update finishes anyway
tail -f composer-detached.log
```
