# Updates and channels

A deployment gets new versions in three independent ways: new application images,
new inline DjangoLux releases, and new Composer images. Each has its own channel
rules. The commands' syntax and exit codes are in the
[commands reference](commands.md).

| I want to… | run | section |
| --- | --- | --- |
| update Composer, then the application images and the resident pair | `./start.sh update` | [image updates](#image-updates) |
| only download images | `./start.sh pull` | [image updates](#image-updates) |
| update only Composer's deployer | `./start.sh update self` | [Composer itself](#composer-itself) |
| update only the resident pair | `./start.sh agent update` | [Composer itself](#composer-itself) |
| update DjangoLux inline, or roll it back | `./start.sh dlux update` / `dlux rollback` | [inline DjangoLux updates](#inline-djangolux-updates) |
| see what is available | `./start.sh agent check`, `./start.sh dlux check` | [image availability](#image-availability) |
| switch a channel | `./start.sh dlux channel beta`, `./start.sh check --beta` | [channels](#channels) |

## Image updates

`composer update` pulls the latest application image(s), recreates the affected
containers, waits for health checks, and runs [post-start tasks](deploying.md#post-start-tasks).
Naming services scopes both the pull and the recreate; the resident executor and
the watcher run this same pipeline for an update requested from DjangoLux. With
wrapper 5 or newer, a plain `update` with no named services first updates the
deployer ([`update self`](#composer-itself)), so the stack update that follows runs
on the new Composer; naming services skips that.

`composer pull` only downloads images. It never recreates containers, runs health
checks, or executes post-start tasks.

### Version gate

When deploying through `update` or `-u`, Composer can refuse to recreate onto an
image that is **older** than the version already deployed: the one thing a
pull-and-restart can't safely undo when forward-only migrations have already run.
It is opt-in and generic: set `COMPOSER_ACTIVE_VERSION_FILE` (a JSON file, e.g. a
runtime `active.json`) and, if needed, `COMPOSER_ACTIVE_VERSION_KEY` (default
`version`) and `COMPOSER_VERSION_LABEL` (the image label to compare, default
`org.opencontainers.image.version`). With no active-version source configured the
gate is disabled. Once configured it fails closed: when no target image carries a
readable version in that label, Composer cannot tell whether the update is a
downgrade, so it refuses ("no target image carries a readable '…' label"), names the
images it checked, and asks for a labelled image. `--force` is the deliberate
override for this refusal and for a downgrade block.

An older image can also pass when the running DjangoLux deployment confirms it will
keep its newer runtime-volume release. Composer runs `python manage.py
dlux_image_gate --baked-dlux-version VERSION` in `web`, or the service selected by
`COMPOSER_RUNTIME_GATE_SERVICE`. The command ships in DjangoLux 1.8.12+ and checks
the active release's `requires.baked_image` floor. Only a `keep` verdict permits this
exception; an abort, unavailable service, missing command or invalid response still
blocks. `--force` bypasses the check explicitly.

## Composer itself

Composer has two halves that update separately: the deployer the wrappers run, and
the resident `composer-agent` + `composer-executor` pair inside the stack.

- `composer update self` pulls the deployer used by `start.sh`
  (`debeski/composer:latest`, or `:beta` on the beta channel) and leaves the stack
  alone. `self update` remains a deprecated alias. Set `COMPOSER_SELF_IMAGE` only
  when using a compatible Composer image mirror.
- `composer agent update` pulls the pair's image once and recreates both, without
  touching the application ([the resident pair](resident-pair.md#lifecycle-commands)).
- Plain `./start.sh update` does both: the deployer first, then the stack and the
  pair.

`composer check` reports drift between the deployer, the wrappers and the pair
([`versions`](check.md#versions), [`composer-channel`](check.md#composer-channel),
[wrappers](check.md#wrapperstartsh-wrapperstartps1)).

## Inline DjangoLux updates

DjangoLux releases can be applied to a running stack without a new project image:
Composer installs the wheel from PyPI onto the runtime volume, and DjangoLux's
runtime supervisor runs the active release.

### What is offered: `dlux check`

`composer dlux check` publishes what is available to `state/package-available.json`,
the document DjangoLux reads instead of polling PyPI itself. The report carries the
offered release's own notes as `display` (`summary`, `highlights`, `release_url` from
the verified wheel's manifest), which DjangoLux 1.15.2b2+ shows in its review dialog.
The newest-release choice skips releases quarantined on this deployment (below) and
offers the next newest eligible one, or reports none available; the report lists
them as `quarantined`, and a check pinned to one with `--version` publishes it with
`inline_safe: false` and a reason that says how to retry it, so DjangoLux's Updates
card does not offer a release that already failed here.

### A lagging PyPI index

For several minutes after a release is published, PyPI's CDN can still serve the
project index (`/simple/django-lux/`, and `/pypi/django-lux/json`) without it, while
the release's own page (`/pypi/django-lux/<version>/json`) is already fresh: the CDN
caches each URL apart, and a query string does not bypass it. So Composer reads the
index and tops it up from release pages:

- a pinned `--version` the index lacks is read from its own release page; if that is
  not published either, Composer re-reads both after 3, 6 and 12 seconds before
  saying "not published";
- an unpinned check or update also reads the release pages of the likely next
  releases: the final of a newest beta, the next patch, minor and major, and on the
  beta channel the next beta of each. A missing page is a cheap 404; a probe that
  fails is ignored, never an error;
- an unpinned update that resolves below the release the last `dlux check` offered
  reads that release's page too, and re-reads after 3, 6 and 12 seconds before
  settling for what it found.

A release page gets the same trust as the index: an approved HTTPS host, a SHA-256
digest, and then the same attestation and digest checks on the download. A yanked
release is ignored. Finding a release this way does not choose it: the channel and
the quarantine still decide.

### Applying a release: `dlux update`

`composer dlux update`:

1. resolves the newest wheel on the deployment's DjangoLux channel (or the pinned
   `--version`, honoured on either channel) from PyPI. See
   [a lagging index](#a-lagging-pypi-index) for how it copes with PyPI's CDN right
   after a publish;
2. verifies its Trusted Publisher attestation and SHA-256, and honours the
   manifest's `inline_safe` ([release manifests](#release-manifests));
3. takes a backup ([below](#backups));
4. activates it under `state/active.json`;
5. brings up the services that load DjangoLux (by default `web`, `worker` and
   `celery`, each where the stack defines it; `--restart-service` or
   `COMPOSER_DLUX_RESTART_SERVICES` choose others; `dlux-updater` is deliberately
   excluded) with the same health wait `composer update` uses. The service whose
   `pre_start` applies migrations is **recreated** so Compose runs those hooks (a
   plain restart skips them, and a release carrying migrations would then activate
   with nobody applying them); every other service is restarted.

`--dry-run` resolves and verifies without activating.

**Quarantine.** When the services do not come back healthy, the update restores the
previous release and quarantines the new one: it moves to `failed/<version>/` on the
runtime volume with the reason in `quarantine-reason.txt`. Asking for a quarantined
release again is refused with nothing changed (before anything is fetched or backed
up), and the message names the release, the recorded reason and the retry command;
an unpinned update skips it. `--retry-quarantined` (it needs `--version`) applies it
anyway: a healthy retry renames its record to `failed/<version>.retried-<UTC time>/`,
which is kept and no longer counts as a quarantine, and a retry that fails again
sets the earlier record aside the same way before recording the new failure.

Exit `3` means the rollback was also unhealthy: a deployment that needs a human, and
one a caller must not retry.

### Rolling back: `dlux rollback`

`composer dlux rollback` restores the previous release: the newest staged release
below the active one that is newer than the DjangoLux baked into the project image,
else the image's own release. DjangoLux's runtime supervisor runs a volume release
only when it is newer than the baked one, so an older staged release would be
activated and never run. Composer reads the baked version from the image in a fresh
`web` container (`docker compose run --rm --no-deps`, `DLUX_BAKED_VERSION` else
`dlux.__version__`); when it cannot, it takes the newest staged release below the
active one. When the image's own release is already running, there is nothing older
to return to: it says so and exits `0` without a backup or a restart.

### Backups

Before an update or a rollback swaps anything (once the release is resolved and is
not already active) it takes a DjangoLux system backup inside the stack:

- `--backup data`, the default, is the database; `full` adds media; `skip` takes
  none; `COMPOSER_DLUX_BACKUP` sets the default;
- it execs `manage.py dlux_backup --trigger update` in `worker` (else `celery`, else
  `web`) under the runtime supervisor, so the active release takes it, and falls back
  to `manage.py shell` and the backup API on a DjangoLux without that command;
- a failed backup stops the update with nothing changed;
- when DjangoLux hands an update over (the executor runs it with
  `COMPOSER_OPERATION_ID` set), Composer takes no backup: DjangoLux 1.10.2b2+/1.11.0b5+
  backs up before it writes the request, and a second one would only double the wait.

### One operation at a time

An update, a rollback and a `--dry-run` each hold `state/package-operation.lock` for
their whole run: an exclusive `flock` on the runtime volume, so it covers the
deployer CLI, the sibling container it re-runs in and the resident executor alike.
One started while another runs is refused at once with "Another DjangoLux update or
rollback is already running; nothing was changed." (exit `1`) instead of waiting; the
lock belongs to the running process, so a run that dies leaves none behind.

### Where it runs

- **From the project root.** The runtime root (`/opt/dlux-runtime`,
  `DLUX_UPDATE_RUNTIME_ROOT`, or `--runtime-root`) is a container path, so Composer
  locates the project's runtime volume in the compose config and re-runs itself in a
  sibling container with that volume attached; it refuses when no service mounts
  that path or when Docker does not have the volume yet.
- **In the hardened topology,** the two halves split the work: `composer-agent` owns
  the package trigger and stages the verified wheel on the runtime volume (it has the
  egress), then hands `composer-executor` the version, filename and digest over the
  private socket, and the executor applies it offline (`--staged-wheel` /
  `--staged-sha256`, the same flags an air-gapped operator can use by hand). Why:
  [executor hardening](executor-hardening.md#inline-djangolux-packages-fetched-by-the-agent-swapped-by-the-executor).

## Release manifests

A DjangoLux wheel carries a release manifest, and Composer accepts schemas 1 and 2:

- schema 1 needs a boolean `inline_safe`;
- schema 2 enforces `install.inline` (`allowed`/`forbidden`), the migrations' effect
  and `rollback_compatible`: a release is inline-safe only when install is allowed,
  its migrations are `none`, `state_only` or `additive`, and it is rollback
  compatible;
- `requires` may name `baked_image`, `updater_schema`, `services` (only `composer`),
  `migration_baseline` and `stack_schema`. An unknown key refuses the release, so a
  new DjangoLux key needs Composer support first;
- `requires.services.composer` is a floor on Composer itself: update both the deployer
  (`update self`) and the resident pair (`agent update`) before applying a release
  that raises it;
- `requires.stack_schema` is the stack schema stamp in the project's `compose.yml`;
  below it the release is reported but not installed inline, and the reason says to
  run `check --fix` ([stack migrations](stack-migrations.md)).

## Image availability

The resident agent tells DjangoLux when a newer application image exists, without
pulling anything.

- **Registry checks.** `--check-image IMAGE` (repeatable) + `--availability-file PATH`
  poll the registry's tag digest against the locally pulled one and write
  `{ "available": …, "images": [ … ] }`. It only reports *readable* differences (an
  unreachable registry is "unknown", never a false positive), needs no registry
  access from the consumer, and re-checks right after an applied update. A private
  repository is read with the [registry login](private-registry.md) or a static
  `COMPOSER_REGISTRY_TOKEN`; an unknown entry carries the `reason` it could not be
  read.
- **Interval.** Every 15 minutes by default (`--check-interval`, 900 s, which the
  generated block passes); DjangoLux 1.9.0+ overrides it with the interval chosen in
  Options, and asks for an immediate check through `state/check-request.json`
  ([protocol](agent-protocol-v1.md#check-interval-and-check-requests)).
- **Updates applied elsewhere.** The document self-corrects after an update applied
  by **someone else** (`composer update` run from the project root, the executor, a
  manual `docker compose pull`). Between scheduled checks the watched images' local
  digests are polled every 30 s, and a moved digest re-publishes immediately, so an
  already-installed update stops being advertised within seconds instead of
  lingering until the next check. An unreadable local digest is "unknown" and never
  triggers a re-publish.
- **What the update brings.** For an available image, Composer also reads remote
  image labels once and publishes two independent, optional fields. `version` comes
  from `COMPOSER_VERSION_LABEL` (default `org.opencontainers.image.version`).
  `manifest` comes from `COMPOSER_RELEASE_MANIFEST_LABEL` (default
  `org.dlux.project.release-manifest`) when that label contains a schema-1 JSON
  object with any of `version`, `summary`, up to eight `highlights`, or an HTTPS
  `release_url`. Raw JSON remains supported, but CI/build pipelines should use
  `base64:<URL-safe-base64-JSON>` so quoting cannot corrupt the label while it crosses
  YAML, action inputs, and Docker build arguments. Missing or malformed metadata is
  omitted without affecting digest detection or deployment.

### Refreshing it now: `agent check`

`composer agent check` checks image availability without pulling, recreating, or
enabling maintenance. From a deployment root it reads the resident agent's watched
images and availability path from the resolved Compose configuration, checks every
configured `--check-image` entry together, and atomically publishes the result
through that running service (`compose exec -T` and a standard-library atomic writer
inside it), so the CLI does not need the runtime volume mounted and the resident
service does not need a newer Composer. The DLUX UI shows the new result on its next
refresh, instead of waiting for the next scheduled check; no service restart is
needed. It sends no control-plane command.

`--no-publish` opts out. Explicit image arguments are diagnostic and do not replace
the deployment's document; `--availability-file PATH` instead writes to that path in
the CLI's filesystem. Deployments without a configured image publisher keep
diagnostic behavior, and outside a configured deployment, image discovery falls back
to `COMPOSER_CHECK_IMAGE`, `WEB_IMAGE`, then the Compose agent block. A stopped
service or unwritable runtime file returns exit `1` and a stderr diagnostic; `--json`
still prints the checked document when publication fails. Exit codes:
[commands](commands.md#agent-check).

## Channels

Composer and DjangoLux each have a release channel, stable or beta, set
independently: running a Composer beta to test Composer is not a reason to start
feeding a production deployment beta framework releases.

### DjangoLux's channel

`composer dlux channel [stable|beta]` reports or requests the **DjangoLux** release
channel for this deployment. Stable is the default and admits no prerelease; beta is
an explicit opt-in that widens eligibility only: every digest, attestation, manifest
and migration check is identical on both.

Composer *reads* the channel from `state/channel-policy.json` and never writes it:
the DjangoLux worker owns that file because it owns the database row it mirrors, and
two writers of one policy is how a deployment ends up disagreeing with itself. So
`composer dlux channel beta` leaves a token-carrying request beside the policy and
reports "requested, not yet applied" until that worker acknowledges it. A missing,
unreadable, malformed or future-schema policy reads as **stable**: a deployment that
never opted in is never handed a beta because a file was missing.

### Composer's channel

`composer check --beta` / `--stable` switch **Composer's own** channel. One operation
moves both halves together: the persisted `.composer-channel` file that the wrappers
read before any Composer code runs, and the image the resident
`composer-agent`/`composer-executor` pair run inside the stack, because a deployer on
`:beta` talking to an agent on `:latest` is a configuration nobody chose. The retag is
scoped to `debeski/composer:*` images inside the generated block, so the third-party
socket proxy and anything outside the markers are untouched.

Both flags are confirmation-gated (`-y` honoured), mutually exclusive, and dry-run
first: the channel file is written only after the Compose edit validates. Plain
`composer check` reports drift between the two halves without changing anything, and
`check --fix` preserves the channel. An explicit `COMPOSER_SELF_IMAGE` pin always wins
over the channel: an operator who pinned an image asked for that image.

How Composer's own releases reach each channel: [releasing](RELEASING.md#channels-stable-and-beta).
