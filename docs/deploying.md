# Deploying

How `./start.sh` brings a stack up: the wrappers, secrets, post-start tasks, the
status Composer publishes while it works, and how a run behaves. The commands and
their flags are in the [commands reference](commands.md); updates are in
[updates and channels](updates.md).

## Wrappers

Put `start.sh` or `start.ps1` in your project root. The DLUX scaffold writes them
once; Composer owns them from then on and `check --fix` keeps them current.

Every line in them is Composer's own invocation contract: the self image, `-i`/`-t`,
the `--env-file` secrets handoff, the `update self` route and the deployer refresh
before `update`. Composer is the only component still running in a project after
it is created: the DLUX scaffold writes the wrappers once and then refuses to
overwrite them. DLUX's `scaffold_templates/project/` copies are mirrors of the files
here, not the source. The wrappers also read the persisted `.composer-channel` file
before any Composer code runs (see [channels](updates.md#channels)).

### Wrapper versioning

Both carry a marker (line 2 of `start.sh`, after the shebang; line 1 of
`start.ps1`):

```bash
# composer-wrapper: 8
```

It is a plain integer, bumped only when the wrapper bytes change, deliberately
**not** Composer's release version. Composer ships far more often than the wrapper
does, and a marker that tracked it would report every project stale after every
release until nobody read the warning.

`composer check` compares the project's wrappers against the copies baked into
`/app/wrappers/` in the image it is running, so verification needs no registry and
works air-gapped. `wrappers-history.json` records the sha256 of every published
version, which is what separates *old but pristine* from *edited locally*:

| Reported | Meaning |
| --- | --- |
| `is at wrapper version N` | matches the image byte for byte |
| `is wrapper version N, this composer ships M` | stale; `check --fix` updates it |
| `predates wrapper versioning` | older than the marker itself; `check --fix` updates it |
| `contents do not match what that version shipped` | local edits: diff before replacing |
| `newer than the M this composer ships` | the **image** is behind; run `update self`, not `--fix` |

`check --fix` archives the current file under `.xclude/composer-check/<stamp>/` and
then swaps it via `os.replace`, so a `start.sh` that is executing the very check
that replaces it keeps reading the file it was launched from.

```bash
./start.sh check          # report drift
./start.sh check --fix    # update, archiving the old copy first
```

Publishing a new wrapper version is a release step: see
[releasing](RELEASING.md#cutting-a-release).

## Secrets

Composer resolves secrets automatically. The wrappers look for a plaintext env file
(`.env`, `secrets/.env`, then `.secrets/.env`) and pass the first one that exists to
Composer as `--env-file`. They refuse to start, naming the files and the keys, when
another of those files holds keys the first one lacks: a stray project-root `.env`
would otherwise shadow `.secrets/.env` and the deploy would run on compose defaults.
Move the file you do not use into `.xclude/`, or merge them.

Docker reads the file as `--env-file`, so every value line is `KEY=value`: no
`export`, no spaces around `=`. The wrappers refuse any other shape and name the
line. Compose variables reach Composer only through this file: a variable set in the
shell (`WEB_IMAGE=… ./start.sh`) is not passed, and the wrappers warn when the
Compose file reads one that is set only there.

`check --fix` writes to that file only when an operator at a terminal answers one
of its questions (`DLUX_FRONT_PROXY`, `DLUX_UPDATE_POLICY`): the one line is set in
place, the file keeps its owner, mode and line endings, and it is never copied
([questions](check.md#questions-at-a-terminal)).

### The resident pair keeps the values

A resident `composer-agent` must retain the same values for later image-update
runs. The `start.sh`/`start.ps1` wrappers pass the selected file to the one-shot
Composer container with Docker's `--env-file`; when Composer creates or recreates
`composer-agent`, its private mode-`0600` runtime override forwards those values
and a key manifest only to that service. The agent and its update children then use
the inherited environment instead of reopening the host bind-mounted file. Host
mode-`0600` secrets therefore need no ACL or capability exception.

### When the executor cannot read the file

The terminal launcher and the deployment executor can map to different host UIDs,
especially with Docker user-namespace remapping. A readable file in your shell
therefore does not establish that the executor can read it. Operations deployment
checks run in the executor's identity, so `./start.sh check` verifies executor
access too, and a readable host-side file cannot conceal a resident-only permission
failure. The network-facing agent does not need secrets access.

An unreadable file prevents the executor from validating deployment configuration
and can block recreates/updates that need those values. The finding identifies the
affected host UID and prints specific `sudo setfacl` commands for the Linux VM:

1. run those commands in the VM project directory. sudo asks for the VM account
   password locally, never in DjangoLux;
2. install the host's `acl` package if needed;
3. rerun `./start.sh check`.

Keep the file restricted; do not make it world-readable or give the network-facing
agent access to silence a warning.

### First deploy without importing `config.json`

`./start.sh --skip-config` leaves DjangoLux's initial setup wizard available even
when the image contains `config.json`: see
[manual first-deploy setup](first-deploy.md).

## Post-start tasks

A service opts into a post-start task with an `org.dlux.post-start` label naming
the command:

```yaml
  web:
    labels:
      org.dlux.post-start: "python -m dlux.updater.supervisor --no-watch -- python manage.py migrator"
```

Composer runs it once, after the stack is healthy, appending whichever flags the
run was given. A label is used rather than Compose's native `post_start` hook
because Compose runs that hook itself, on container start, without Composer's
flags, which meant two migrators running at the same time.

- Deployments still carrying a native `post_start` block keep working (Composer
  execs it and says so); `composer check --fix` folds it into the label.
- Existing DLUX updater stacks missing both declarations use a narrowly gated
  compatibility migrator instead of silently doing nothing, and `check --fix`
  installs the missing label.
- Post-start execution is non-interactive (`compose exec -T`), and a failed or
  unhealthy hook fails the Composer run rather than ending with a false
  "Environment ready."

### The migrator contract

When the command is DjangoLux's `migrator`, `-a APP`, `-mm`, and `-nm` are
forwarded and mean:

| flags | makemigrations | migrate | collectstatic |
| --- | --- | --- | --- |
| *(none)* | only apps with no `migrations/0001_*.py` | yes | yes |
| `-mm` | **all** target apps, forced | yes | yes |
| `-nm` | no | no | **yes** |

`-mm` and `-nm` are mutually exclusive. `-a APP` scopes which apps are considered;
without it the migrator auto-discovers local apps. `-mm` writes migrations into the
container filesystem, so they are lost on recreate unless the app source is
bind-mounted: it is a development affordance. `composer migrate` runs the same
migrator on demand ([commands](commands.md#migrate)).

## Deploy status

Pass `--status-file PATH` (or set `COMPOSER_STATUS_FILE`) and Composer writes an
atomic JSON document as it works, so another process (a Django admin panel, a
dashboard) can watch a deploy:

```json
{ "status": "migrating", "updated_at": "2026-07-04T08:31:38+00:00",
  "composer_version": "1.3.1", "compose_files": ["compose.yml"],
  "target_images": ["debeski/app:latest"], "target_version": "1.2.10",
  "active_version": "1.2.9" }
```

States: `starting` → `pulling` → `recreating` → `migrating` → `ready`, or `failed`
(with an `error`). `pull` reports `starting` → `pulling` → `pulled`. The restart flow
reports `restarting`/`ready`/`failed`. Nothing is written unless configured.

A run that succeeds but needs the operator's attention lists it in `warnings` (an
array of strings, present only when there is one), in every status written after it
arose. Each warning is also printed in the terminal, again after "Environment
ready", and written to the console log as a `WARNING:` line.

### Console log

With `--status-file` (or `--log-file PATH`), each update run also writes a clean,
ANSI-free console log (`deploy-log.txt` beside the status file, fresh per run).
Together with the deploy status, a proxy can render a live progress page and console
while the app is being recreated and unreachable.

## Before and after `up`

### Downgrade guard

Before every `up` that (re)creates the app services, Composer compares the app
image it is about to deploy with the newest image this deployment has deployed, and
refuses an older one. Migrations only run forwards: once a newer image's migrator
has changed the schema, older code may fail on it or write data the newer release
then reads wrongly. The guard is always on; it does not need the opt-in
[version gate](updates.md#version-gate).

- **What is compared**: the app version (the `version` in the image's
  `org.dlux.project.release-manifest` label, else `org.opencontainers.image.version`)
  and the baked DjangoLux version (`org.<project>.dlux_baked_version`). A version
  either side does not label is not compared; an image with neither label is refused
  when the recorded release had them, since nothing shows it is not older.
- **What it is compared with**: the labels of the app containers that have started
  (they ran the migrator, even if they then failed their health check; one only
  created, never started, ran nothing and is not counted), and the deploy record,
  which Composer raises after every `up` so the floor survives a `down`. The record
  is kept twice, and the newer copy counts:
  - `.xclude/composer-deploy/deployed.json` in the deploy root;
  - `state/composer-deployed.json` on the DjangoLux runtime volume, written in a
    throwaway Composer container (`--pull never`, `--network none`), so a deploy
    root Composer cannot write still keeps a record. `down -v` removes it, together
    with the database it protects.

  Each copy names the database it describes (`database`: the Postgres data volume,
  the named volume mounted at `/var/lib/postgresql/data` or `/var/lib/postgresql`,
  and its creation time). A copy whose volume no longer exists, or was created
  again since (`down -v`, a restore onto a new volume, a move to another host), is
  not compared with the target: the run says so, and the deploy's `up` replaces it.
  A copy without `database`, or a stack whose volume Docker cannot describe, is
  compared.
- **The database itself**: the records cannot follow the data (a restore into a new
  volume, a move to another host, a fresh clone with no record), so the guard also
  reads the database's applied migrations (`django_migrations`) in the stack's own
  Postgres service, with `docker compose exec` and the container's own
  `POSTGRES_USER` / `POSTGRES_DB`. A Postgres service that is not running is started
  first, as the `up` that follows would start it, and stopped again when the deploy
  is refused. It lists the migrations the target ships in a throwaway container of
  the target image (`--network none`, `--pull never`, read-only, nothing imported):
  the `migrations/` packages under its working directory, `PYTHONPATH` and
  site-packages, the names a squashed migration `replaces` included, and the
  runtime volume's active DjangoLux release when there is one. The database has
  newer code's migrations when it holds a migration of an app the target ships under
  a name the target does not know; an app the target does not ship at all is not
  compared. A stack with no Postgres service, or an empty database, has nothing to
  compare. When the database or the image cannot be read, the run warns and
  compares the records and containers only.

  When neither copy can be written, the run says so as a
  [deploy-status](#deploy-status) warning: the guard then relies on container labels
  alone, which a `down` removes. When only the deploy-root copy fails, the console
  log notes it.
- **Older DjangoLux code on a newer schema**: a target whose only difference is an
  older DjangoLux (the app version is not older, and every migration the target does
  not know belongs to DjangoLux or its dependencies, none to the project's own apps)
  follows DjangoLux's own rollback rule, `migrations.rollback_compatible` in the
  DjangoLux release manifest. The release being left is the newest DjangoLux the
  records and containers name for this database, or the oldest readable release that
  knows every migration the target does not, whichever is newer. It must be readable
  (a release on the runtime volume, or a local image this deployment ran: the
  records' images and the app containers' images), and it and every readable
  release between the target and it must declare `rollback_compatible: true`. Then
  the deploy goes ahead with a warning: the database keeps the newer migrations,
  migrations are never reversed, and restoring a backup taken before the newer
  release later needs "Ignore version mismatch". Otherwise it is refused, saying why.
- **Refused**: the run stops before anything is recreated and names each older
  version, or the migrations the target does not know. To go back on purpose,
  restore a database backup taken before the newer release, then deploy with
  `--force`; a forced deploy becomes the new record.
- **Not compared**: `-d` and `-b` runs (local builds carry no labels) and scoped
  runs that do not touch the app services.
- **What was deployed**: the record also names the image `up` used, by tag
  (`image`), by repository digest (`repo_digest`, the pulled image's `RepoDigests`
  entry for that repository; empty for a local build) and by `image_id`. That is the
  exact rollback target even after the tag has moved (`WEB_IMAGE=<repo_digest>`),
  and `check`'s [`registry-login`](check.md#registry-login) finding notes when the
  tag now names another image. It is a record, not a protection: an update still
  pulls whatever the tag names, and labels can be set by anyone who can push it.
  Composer's own images are pinned to signed digests instead
  ([the Composer pin](updates.md#the-composer-pin)).

The record's `dlux` is the baked DjangoLux of the newest image deployed, not the
release that runs: an inline update (`dlux update`) changes what runs without a
deploy, so after one the record still names the image's baked version (1.16.3 under
an active 1.17.0b1, for example). The guard still holds there. DjangoLux's runtime
supervisor serves a volume release only while it is newer than the image's baked
package, so an image baked older than the active release keeps running the active
release, and an image below that release's floor is refused by the check below. The
active release stops running only through `dlux rollback`, which checks migration
compatibility itself, or with the volume, which `down -v` removes along with the
database.

The baked DjangoLux version is also checked against the image floor of the release
the runtime volume has active: `requires.baked_image` (legacy `image_baseline`) in
`releases/<active>/dlux/release-manifest.json`. Composer reads it in a throwaway
container of its own image (`--pull never`, `--network none`, the volume mounted
read-only), and only when the volume already exists. From DjangoLux 1.17 the
runtime supervisor serves an image's baked package when the image is below that
floor; an older image runs its own supervisor, which never checks, so this
pre-`up` refusal is the only defence there.

### One operation at a time

A deploy (`./start.sh`, `update`, `restart`, `stop`, `agent update`, and the
resident pair's updates and recoveries), a `check --fix` and an Operations-panel
apply hold one lock per project, `.xclude/composer-check/check-fix.lock` (`flock`),
for their whole run. A second one started meanwhile is refused at once and changes
nothing; the refusal names what holds the lock (the command, its container when it
runs in one, pid, host and start time) and how to stop a holder that waits for an
answer (`docker stop <container>`). An Operations apply refused this way shows that
refusal in the panel. A `pull` changes no container and takes no lock. The system
frees the lock when the holder ends, however it ends; a holder that was killed is
reported by the next run, which deploys every service again. The resident pair
mounts the project read-only and takes the same lock through a read-only
descriptor.

### Before `up`

- **An interrupted recreate**: Compose recreates a service by renaming the old
  container to `<12 hex>_<name>`, creating the new one and removing the old. A
  deploy killed in between leaves the renamed container. Before `up`, Composer
  removes such a container when it is stopped (its volumes stay) and gives a
  running one its own name back when that name is free, and says so.
- **OCR without its engine**: a service that runs `python -m dlux.ocr.serve` on an
  image without the `django-lux[ocr]` extra cannot start. Composer asks the image
  (`--network none`, `--pull never`) whether it ships the engine; when it does not,
  the service is left out of the run and stopped, with a warning. Build the image
  with the extra, or add `COMPOSER_EXCLUDE_SERVICES=ocr` to the secrets file.
- **The dev overlay's project**: with `-d`, `compose.dev.yml` must not name another
  Compose project (`name:`) than `compose.yml`. The last `name:` wins in Compose, so
  it would start a second stack with fresh volumes beside the real one; the run is
  refused (exit 2) and names both.

### Health check

After `up`, every monitored service must reach healthy (running, and `healthy` when
it has a healthcheck) and its newest container must have been up for the settle
window, 10 s by default. `COMPOSER_HEALTH_SETTLE_SECONDS` sets it in seconds, `0`
turns it off: in the shell for one run (`COMPOSER_HEALTH_SETTLE_SECONDS=0 ./start.sh`,
which both wrappers pass to Composer), or in the secrets file for every run.
The window is measured from each container's own Docker `StartedAt`, so a container
that has already been up that long when the last service turns healthy (usually
`web`, after its migrations) adds no wait, and one that just started is watched
until it reaches the window. When the start time cannot be read, or lies ahead of
Composer's clock, the window runs from the first poll that found the service
healthy. The run fails, naming the service and its last log lines, when:

- a container restarts at any point from the first health poll on (a higher
  `RestartCount`, a new start time, or a poll that finds it `restarting`): a
  crash-looping service without a healthcheck reads as running between crashes, so
  one healthy poll is not proof;
- a service that was healthy leaves that state before every service has passed;
- not every service is healthy within 180 s of the last state change;
- after the post-start tasks, a container has restarted since the first health poll,
  or a service is no longer healthy. Composer then waits, when needed, until every
  container has been up for the confirmation window, 30 s by default and never
  shorter than the settle window (`COMPOSER_HEALTH_CONFIRM_SECONDS`), so a crash
  loop slower than the settle window still fails the deploy.

The failure also names the likely cause when the service's last log lines or exit
code show one: a Python module missing from the image (a service left over from an
older release, such as `celery` after Decrees 2.2), another CPU architecture, a
missing command, a port already in use, an unreachable database or Redis, a schema
that does not match the code, incomplete settings, a permission problem, or exit 137
(out of memory). It ends with the next steps: `./start.sh logs SERVICE`, and adding
`COMPOSER_EXCLUDE_SERVICES=SERVICE` to the secrets file to deploy without it while it
is fixed. A failed `up` and a failed `restart` carry the same diagnosis.

`restart` and inline DjangoLux updates (`dlux update`, `dlux rollback`) use the same check.

### When a deploy fails

The edge proxy (`caddy`, `nginx` or `proxy`) depends on `web`, so Compose can leave
it "Created" when the app does not come up, and the site then has no front door.
After `up`, and again when `up` or the health check fails, Composer starts any edge
proxy in scope that is not running with `docker compose up -d --no-deps`. It then
serves the [maintenance page](maintenance-page.md) on 502/503/504 while `web` cannot
answer, and the failure message says so (or says the proxy could not be started).
The failed containers stay as they are for diagnosis: putting the previous image
back after its successor migrated the database would itself be a downgrade.

## How a run behaves

- **Version**: every service gets `COMPOSER_VERSION`.
- **Runtime override**: the generated Compose override is a private system-temp
  file, not a project-root file, so Composer supports host-owned and read-only
  project mounts without extra Linux capabilities. It names only services the
  Compose files define: when a file changes during the run (a `check --fix` repair
  that drops `celery` or `docker-socket-proxy`), Composer rewrites it before its next
  Compose call.
- **Service exclusions**: `COMPOSER_EXCLUDE_SERVICES` is a comma/space-separated
  service list omitted from generated runtime overrides, bulk pulls, bulk `up -d`,
  health checks, and diagnostics. Explicit `update SERVICE`, `pull SERVICE`, and
  `-u SERVICE` still target the named service.
- **Progress**: progress stays on one status line. Image pulls (`update self`,
  `update`/`-u`, and any `up` that has to fetch an image) draw an aggregated bar,
  `web ████████░░░░ 62% · 2/4 layers (2 cached) · 144MB/240MB`, built from the
  per-layer phases Docker reports. It names every image still in flight and drops
  each one as Compose reports it pulled, because Compose interleaves layers without
  saying which service they belong to; `(n cached)` counts the layers already on the
  host, so a full re-download is visible as `0 cached`. Progress only moves forward
  and reaches 100% when the pull actually reports it. Non-pull output keeps the plain
  status line, and a detached run logs coarse summaries instead of redrawing.
- **Service circles**: ⚪ not seen · 🔵 updating · 🟡 starting · 🟢 healthy ·
  🔴 failed. A service stops showing 🟢 the moment its pull/recreate/restart starts
  (the health it last reported belongs to the container being replaced) and health
  monitoring resolves it back.
- **App data**: before `up`, a non-root app image gets access to its data, in
  development mode (`-d`, where `web` and `worker` are built) as in production; see
  [app data permissions](app-permissions.md#before-every-start-of-the-app).
- **Closing the terminal** does not stop a deploy, and destructive flags ask first:
  see [commands](commands.md#closing-the-terminal).
