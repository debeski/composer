# `composer check`

`composer check` is the doctor for the *outside* of a DLUX stack: Docker, the
Compose files, secrets, the resident pair, the proxy and the stack's generated
wiring. `composer check --fix` repairs what it can, guarded and archived. It is the
single evolving check that replaces per-change `enable-*` one-offs: Composer owns the
outside and hands the rest to the container(s) (`--deep`).

```
composer check [--fix] [-y] [--deep] [--deep-service S] [--deep-command CMD]
               [--json] [--beta|--stable] [-f FILE] [-d]
```

| Flag | Effect |
| --- | --- |
| `--fix` | plan the repairs, list their consequences, ask, then apply them (see [below](#how---fix-is-guarded)) |
| `-y`, `--yes` | answer the confirmation in advance ([confirmation guard](commands.md#confirmation-guard)) |
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

1. **Plan and ask.** `--fix` plans every repair, prints what each will change, and
   asks before applying (`-y` answers in advance; a non-terminal without `-y` is
   refused). Declining reports `fix` WARN "Changes declined."
2. **Validate first.** Every repair validates its result with `docker compose config`
   before writing; a changed proxy file mounted by Caddy or Nginx is validated as
   well, in a throwaway container.
3. **Archive.** Every changed deployment or proxy file is archived under
   `.xclude/composer-check/` first (the secrets file excepted: only its stamp
   changes, see [stamps](stack-migrations.md#stamps)).
4. **Apply in a fixed order:** wrappers, maintenance page, stored files, front proxy,
   relay settings, obsolete services and routes, init containers, dev compose,
   Composer install, `agent enable`, `executor enable`, resident block, updater
   runtime, post-start label, restart labels, app permissions, task worker. A
   failed obsolete-services cleanup stops there.
5. **Re-check.** `resident-commands`, `docker-proxy` and `stored-files` are checked
   again when they failed, and `app-permissions` always is (deep); the new results
   replace the old.

The obsolete-services cleanup stops and removes only the detected obsolete
containers with a targeted `docker compose rm -sf`, applies the service and route
cleanup, rediscovers services, and verifies every pre-existing named Docker volume
remains. A running Caddy or direct-config Nginx service is reloaded; template-backed
Nginx is restarted and checked so its live rendered configuration is regenerated.
The fix never uses a broad orphan-cleaning redeploy and never removes volumes.

From the Operations card, an apply carries the digest of the files its preview read
and is refused if they have changed since, so the repair that lands is the one the
operator confirmed.

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
- **OK** readable by the launcher and the executor. **WARN** no file (secrets may come
  from the environment), or the executor's access cannot be verified (it is not
  running). **FAIL** unreadable, unparseable or empty (an empty file falls through to
  Compose defaults), or unreadable by the executor.
- **Fix:** for an unreadable file, the `sudo setfacl` commands for the mapped host UID
  ([when the executor cannot read the file](deploying.md#when-the-executor-cannot-read-the-file)).
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

- **Checks:** that the resident pair runs the image of Composer's channel.
- **OK** it does, or no Composer image is declared in the generated block (a
  `COMPOSER_SELF_IMAGE` pin is noted). **WARN** the pair runs another image.
- **Fix:** `composer check --beta` or `--stable` to move both halves together.
- **`--fix`:** nothing; it preserves the channel. See [channels](updates.md#channels).

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
| `composer-agent` + `composer-executor` (hardened) | OK | nothing |
| `composer-agent` + a proxy, no executor (agent-only) | WARN | hardens it (`executor enable`) |
| `composer-agent`, no executor, no proxy | WARN | attempts the hardening, which fails without `docker-socket-proxy` |
| `composer-executor` without `composer-agent` | WARN | nothing: recreate the pair |
| legacy `composer-updater` | WARN | migrates it to the hardened trio (`agent enable`) |
| `composer-agent` and `composer-updater` together | FAIL | nothing: review by hand |
| no Composer service | FAIL | installs `docker-socket-proxy`, `composer-executor` and `composer-agent` |

Details: [topology migrations](stack-migrations.md#topology-migrations).

#### `resident-commands`

- **Checks:** that `composer-agent` and `composer-executor` use the nested
  `agent run` / `executor run` commands.
- **OK** they do. **FAIL** a flat `<role> --flag` command is left.
- **`--fix`:** inserts the `run` subcommand (resident-block repair), then re-checks.

#### `docker-proxy`

- **Checks:** that `docker-socket-proxy` grants no write endpoint. No finding without
  a proxy.
- **OK** read-only. **FAIL** `POST` or `EXEC` is enabled.
- **`--fix`:** switches the write endpoints off and repins the image in the generated
  block (resident-block repair or `executor enable`), then re-checks. A proxy outside
  the generated block is edited by hand. Why:
  [the proxy is read-only and pinned](executor-hardening.md#the-proxy-is-read-only-and-pinned).

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

- **Checks:** that the stamped stack files agree on their DjangoLux stack schema.
- **OK** none stamped, or compose stamped and every other stamp agrees. **WARN**
  stamps disagree, or other files are stamped but compose is not.
- **Fix:** bring the older files up to date using DjangoLux's `docs/stack-schema.md`,
  then restamp; or add `DLUX_STACK_SCHEMA: "<n>"` to compose's `x-environment`.
- **`--fix`:** nothing for this finding; each schema migration restamps the files it
  moves on. Details: [stamps](stack-migrations.md#stamps).

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
- **Fix:** update DjangoLux to 1.15.3+, then `check --fix` (stack schema 4).
- **`--fix`:** rewrites an unmodified generated proxy, then re-checks; refuses on
  DjangoLux older than 1.15.3. Details: [stored files](proxy.md#stored-files).

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
  refuses while the value is invalid or quoted. Details:
  [trusted front proxies](proxy.md#trusted-front-proxies).

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
- **OK** readable; unreachable from here (not checked); a local build. **WARN** a
  login is needed; the secrets file has a login the pair does not receive; no such
  tag; an untrusted certificate. **FAIL** the login is refused.
- **`--fix`:** passes the login keys to `composer-agent` and `composer-executor`
  (resident-block repair); nothing for the other outcomes. Details:
  [private registry](private-registry.md#the-registry-login-finding).

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
- **OK** `worker` present, or not needed yet (the reason is given). **WARN** DjangoLux
  1.16+ and no `worker`: its tasks are queued and never run.
- **`--fix`:** adds `worker` (stack schema 5), last of all repairs. Details:
  [schema 4 to 5](stack-migrations.md#schema-4-to-5-worker).

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
| resident block | normalizes the generated resident block: nested commands, `DAC_OVERRIDE`, a digest-pinned read-only proxy, the registry login keys |

What each one changes: [retired wiring](stack-migrations.md#retired-wiring-check---fix-normalizes).
