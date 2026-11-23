# Stack migrations

A DjangoLux stack is generated once and then lives on a deployment host for years.
Composer brings an older stack forward: `composer check` reports what is behind and
`composer check --fix` migrates it, guarded the same way every time (see
[how `--fix` is guarded](check.md#how---fix-is-guarded)). This page covers the
stack schema steps, the resident topology migrations, and the retired wiring
`check --fix` normalizes.

## Stamps

DjangoLux stamps each generated stack file with the stack contract schema it was
written for: `DLUX_STACK_SCHEMA: "N"` in compose.yml's `x-environment`,
`LABEL org.dlux.stack-schema="N"` in the Dockerfile, and a `dlux stack schema N`
header in `compose.dev.yml`, `entrypoint.sh`, `gunicorn.py`, the secrets file in use
and `.proxy/*`. The `stack-schema` finding of `composer check` reads them
(`composer/stack_schema.py` uses the same rule as DjangoLux's
`dlux.contracts.stack.read_stamp`):

| Files | Finding |
| --- | --- |
| none stamped | `ok`: the stack predates stamping |
| compose.yml stamped, every other stamp agrees | `ok`, with the count of unstamped files |
| stamps disagree, or other files stamped but compose.yml is not | `warn`, naming the files per schema |

Composer compares the files with each other. Whether compose.yml's schema is the
one the running DjangoLux expects is the app's `stack.schema` doctor check, which
`check --deep` relays. `start.sh`/`start.ps1` are not stamped; they carry
Composer's own [wrapper marker](deploying.md#wrappers).

Each schema migration of `check --fix` restamps every file stamped with the schema
it leaves, including the secrets file `check` reads (the first of `.env`,
`secrets/.env`, `.secrets/.env`). In the secrets file only the stamp changes: it is
rewritten in place byte for byte, keeping its mode, owner and line endings; it is
not copied to the `.xclude/composer-check/` backup; and the repair preview lists it
without showing any of its lines. An unstamped secrets file is left as it is.

## Schema steps

| Step | What changes | Details |
| --- | --- | --- |
| 2 → 3 | the maintenance page moves from a bind-mounted `.proxy/maintenance.html` to the page Composer publishes | [maintenance page](maintenance-page.md#migrating-a-stack-from-before-schema-3) |
| 3 → 4 | the proxy stops serving private `/media/` files without DjangoLux's check | [stored files](proxy.md#stored-files) |
| 4 → 5 | DjangoLux's tasks move from `celery` to a `worker` service | below |

### Schema 4 to 5: `worker`

DjangoLux 1.16 runs its own tasks with `python manage.py dlux_worker` in a `worker`
service, which takes over everything `celery` did for DjangoLux. The `task-worker`
finding is a WARN when the running DjangoLux is 1.16 or later and the stack has no
`worker` (its tasks are queued and never run), and OK otherwise.

`composer check --fix` adds the service once the stack's DjangoLux has
`dlux_worker`: the running release (the active inline release, or the image's), or
with `-d` the `django-lux` version `requirements.txt`/`pyproject.toml` pins, since a
development stack builds from the source. On an older DjangoLux it changes nothing,
because `worker` could not start. The fix:

- copies `celery` to `worker` with its mounts, environment anchors, networks and
  dependencies, and gives it the command `python -m dlux.updater.supervisor --
  python manage.py dlux_worker`, the healthcheck `python manage.py dlux_worker
  --check` and `stop_grace_period: 30s`;
- moves the init containers (`dlux_reconcile`, then `migrator`) and
  `DLUX_BOOT_GATE: "apply"` from `celery` to `worker`, which mounts `dlux_runtime`
  and `static` read-write;
- keeps `celery` for the project's own tasks: no `pre_start`, so its boot gate waits
  like `web`; its command, healthcheck and read-write runtime mount stay (project
  tasks may write relay requests);
- adds `worker` to `COMPOSER_AGENT_RESTART_SERVICES` right after `web`, and with
  `-d` mirrors the `compose.dev.yml` `celery` override for `worker`;
- restamps files stamped 4 to 5, the secrets file among them, also on a stack whose
  `compose.yml` is already stamped 5. An unstamped `compose.yml` gets
  `DLUX_STACK_SCHEMA: "5"` only when nothing older is left: no `tools/` runtime
  modules, the maintenance page published by Composer, and stored files private.
  Otherwise it stays unstamped and the result says why; a later `check --fix` stamps
  it once the rest is done.

The merged configuration is validated with `docker compose config` before anything
is written, the originals are archived under `.xclude/composer-check/`, and nothing
is recreated: the next deploy (`./start.sh`, or `./start.sh -d -b`) starts `worker`.
The step runs after the others, so a stack still on the old layout (`dlux-updater`,
`tools.dlux_runtime_supervisor`, the project's maintenance page) reaches schema 5 in
one `check --fix`.

DjangoLux 1.16 cannot be installed inline onto an older stack, and the step waits
for a project image on 1.16, so the order is:

1. put the image on DjangoLux 1.16 (`./start.sh pull`, or bump the `django-lux` pin
   and rebuild);
2. run `./start.sh check --fix`;
3. deploy.

A development stack runs `./start.sh -d check --fix` after bumping the pin, then
`./start.sh -d -b`.

## Topology migrations

New deployments run the hardened resident pair: `composer-agent` (`agent run`) plus
`composer-executor` (`executor run`), with a read-only `docker-socket-proxy` (see
[executor hardening](executor-hardening.md)). `composer check` reports any other
topology, and `check --fix` runs whichever migration applies:

| Topology `check` finds | `check --fix` |
| --- | --- |
| legacy `composer-updater` | replaces it with the hardened trio |
| agent-only (`composer-agent` with a write-enabled proxy) | hardens it: adds `composer-executor`, demotes the proxy to read-only |
| no Composer service at all | installs `docker-socket-proxy`, `composer-executor` and `composer-agent`, and prints the command that starts them |
| `composer-agent` and `composer-updater` together | fails closed |

Legacy `composer-updater` stacks still deploy until then.

### By hand: `agent enable` and `executor enable`

Composer itself migrates an existing generated stack. Its wrapper may predate
`update self` (wrapper 5), so refresh the deployer with `self update`, which every
wrapper still accepts:

```bash
./start.sh self update
./start.sh agent enable           # dry run: prints the exact Compose diff
./start.sh agent enable --apply
```

`agent enable` writes the hardened trio directly, from a legacy `composer-updater`
block or from an agent-only block. `executor enable` (`--apply` to write) hardens an
agent-only stack the same way; on a stack that is already hardened both change
nothing. A locally installed binary may instead run `composer agent enable
--project-dir /path/to/project` (or `executor enable --project-dir …`). Composer
owns the only Compose transformer; DjangoLux's `agent enable` command only forwards
to it.

Both commands use the same guardrails:

- the default is a dry run that prints the exact Compose diff;
- apply accepts only a recognized DLUX-generated block, validates the candidate with
  `docker compose config` before writing, preserves the original under
  `.xclude/dlux-agent-bootstrap/<timestamp>/`, and replaces it atomically; re-runs
  are idempotent;
- the replacement keeps the deployment's own topology: the networks attached to the
  outgoing `docker-socket-proxy`/`composer-updater` services, that block's
  `COMPOSER_VERSION_LABEL`, and its `WEB_IMAGE` reference are carried forward
  verbatim, so projects generated before the DjangoLux 1.5 scaffold (`egress` /
  `docker_proxy`) migrate without renaming networks or losing the baked-version
  gate. A network the project never declares is reported by name before any write;
- only the Compose file is read and rewritten, so this runs in a deployment
  directory that holds nothing but `compose.yml`, `.proxy/`, and `.secrets/`. When a
  `requirements.txt`/`pyproject.toml` is present it must declare DjangoLux 1.5.0+ or
  apply refuses without `--allow-unverified-dlux`; with no manifest to read the
  check is reported as an advisory warning instead.

## Retired wiring `check --fix` normalizes

Besides the schema steps and topologies, `check --fix`:

- normalizes generated resident commands to `agent run`/`executor run`;
- rewrites retired local DLUX tools commands (`tools.dlux_runtime_supervisor`,
  `tools.smtp_relay`) to package-owned modules and removes generated local `tools/`
  bind mounts. Runtime-command rewrites are gated on the project image shipping the
  needed DLUX module (`1.6.2+` for `dlux.updater.supervisor`, `1.7.0+` for
  `dlux.smtp_relay`);
- adds missing `org.dlux.restart` labels;
- strips updater-era `web.post_start` hooks when `dlux-updater` is retired into
  init containers (`pre_start` on `worker`, or on `celery` before stack schema 5);
- installs or normalizes the `org.dlux.post-start` migrator label only for stacks
  that still need that compatibility path (see
  [post-start tasks](deploying.md#post-start-tasks));
- with `-d`, validates and normalizes `compose.dev.yml` against the merged base/dev
  config: it removes dev `dlux-updater` overrides, keeps the runtime mount of
  `worker` and `celery` writable, and disables inline updates in dev containers.

Proxy, permission, relay and registry repairs are listed per finding in the
[`check` reference](check.md).
