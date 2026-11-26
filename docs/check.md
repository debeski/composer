# `composer check`

`composer check` is the doctor for the *outside* of a DLUX stack: Docker, the
Compose files, secrets, the resident pair, the proxy and the stack's generated
wiring. `composer check --fix` repairs what it can, guarded and archived. It is the
single evolving check that replaces per-change `enable-*` one-offs: Composer owns the
outside and hands the rest to the container(s) (`--deep`).

```
composer check [--fix] [-y] [--no-recreate] [--deep] [--deep-service S]
               [--deep-command CMD] [--json] [--beta|--stable] [-f FILE] [-d]
```

| Flag | Effect |
| --- | --- |
| `--fix` | plan the repairs, list their consequences, ask, apply them, then bring the running stack to the changed files (see [below](#how---fix-is-guarded)) |
| `-y`, `--yes` | answer the confirmation in advance ([confirmation guard](commands.md#confirmation-guard)) |
| `--no-recreate` | with `--fix`, change the files only: running services keep their definition until `./start.sh` recreates them ([after the repairs](#after-the-repairs)) |
| `--deep` | also relay the app-level doctor ([`deep`](#deep)), and walk every entry for [`app-permissions`](#app-permissions) |
| `--deep-service`, `--deep-command` | where and what `--deep` runs (default `web`, `python manage.py dlux_doctor`) |
| `--json` | print `{"results": [...], "fixes": [...]}` |
| `--beta`, `--stable` | switch Composer's own channel before checking ([channels](updates.md#channels)) |
| `-f FILE`, `-d` | select the Compose files, as for every command |

## Output and exit code

Each result has a level (`ok`, `warn`, `fail`), a name and a message, plus a fix
hint that prints only when the level is not OK. `--json` emits the same entries as
`{"level", "name", "message"}`, with `"fix"` only when a hint is set. The summary
counts `results`; repairs `--fix` applied are listed separately under `fixes`.

`check` exits `1` when any result or applied fix is a `fail`, and `0` otherwise: a
WARN never makes it fail.

The Operations card runs the same checks in `composer-executor`, and previews the
repairs `check --fix` would apply ([Operations requests](agent-protocol-v1.md#operations-requests-from-djangolux)).

## How `--fix` is guarded

0. **One at a time.** `--fix` holds `.xclude/composer-check/check-fix.lock` (`flock`)
   for the whole run. A second `--fix` on the same project is refused at once with
   `check-fix` FAIL, naming the holder's pid, host and start time, and changes
   nothing. A run keeps going when its terminal is closed or its `./start.sh` is
   killed (the Composer container finishes it), so wait for it (`docker ps` lists
   it). The system frees the lock when the holder ends, however it ends. The holder
   records itself in the file and empties it on exit, so a run that was killed
   (SIGKILL, a host crash) is reported by the next one as a `check-fix` WARN: one of
   its fixes may have stopped halfway, and this run checked everything again. Where
   `flock` is unsupported, that record decides: a holder on this host whose process
   is gone, or one older than two hours, is stale.
1. **Plan and ask.** At a terminal, `--fix` first asks the operator-choice questions
   ([questions](#questions-at-a-terminal)). It then plans every repair, prints what
   each will change, which services it will stop and remove, which it expects to
   recreate and their restart window, and asks once before applying (`-y` answers in
   advance; a non-terminal without `-y` is refused). A repair to the shared
   `x-environment` block expects every service that merges it, read from the
   resolved model the same way the recreate compares it (a scratch copy of the block
   gains one line), so a `db` that merges it is listed too. Declining reports `fix`
   WARN "Changes declined."
2. **Validate first.** Every repair validates its result with `docker compose config`
   before writing; a changed proxy file mounted by Caddy or Nginx is validated as
   well, in a throwaway container.
3. **Archive, then write as found.** Every changed deployment or proxy file is
   archived under `.xclude/composer-check/` first (the secrets file excepted: only
   its stamp changes, see [stamps](stack-migrations.md#stamps)). A rewritten file
   keeps its line endings, so a CRLF `compose.yml` stays CRLF. A file Composer
   cannot write stops the repair before anything is validated, archived or written,
   with a message naming the file and the `chmod`/`chown` that fixes it.
4. **Apply in a fixed order:** wrappers, maintenance page, stored files, front proxy,
   update policy, relay settings, Redis recovery, Redis snapshots, obsolete services
   and routes, init containers, dev compose,
   Composer install, `agent enable`, `executor enable`, resident block, updater
   runtime, post-start label, restart labels, app permissions, task worker,
   `celery` removal, stamp repair. A failed obsolete-services cleanup stops there.
5. **Finish what changed.** The stack is brought to the files the repairs wrote, in the
   same run ([after the repairs](#after-the-repairs)).
6. **Re-check.** After a repair the services are discovered again. `app-permissions`
   (deep) and `redis-persistence` are always checked again. After a run that applied
   repairs, every check a repair can change is checked again, whatever it reported
   before: `resident-commands`, `docker-proxy`, `stored-files`, `stack-schema`,
   `task-worker`, `services`, `redis-aof-corrupt`, `front-proxy`, `update-policy`,
   `relay-env`, `removed-services`, `proxy-routes`, `topology`, `compose-config`, `celery`,
   the `wrapper:` findings and `pending-recreate`. A run that applied nothing checks again
   only the first seven of those that failed. The new results replace the old, and a
   finding whose service the run removed (`docker-socket-proxy`, `celery`) leaves the
   report, as from a fresh `check`, so the report and the summary say what stands now.

The obsolete-services cleanup stops and removes only the detected obsolete
containers with a targeted `docker compose rm -sf`, applies the service and route
cleanup, rediscovers services, and verifies every pre-existing named Docker volume
remains. A running Caddy or direct-config Nginx service is reloaded; template-backed
Nginx is restarted and checked so its live rendered configuration is regenerated.
The fix never uses a broad orphan-cleaning redeploy and never removes volumes.

From the Operations card, an apply carries the digest of the files its preview read
and is refused if they have changed since, so the repair that lands is the one the
operator confirmed.

### After the repairs

Before its first write, a confirmed `--fix` reads the resolved Compose model
(`docker compose config`, with the deployment's secrets, as a deploy reads it) and
the stack's containers. After the repairs it reads the model again, from the files as
the repairs left them, and compares the two one service at a time, key order aside:

- **Dropped services are stopped and removed.** A repair that drops a service
  (`dlux-updater` retired into init containers, `composer-updater` and
  `docker-socket-proxy` replaced by the hardened pair, `celery`, the obsolete
  `db-backup`/`pgadmin`) runs `docker compose rm -s -f` for it while the file still
  defines it, and writes nothing if that fails. Whatever a repair drops and leaves
  running is then found by Compose's project and service labels and stopped and
  removed. Volumes are kept.
- **Changed services are recreated.** Every service whose definition changed and that
  was running (or restarting), and every service the repairs added to a running stack,
  is recreated with `docker compose up -d --no-deps --force-recreate`, so its
  start-up steps run, with the deployment's secrets loaded and refused when they
  cannot be read. A service its own repair already recreated (the proxy, `caddy`,
  `web` for the relay settings) is not recreated twice. Then the health check runs
  for those services alone, with the per-container settle window
  ([health check](deploying.md#health-check)). A stopped service stays stopped and
  takes its new definition when it next starts; in development (`-d`) a service the
  repairs add is left for `./start.sh -d -b`, which builds it first.
- **Never another image on the side.** A service whose image reference is unchanged
  but now names another image than its container runs (pulled or built, not
  deployed) is not recreated: `./start.sh` deploys it on purpose. A service the
  repairs add is compared through a running service on the same reference: `worker`
  added for a pulled image the stack does not run yet is left for that deploy.
- **The resident pair goes last.** From a terminal, `composer-executor` and then
  `composer-agent` are recreated after the application services are healthy. An
  Operations panel apply runs inside the pair, so it never recreates it: the apply
  reports `./start.sh` as the remaining step, and the
  [`pending-recreate`](#pending-recreate) finding shows it on the card until the pair
  runs the new definition.
- **`--no-recreate`** keeps the files-only behaviour for an operator who schedules
  the restart: nothing is recreated, dropped services are still removed (nothing else
  would remove them), and `pending-recreate` names what `./start.sh` will recreate.
  `-y` still recreates.

Each step reports itself as a fix: `fix:remove-services`, `fix:recreate` and
`fix:recreate-resident`. A recreate that fails or does not stay healthy is a FAIL
naming `./start.sh logs <service>`; the files stay changed (their originals are
archived) and the resident pair is left as it is. Every remaining step is a
`./start.sh` command.

### Questions at a terminal

Some findings are the operator's choice rather than a defect. With a terminal on
stdin and without `-y`, `COMPOSER_ASSUME_YES` or `--json`, `check --fix` asks them as
short questions before it plans, each with the recommended answer as the default
(Enter) and `s` to skip. A script, the resident pair and an Operations panel apply
are never asked: they keep the WARN and its advice, as without the questions.

| Question | Asked when | Answer |
| --- | --- | --- |
| How do users reach this server? | [`front-proxy`](#front-proxy) is a WARN because `DLUX_FRONT_PROXY` is unset, and the stack is generated (or `--fix` makes it so) | `1` a front proxy: its addresses or CIDR ranges, space-separated, checked as Caddy would; `2` directly: empty (Caddy trusts no proxy); `3` (default) keep trusting every private network: `private_ranges` |
| Which updates may the app request? | the secrets file does not set `DLUX_UPDATE_POLICY`, or sets an unknown value | `all`, `inline` or `none`; the default is the policy in force (`all` when unset) |
| Switch Redis to snapshots? | [`redis-persistence`](#redis-persistence) is a WARN and Redis is not crash-looping on a damaged file | `y` (default) rewrites its command; `n` keeps the append-only file |

An answer that sets a value adds it to the plan: the confirmation lists the line, and
after it the line is written to the secrets file in place, before any other repair,
so the front-proxy repair validates Caddy with it. Only that line changes (it is added
at the end when missing), the file keeps its owner, mode and line endings, and it is
not copied to the archive (`fix:secrets`). The services that read the value are then
recreated with it ([after the repairs](#after-the-repairs)). A value with a quote,
`#`, a backslash or a line break is refused. A skipped question changes nothing and
is asked again next time.

### The summary

A `--fix` run that prints text ends with three lists, each shown only when it has
entries:

- **Healthy now:** the findings its repairs fixed (a repair whose finding still warns
  is not counted), and the findings that were not OK before and are now.
- **Still needs you:** every finding and fix that is still not OK, each with one
  `./start.sh` command: the first step its advice names (`./start.sh check --fix` when
  the repair comes before a restart, `./start.sh logs <service>` for a service that
  keeps restarting), else `./start.sh check` once its advice is followed.
- **Skipped by your choice:** the questions you skipped.

## Findings

`check` runs the first group always, and the second only once the Compose
configuration resolves. Each entry says what it checks, when it is OK, WARN or
FAIL, the fix, and what `--fix` does.

### Environment

#### `docker`

- **Checks:** that the Docker daemon answers `docker version`.
- **OK** reachable. **FAIL** unreachable; the `compose` check is then skipped.
- **Fix:** install Docker and ensure the daemon is running.
- **`--fix`:** nothing.

#### `compose`

- **Checks:** the Docker Compose plugin and its version.
- **OK** 5.3.0 or newer (init containers supported). **FAIL** older: Compose then
  ignores the init containers (`pre_start`) DjangoLux projects use to run migrations
  before the stack starts, and gated services never start. **WARN** the plugin is
  missing, or its version cannot be read.
- **Fix:** install, or upgrade the Docker Compose plugin to 5.3.0 or newer.
- **`--fix`:** nothing.

#### `compose-files`

- **Checks:** that every active Compose file exists (`-f` and `-d` select them).
- **OK** present. **FAIL** missing.
- **Fix:** run from the project directory, or pass `-f`/`--file`.
- **`--fix`:** nothing.

#### `compose-config`

- **Checks:** that the Compose configuration resolves.
- **OK** it does. **FAIL** it does not, and every check in the second group is
  skipped.
- **Fix:** fix the reported YAML, interpolation or network error; missing variables
  are reported separately ([`env-vars`](#env-vars)).
- **`--fix`:** nothing.

#### `secrets`

- **Checks:** the secrets file (the first of `.env`, `secrets/.env`, `.secrets/.env`)
  is present, readable, parses and holds values; then, when the stack has
  `composer-executor`, that the executor can open it too.
- **OK** readable by the launcher and the executor. **WARN** no file while the
  environment supplies every value the Compose files read from it, or the executor's
  access cannot be verified (it is not running). **FAIL** no file while the Compose
  files read a value DjangoLux's generated secrets file supplies (`DJANGO_SECRET_KEY`,
  `POSTGRES_PASSWORD`, `ADMIN_PASS`, `WEB_IMAGE`, …, the keys of its
  `.secrets/.env` template) and nothing else supplies it: a deploy would run on the
  Compose file's built-in defaults, a placeholder secret key and default database
  credentials. The message lists the keys, credentials first. Also FAIL: unreadable,
  unparseable or empty (an empty file falls through to Compose defaults), or
  unreadable by the executor.
- **Fix:** restore the secrets file from your backup, or supply the values through the
  environment; for an unreadable file, the `sudo setfacl` commands for the mapped host
  UID ([when the executor cannot read the file](deploying.md#when-the-executor-cannot-read-the-file)).
- **`--fix`:** nothing to the file. The resident-block repair gives `composer-executor`
  the `DAC_OVERRIDE` capability it needs to read it
  ([executor hardening](executor-hardening.md#capabilities)).

#### `wrapper:start.sh`, `wrapper:start.ps1`

- **Checks:** each wrapper against the copy baked into the image (`/app/wrappers/`)
  and `wrappers-history.json`. Skipped when the image has no baked copies or the
  project has neither wrapper.
- **OK** byte-identical. **WARN** missing, unversioned (no marker), stale, modified
  locally, or ahead of the image.
- **Fix:** `check --fix`; for a wrapper ahead of the image, `./start.sh update self`
  instead (`--fix` would downgrade it).
- **`--fix`:** writes the baked copy, archiving the old file, for every state except
  "ahead". Details: [wrapper versioning](deploying.md#wrapper-versioning).

#### `composer-channel`

- **Checks:** that the resident pair runs the image in `.composer-image`, or, before
  the first pin, the tag of Composer's channel ([the Composer pin](updates.md#the-composer-pin)).
- **OK** the pair runs the pinned digest; or there is no pin yet and the pair runs the
  channel tag ("not pinned yet": the next Composer update pins it); or no Composer
  image is declared in the generated block. A `COMPOSER_SELF_IMAGE` pin is noted.
- **WARN** the pair runs another image than the pin, or than the channel tag when
  there is no pin; or the pin is a beta while the channel is stable.
- **Fix:** `composer check --fix` for a pair that does not follow the pin; `composer
  check --stable` for a beta pin on the stable channel; `--beta` or `--stable` to move
  both halves to a channel.
- **`--fix`:** pins the generated block's `debeski/composer` lines to the existing
  `.composer-image` (validated, original kept under `.xclude/composer-check/`). It
  never resolves a new pin, so `check` stays offline. It preserves the channel. See
  [channels](updates.md#channels).

### Stack

#### `env-vars`

- **Checks:** that every variable the Compose files require has a value in the
  environment, the inherited secrets or an env file.
- **OK** all set. **FAIL** names the missing ones.
- **Fix:** add them, or give them a `${VAR:-default}`.
- **`--fix`:** nothing.

#### `topology`

- **Checks:** which resident services the stack runs.

| Stack has | Level | `--fix` |
| --- | --- | --- |
| `composer-agent` + `composer-executor` (hardened) | OK | nothing (a leftover proxy is the `docker-proxy` finding) |
| `composer-agent` + a proxy, no executor (agent-only) | WARN | hardens it (`executor enable`) |
| `composer-agent`, no executor, no proxy | WARN | attempts the hardening, which fails without `docker-socket-proxy` |
| `composer-executor` without `composer-agent` | WARN | nothing: recreate the pair |
| legacy `composer-updater` | WARN | migrates it to the hardened pair (`agent enable`) |
| `composer-agent` and `composer-updater` together | FAIL | nothing: review by hand |
| no Composer service | FAIL | installs `composer-executor` and `composer-agent` |

Details: [topology migrations](stack-migrations.md#topology-migrations).

#### `resident-commands`

- **Checks:** that `composer-agent` and `composer-executor` use the nested
  `agent run` / `executor run` commands.
- **OK** they do. **FAIL** a flat `<role> --flag` command is left.
- **`--fix`:** inserts the `run` subcommand (resident-block repair), then re-checks.

#### `docker-proxy`

- **Checks:** whether the stack still runs a `docker-socket-proxy`. No finding without
  one.
- **OK** a read-only proxy on a stack without `composer-executor` (a legacy agent
  reads through it). **WARN** a read-only proxy beside `composer-executor`: it lets
  `composer-agent` read every container's environment and files, and the agent does
  not use it. **FAIL** `POST` or `EXEC` is enabled.
- **`--fix`:** removes the proxy from the generated block, its container first
  (resident-block repair or `executor enable`), then re-checks. A proxy outside the
  generated block is removed by hand. Why:
  [no Docker proxy](executor-hardening.md#no-docker-proxy).

#### `removed-services`

- **Checks:** for services DLUX retired: `pgadmin`, `db-backup`, or the legacy
  `db_backup` spelling.
- **OK** none. **WARN** names them.
- **`--fix`:** removes their definitions and containers and keeps their volumes
  ([guarded cleanup](#how---fix-is-guarded)).

#### `dlux-updater-executor`

- **Checks:** a stack that still has a `dlux-updater` service: whether something runs
  the update loop, and whether DjangoLux hands updates to Composer.
- **OK** no `dlux-updater`; or DjangoLux older than 1.8.0 (it still updates
  in-container); or 1.8.0+ with a Composer service mounting the runtime volume.
  **WARN** the DjangoLux version cannot be read. **FAIL** no agent, executor or
  updater runs the update loop; or that service does not mount `dlux_runtime`.
- **Fix:** install the Composer services; or add
  `dlux_runtime:/opt/dlux-runtime:rw` to the loop service and recreate it.
- **`--fix`:** installs the Composer services when there are none. Retiring
  `dlux-updater` itself is a separate repair ([init containers](#repairs-without-a-finding-of-their-own)).

#### `proxy-routes`

- **Checks:** recognized pgAdmin routes in `.proxy/Caddyfile`,
  `.proxy/default.conf.template` and `.nginx/nginx.conf`, independently of the
  services.
- **OK** none. **WARN** recognized legacy routes. **FAIL** unrecognized customized
  pgAdmin routes.
- **`--fix`:** removes recognized routes and reloads the proxy (this proxy-only path
  also repairs projects whose obsolete services were removed by v1.2.6); on a FAIL it
  applies nothing and fails closed for manual review.

#### `stack-schema`

- **Checks:** every stamp in every stamped stack file, compose.yml's included (a
  file may carry a `# dlux stack schema N` header above its `DLUX_STACK_SCHEMA`
  value).
- **OK** none stamped, or compose carries `DLUX_STACK_SCHEMA` and every stamp agrees.
  **WARN** files disagree with each other; a file other than compose disagrees with
  itself; other files are stamped but compose is not; or compose is stamped in a
  comment only, without the `DLUX_STACK_SCHEMA` value DjangoLux reads. **FAIL**
  compose's own stamps disagree, the trace of a restamp that changed only some of
  them: DjangoLux runs the stack as the schema `DLUX_STACK_SCHEMA` names.
- **Fix:** for a file at odds with itself, `check --fix`, which names the schema the
  stack's layout follows; otherwise bring the older files up to date using
  DjangoLux's `docs/stack-schema.md`, then restamp, or add `DLUX_STACK_SCHEMA: "<n>"`
  to compose's `x-environment`.
- **`--fix`:** each schema migration restamps every stamp in the files it moves on.
  A file whose own stamps disagree is restamped to the schema the stack's layout
  follows, last of all repairs. Details: [stamps](stack-migrations.md#stamps).

#### `maintenance-page`

- **Checks:** how the proxy serves the maintenance page, and whether the agent has
  published it. No finding without `composer-agent`.
- **OK** the published page is served (Composer's, or the project's override).
  **WARN** the legacy bind-mounted `.proxy/maintenance.html` (schema 2); or the page is
  not published on the runtime volume; or `.proxy/maintenance/` exists but the
  published page lacks the `.custom` marker.
- **`--fix`:** migrates a legacy stack (needs resident agent 1.7+), or publishes the
  page through the agent. Details: [maintenance page](maintenance-page.md).

#### `stored-files`

- **Checks:** that the proxy does not serve private `/media/` files without
  DjangoLux's check.
- **OK** no `/media/` route, or private. **WARN** custom `/media/` rules. **FAIL**
  (security) the legacy public `/media/` route.
- **Fix:** on DjangoLux 1.15.3+, `check --fix` (stack schema 4). On an older one, the
  way there, worked out from the release data: `./start.sh dlux update --version X`
  with X the newest release this stack still takes inline, or, when none reaches
  1.15.3, a project image on 1.15.3 or later; then `check --fix`
  ([the way out of an old stack](stack-migrations.md#the-way-out-of-an-old-stack)).
- **`--fix`:** rewrites an unmodified generated proxy, then re-checks; refuses on
  DjangoLux older than 1.15.3, naming the same way there. Details:
  [stored files](proxy.md#stored-files).

#### `fetch-metadata`

- **Checks:** that each proxy file present carries DjangoLux's `cross_site_request`
  policy.
- **OK** present, or neither proxy file exists. **WARN** missing (naming the file), or
  the proxy config cannot be read.
- **Fix:** copy the block from DjangoLux's `docs/deployment-configuration.md`, then
  `./start.sh restart caddy` (or `nginx`).
- **`--fix`:** nothing. Details: [Fetch Metadata](proxy.md#cross-site-requests-fetch-metadata).

#### `front-proxy`

- **Checks:** which front proxies Caddy takes client addresses from
  (`DLUX_FRONT_PROXY`).
- **OK** set deliberately (empty, `private_ranges`, a list), or a stack Composer does
  not manage here (no `caddy`, no Caddyfile, a custom or absent `trusted_proxies`, a
  `caddy` service with its own value). **WARN** unset; the stack cannot be told yet
  (pending `--fix`); or the config cannot be read. **FAIL** an invalid or quoted value.
- **`--fix`:** rewrites an unmodified generated stack to read `DLUX_FRONT_PROXY`;
  refuses while the value is invalid or quoted. At a terminal, it first asks how users
  reach the server and writes the answer ([questions](#questions-at-a-terminal)).
  Details: [trusted front proxies](proxy.md#trusted-front-proxies).

#### `relay-env`

- **Checks:** that `web` and `worker` see the SMTP relay's settings, and that the
  relay's certificate check can be configured.
- **OK** shared, no `smtp-relay`, or custom (naming what it left alone). **WARN** the
  settings reach `smtp-relay` only; the certificate settings are not shared;
  `x-environment` passes the password itself; or compose cannot be read.
- **`--fix`:** moves the settings into `x-environment` and recreates `web`, `worker`
  and `smtp-relay`; a password in `x-environment` is moved by hand. Details:
  [SMTP relay settings](smtp-relay-settings.md#the-relay-env-finding).

#### `relay-version`

- **Checks:** when web runs DjangoLux 1.16.2+, the DjangoLux in the relay's own image.
  No finding otherwise, without a relay, or when the version cannot be read.
- **OK** 1.16.2 or later. **WARN** older: it does not check the provider's
  certificate.
- **`--fix`:** nothing; move to a project image built on 1.16.2+. Details:
  [SMTP relay settings](smtp-relay-settings.md#the-relay-version-finding).

#### `registry-login`

- **Checks:** that the agent's watched image can be read from its registry, the way
  the agent reads it. No finding without `composer-agent` or an image.
- **OK** readable; unreachable from here (not checked); a local build. A readable
  tag that now names another image than the last deploy recorded
  ([what was deployed](deploying.md#downgrade-guard)) is noted. **WARN** a
  login is needed; the secrets file has a login the pair does not receive; no such
  tag; an untrusted certificate. **FAIL** the login is refused.
- **`--fix`:** passes the login keys to `composer-agent` and `composer-executor`
  (resident-block repair); nothing for the other outcomes. Details:
  [private registry](private-registry.md#the-registry-login-finding).

#### `update-policy`

- **Checks:** which update requests the deployment allows (`DLUX_UPDATE_POLICY`
  in the secrets file), and that the resolved stack passes it to `web`, `worker`,
  `composer-agent` and `composer-executor`.
- **OK** names the policy. **WARN** a reader does not receive it (the resident pair
  cannot enforce it, or DjangoLux cannot show it); an unknown value, which counts
  as `none`.
- **`--fix`:** adds `DLUX_UPDATE_POLICY: "${DLUX_UPDATE_POLICY:-all}"` to the
  `x-environment` block and to both resident services, and archives the original
  under `.xclude/composer-check/<UTC>/update-policy/`. The services that read it
  are then recreated ([after the repairs](#after-the-repairs)). At a terminal, it
  asks for the policy when the secrets file does not set one
  ([questions](#questions-at-a-terminal)). Details:
  [what the application may request](updates.md#what-the-application-may-request).

#### `redis-persistence`

- **Checks:** how the resolved `redis` service keeps its data, from its command. No
  finding when the stack has no `redis` service.
- **OK** RDB snapshots: `--appendonly no`, or no `--appendonly` (Redis's default),
  or a command or configuration file of the project's own, which Composer leaves
  alone. **WARN** `--appendonly yes`: a hard power-off or a killed Docker can leave
  the append-only file half-written, and Redis then refuses to start and restarts in
  a loop ([`redis-aof-corrupt`](#redis-aof-corrupt)).
- **Fix:** `check --fix`; at a terminal it asks first, and `n` keeps the append-only
  file.
- **`--fix`:** rewrites the `redis` command in the Compose file that sets it, in that
  file's own style (flow list, block list or string): `--appendonly no`, then
  `--save 60 1` unless the command already has a `--save`, `--appendfsync` dropped and
  every other flag kept, so the previous scaffold's line becomes
  `["redis-server", "--appendonly", "no", "--save", "60", "1", "--maxmemory", "500mb", "--maxmemory-policy", "allkeys-lru"]`.
  It validates the result with `docker compose config`, refuses when another file
  still turns the append-only file on, archives the original under
  `.xclude/composer-check/<UTC>/redis-persistence/`, and `redis` is recreated
  ([after the repairs](#after-the-repairs)). A snapshot is written to a temporary
  file and renamed only when complete, so a crash leaves the previous good one; up to
  about a minute of cache and sessions can be lost, so a few users sign in again.
  Application data is in Postgres. With `-y` the rewrite applies.

#### `app-permissions`

- **Checks:** that the uid the app image runs as can use the data its services mount
  (top two levels; every entry with `--deep` or `--fix`). No finding without an app
  image.
- **OK** usable. **WARN** a root image whose data is not ready for uid 10001; a named
  `USER` instead of a numeric `uid:gid`; a scan error. **FAIL** a non-root image that
  cannot use its data, or a missing mount.
- **`--fix`:** adds the group bits the app needs (only modes and groups change),
  writes a manifest for `app-permissions revert`, then re-checks deep. Details:
  [app data permissions](app-permissions.md).

#### `task-worker`

- **Checks:** that a DjangoLux 1.16+ stack has the `worker` service. No finding
  without `worker` or `celery`.
- **OK** `worker` present, or not needed yet (the reason is given), or the image the
  stack is set to run is on DjangoLux 1.16+ and present here: run `--fix` before
  deploying it. **FAIL** DjangoLux 1.16+ running and no `worker`: its tasks are
  queued and never run, so scheduled backups and jobs stop.
- **`--fix`:** adds `worker` (stack schema 5), after the other migrations. Details:
  [schema 4 to 5](stack-migrations.md#schema-4-to-5-worker).

#### `celery`

- **Checks:** on a stack with both `worker` and `celery`, whether the project image
  can run celery: one `docker run --rm --pull never --network none` of celery's image
  (no Compose hooks, mounts or environment) looks up the `celery` module and the app
  its command names (`-A`/`--app`), importing nothing of the project but that app's
  package. No finding when it can, or when the probe cannot run.
- **WARN** the image has no Celery, or no such app: celery restarts for good, while
  `worker` runs DjangoLux's tasks.
- **`--fix`:** removes celery: its service block leaves `compose.yml` and
  `COMPOSER_AGENT_RESTART_SERVICES`, and a `compose.dev.yml` override of celery moves
  to `worker` when `worker` has none. The result is
  validated with `docker compose config`, the originals are archived under
  `.xclude/composer-check/<UTC>/celery-removal/`, the container is stopped and
  removed, and volumes are kept; restoring the archived files brings celery back.
  It also runs in the same `--fix` that adds `worker`. Details:
  [schema 4 to 5](stack-migrations.md#schema-4-to-5-worker).

#### `services`

- **Checks:** the stack's containers (`docker compose ps --all`, with each one's
  restart count from `docker inspect`). No finding when Docker reports none.
- **OK** nothing restarting or unhealthy. **WARN** a service is unhealthy. **FAIL** a
  service is crash-looping: restarting now, or restarted 3+ times and started less
  than 2 minutes ago. The message gives the restart count and the last error line of
  its log.
- **Fix:** read the service's log (`./start.sh logs <service>`) and fix the cause;
  for celery in an image without Celery, `check --fix` ([`celery`](#celery)).
- **`--fix`:** nothing for this finding; re-checked after the repairs.

#### `redis-aof-corrupt`

- **Checks:** when `redis` is restarting, crash-looping (as for [`services`](#services))
  or exited, the last 60 lines of its log, for Redis's own words about an append-only
  file it cannot load ("Bad file format reading the append only file", "Unrecoverable
  error reading the append only file", an invalid AOF manifest, a `redis-check-aof`
  hint). No finding otherwise.
- **FAIL** Redis refuses its damaged append-only file, so the app cannot reach its
  cache or sessions. The message quotes Redis's line.
- **Fix:** `check --fix`.
- **`--fix`:** stops `redis`, then moves the append-only files (`appendonlydir/` and
  `appendonly.aof`, or the names `--appenddirname`/`--appendfilename` give) into
  `aof-archive/<UTC time>/` on the same volume, with a one-off container from the
  redis image (`docker run --rm --network none --pull never`) that mounts only that
  volume: nothing is deleted. It switches Redis to snapshots
  ([`redis-persistence`](#redis-persistence)) and recreates `redis` even with
  `--no-recreate`, since it is down until then. Redis starts from its last snapshot,
  or empty, so users sign in once more. When the files cannot be moved, `redis` is
  still recreated on snapshots, which ignore them.

#### `pending-recreate`

- **Checks:** the services a `check --fix` left on the definition they were created
  from (`--no-recreate`, the resident pair after an Operations panel apply, a
  recreate it refused), recorded with their container in
  `.xclude/composer-check/pending-recreate.json`. No finding when there is no record,
  or once each recorded container has been replaced.
- **WARN** a recorded container still runs: a restart keeps it, and with it the old
  definition.
- **Fix:** run `./start.sh`, which recreates the services whose definition changed.
- **`--fix`:** nothing for this finding; checked again after the repairs.

#### `versions`

- **Checks:** the deploying Composer's version against the resident executor's.
- **OK** equal, or the resident version cannot be read (not running or not enrolled).
  **WARN** drift.
- **Fix:** update the executor's image so both match, if that matters for the change
  you're shipping (`./start.sh agent update`, or `./start.sh update self` when the
  deployer is the older one; [commands](commands.md#composer-itself-and-the-resident-pair)).
- **`--fix`:** nothing.

#### `deep`

- **Checks:** only with `--deep`: relays the in-container app-level doctor, `python
  manage.py dlux_doctor` in `web` by default (`--deep-service`, `--deep-command`), and
  prints its output. When that service runs under DjangoLux's runtime supervisor, the
  doctor does too (`--no-watch`), so it reports the active release rather than the
  package baked into the image.
- **OK** passed. **WARN** unavailable, or it reported issues.
- **`--fix`:** nothing.

## Repairs without a finding of their own

`--fix` also applies these, each listed under `fixes`:

| Repair | What it does |
| --- | --- |
| init containers | retires `dlux-updater` into `pre_start` init containers (needs Compose 5.3.0+ and DjangoLux 1.8.0+; otherwise a `dlux-init-containers` WARN says why) |
| dev compose | with `-d` and a `compose.dev.yml`: normalizes the dev override (a `dev-compose` WARN when blocked) |
| updater runtime | rewrites retired local `tools.*` commands to package modules and drops `tools/` mounts, gated on the image's DjangoLux (a `dlux-updater-runtime` WARN when blocked) |
| post-start label | installs or normalizes the `org.dlux.post-start` migrator label |
| restart labels | adds missing `org.dlux.restart` labels |
| resident block | normalizes the generated resident block: nested commands, `DAC_OVERRIDE`, no Docker proxy (its container removed), the registry login keys |

What each one changes: [retired wiring](stack-migrations.md#retired-wiring-check---fix-normalizes).
