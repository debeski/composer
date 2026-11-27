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

## What the application may request

DjangoLux can ask the resident pair for three kinds of update: an application
image update, an inline DjangoLux update, and a resident Composer update.
`DLUX_UPDATE_POLICY` in the secrets file decides which of those requests the pair
performs (at a terminal, `check --fix` asks for it when the file does not set it:
[questions](check.md#questions-at-a-terminal)):

| Request from DjangoLux | `all` (default) | `inline` | `none` |
| --- | --- | --- | --- |
| inline DjangoLux update (package trigger, `dlux_package_apply`) | performed | performed | refused |
| application image update (image trigger) | performed | refused | refused |
| resident Composer update (`agent-update`) | performed | refused | refused |
| control-link recovery deploy (`recovery_deploy`, a full `composer update`) | performed | refused | refused |
| inline rollback, `check`, `agent-check`, availability checks | performed | performed | performed |

An empty value means `all`; any other value counts as `none`. A refused request is
answered like a failed one, with exit code 2 and a message naming the policy, so
DjangoLux leaves maintenance and shows why. The pair reads the policy from its own
Compose `environment`, which `web` cannot change, so it refuses even a request a
compromised `web` wrote straight onto the runtime volume. The agent reports the
policy it enforces as `update_policy` in `agent-status.json`, and DjangoLux warns
when that differs from its own.

Every command in the table above still runs from this shell under every policy:
the policy covers what the application requests, not what the operator deploys.
The pair reads the value when it is created, so after changing it run `./start.sh`
to recreate the pair. `composer check` reports the policy and whether every
reader receives it ([`update-policy`](check.md#update-policy)).

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

Neither `update` nor `pull` touches a resident pair whose block names a moving tag
such as `debeski/composer:beta`: the pair moves only through
[the Composer pin](#the-composer-pin). The run leaves `composer-agent` and
`composer-executor` out and says so. An update that names only them is refused
(exit 2) and points to `agent update`. Once the block names a pinned digest, the
update pulls that digest and recreates the pair when the block changed.

### Version gate

When deploying through `update` or `-u`, Composer can refuse to recreate onto an
image that is **older** than the version already deployed: the one thing a
pull-and-restart can't safely undo when forward-only migrations have already run.
It is opt-in and generic: set `COMPOSER_ACTIVE_VERSION_FILE` (a JSON file, e.g. a
runtime `active.json`) and, if needed, `COMPOSER_ACTIVE_VERSION_KEY` (default
`version`) and `COMPOSER_VERSION_LABEL` (the image label to compare, default
`org.opencontainers.image.version`). With no active-version source configured the
gate is disabled. Once configured it fails closed:

- when no target image carries a readable version in that label, Composer cannot
  tell whether the update is a downgrade, so it refuses ("no target image carries a
  readable '…' label"), names the images it checked, and asks for a labelled image;
- when the active-version file is missing, the active version is the same label on
  the image the app container (`web`, or `COMPOSER_RUNTIME_GATE_SERVICE`) runs now.
  DjangoLux reads a missing `active.json` as "running the release baked into the
  image", and the gate runs before the recreate, so the container still names the
  old image. A stack with no app container at all has nothing to downgrade and
  passes;
- when the file is present but unreadable, or the running image has no readable
  label, it refuses.

`--force` is the deliberate override for each refusal and for a downgrade block.

An older image can also pass when the running DjangoLux deployment confirms it will
keep its newer runtime-volume release. Composer runs `python manage.py
dlux_image_gate --baked-dlux-version VERSION` in `web`, or the service selected by
`COMPOSER_RUNTIME_GATE_SERVICE`. The command ships in DjangoLux 1.8.12+ and checks
the active release's `requires.baked_image` floor. Only a `keep` verdict permits this
exception; an abort, unavailable service, missing command or invalid response still
blocks. `--force` bypasses the check explicitly.

Independently of this gate, every deploy runs the
[downgrade guard](deploying.md#downgrade-guard), which needs no configuration and
compares the app image's own version labels with what the deployment last ran.

## Composer itself

Composer has two halves that update separately: the deployer the wrappers run, and
the resident `composer-agent` + `composer-executor` pair inside the stack.

- `composer update self` pins the deployer used by `start.sh` to the channel's
  newest signed release ([the Composer pin](#the-composer-pin)), pulls it, and
  leaves the stack alone. `self update` remains a deprecated alias. Set
  `COMPOSER_SELF_IMAGE` only when using a compatible Composer image mirror.
- `composer agent update` pins the same release, pulls the pair's image once and
  recreates both, without touching the application
  ([the resident pair](resident-pair.md#lifecycle-commands)).
- Plain `./start.sh update` does both: the deployer first, then the stack, and the
  pair when its block names a pin ([image updates](#image-updates)).

A stack pinned at 1.11.0b1 moves on with `./start.sh agent update`, not
`./start.sh update self`. Wrapper 7 resolves the pin with the deployer the lock
names, and 1.11.0b1's own launcher refuses the `update self --pin` that wrapper 7
passes it, so its `update self` fails on every run. `agent update` pins the
channel's newer release into `.composer-image` and the block and recreates the
pair; from then on the wrapper runs that release, and `update self` works.

`composer check` reports drift between the deployer, the wrappers and the pair
([`versions`](check.md#versions), [`composer-channel`](check.md#composer-channel),
[wrappers](check.md#wrapperstartsh-wrapperstartps1)).

### The Composer pin

Both halves run `debeski/composer:vX.Y.Z@sha256:…`, never a moving tag, once a
deployment has been updated. The digest comes from the release itself, not from the
registry, so whoever can push to Docker Hub cannot change what a deployment runs.

1. Composer reads the version label of the channel tag (`:latest`, or the newer of
   `:beta` and `:latest` on the beta channel). That label only chooses which release
   to look up. The stable channel refuses a prerelease.
2. It downloads `composer-image.json` and `composer-image.json.sigstore.json` from
   that version's GitHub Release
   (`https://github.com/debeski/composer/releases/download/vX.Y.Z/…`). Redirects are
   followed only to GitHub's asset hosts.
3. It verifies the Sigstore bundle against the identity
   `https://github.com/debeski/composer/.github/workflows/release.yml@refs/tags/vX.Y.Z`
   (issuer `https://token.actions.githubusercontent.com`). Verification runs online
   first, so Sigstore key rotations arrive, then offline against the trust root
   shipped with Composer, so a Sigstore outage alone does not block an update. The
   statement must name that exact version, tag, `debeski/composer` and a sha256
   digest.
4. It writes the result to `.composer-image` beside `.composer-channel`, and
   rewrites the generated block's `debeski/composer` lines to the same reference.
   The Compose edit is validated first and the original is kept under
   `.xclude/dlux-agent-bootstrap/<UTC>/`.
5. The wrapper then pulls that digest. A pull by digest is checked against the
   content, so the registry cannot substitute another image.

**Only when there is something to update.** The channel's version is compared with
the pin, or, before the first pin, with the Composer doing the update:

| The channel advertises | What happens |
| --- | --- |
| a newer release | It is verified and pinned. If it cannot be verified, the update is refused and the current pin keeps running. |
| the pinned release | Nothing changes. |
| the running release, with no pin yet | It is pinned if it verifies. If GitHub or Sigstore cannot be reached, or the statement is not published yet, nothing changes and the run says why. A statement that fails verification is refused. |
| an older release | Nothing changes: a channel tag never moves a pin backwards. The run says the channel is older. |
| nothing readable (registry unreachable) | Nothing changes, and the run says so. |

None of the "nothing changes" outcomes is an error. `agent update` and `update self`
then pull and recreate nothing, say so and exit 0: before the first pin the block
and the deployer name the channel tag, and following it could move them backwards.
The wrapper keeps the deployer it has. `agent update` still pulls and recreates the
pair when the block did not name the existing pin yet. That is also why a release
candidate's own smoke test, which runs a Composer newer than any published release,
needs no statement and no network beyond the channel's version label.

A registry can point a tag at a genuine old release; following it would be a
downgrade. Pass `--allow-downgrade` to `update self` or `agent update` to move back on
purpose. A channel switch (`check --stable` / `--beta`) may move back, because the
operator chose it. Both must verify the release they move to, and fail on an
unreadable channel.

**Fail closed.** An update that cannot be verified changes nothing, and the current
pin keeps running. When GitHub or Sigstore cannot be reached (an air-gapped host, an
outage), `--allow-unverified-composer` on `update self`, `agent update` or
`check --beta|--stable` pins the registry's digest for that release's own
`:vX.Y.Z` tag instead. The wrapper forwards `COMPOSER_ALLOW_UNVERIFIED_COMPOSER=1`
as the same switch. The output says `UNVERIFIED`. No flag accepts a signature,
identity or statement that fails to match. A mirror is pinned with an explicit
`COMPOSER_SELF_IMAGE=…@sha256:…`, which is never resolved over.

**Releases before signing.** Releases before 1.11.0b1 have no statement, no
`org.dlux.composer.image-pin` label, and cannot run a pinned stack: they cannot read
a digest-pinned block, and their `update self` never moves the pin, so a lock naming
one keeps the wrapper on it for good. Composer never pins one, verified or not: a
switch or an `--allow-downgrade` to one (for example `check --stable` while the
stable channel's newest is 1.10.4) is refused before anything changes, with or
without `--allow-unverified-composer`. Stay on the beta channel until the stable
channel publishes 1.11.0 or later, then run `check --stable`. Updating away from
such a release needs nothing from it.

A stack that 1.11.0b1 did pin to one (`check --stable --allow-unverified-composer`
while stable was 1.10.4) runs that release as its deployer, which cannot get it out.
Run the switch with a deployer that can, once:

```bash
docker pull debeski/composer:beta
COMPOSER_SELF_IMAGE=debeski/composer:beta ./start.sh check --beta -y
```

The wrapper runs the image `COMPOSER_SELF_IMAGE` names instead of the lock (and does
not pull one it already has, hence the pull), trusting the beta tag once as a first
pin does. That deployer verifies the beta channel's
signed release, writes it to `.composer-image` and `.composer-channel`, rewrites the
block and recreates the pair. Plain `./start.sh` commands then run the new pin.
`check` reports the trapped state under [`composer-channel`](check.md#composer-channel)
when a newer Composer reads it (the Operations panel, or `COMPOSER_SELF_IMAGE`).

**First pin.** A deployment with no `.composer-image` runs its channel tag. Each
`update self`, `./start.sh update`, `agent update` or channel switch trusts that tag
to start the deployer that does the verifying, until one of them pins: the channel's
release verified, either newer than that deployer or the same release. A deployer
image without the `org.dlux.composer.image-pin` label cannot pin; the wrapper then
keeps the tag and says so. `check` reports the unpinned state as OK, and
`check --fix` only pins the block to a lock that already exists. It never goes to
the network.

That pull of the channel tag also moves the local tag the pair's block names, and a
`compose.yml` restored from git or a backup, or an edited pin line, can name an
older pinned release. When the block names an older Composer than the running pair
(the image's own version, or the pinned tag's when the image is not local yet), an
image whose release cannot be read, or a pinned image whose version is not the one
its tag names, `./start.sh` and every other `up` leave `composer-agent` and
`composer-executor` as they are, warn and exit 0: move the pair with `agent
update`, which verifies, or switch on purpose with `check --beta` / `check
--stable`. A newer or equal release, a stopped pair and an explicit
`COMPOSER_SELF_IMAGE` are started as the block says. A pair left on an older
Composer than the deployer is named in the deploy's warnings, with `agent update` as
the way on.

`.composer-image` is read the same way by both wrappers and by Composer: the first
non-empty line, stripped at its ends (and of a byte-order mark), must be a pinned
reference whole. Nothing inside it is removed, so a malformed lock reads as no lock
everywhere.

Wrapper 7 is the first that reads `.composer-image`. An older wrapper keeps pulling
the channel tag for the deployer, and `check --fix` replaces it. When the deployer
the lock names cannot resolve the pin (a release from before that route prints only
its usage line), the wrapper points to `./start.sh agent update`.

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
3. unpacks only the `dlux` package and its `django_lux-*.dist-info` into the
   runtime volume's `staging/`. Any other top-level entry in the wheel (a
   `sitecustomize.py`, a `.pth` file, another package) refuses the release, so
   nothing but DjangoLux reaches the app's interpreter path. So does an archive
   that unpacks to more than 256 MB, or one the volume has no room for with 16 MB
   to spare (the message names the free space). A copy that is not staged, after
   a refusal or a failure, is removed;
4. refuses a release whose `requires.baked_image` floor the project image is
   below, read the way DjangoLux's supervisor reads it, before the backup: the
   supervisor would keep serving the baked DjangoLux, so activating it would only
   report a success that is not running. Composer reads the baked version as the
   rollback does ([below](#rolling-back-dlux-rollback)); when it cannot, the floor
   is not checked;
5. takes a backup ([below](#backups));
6. activates it under `state/active.json`;
7. brings up the services that load DjangoLux (by default `web`, `worker` and
   `celery`, each where the stack defines it; `--restart-service` or
   `COMPOSER_DLUX_RESTART_SERVICES` choose others; `dlux-updater` is deliberately
   excluded) with the same health wait `composer update` uses. The service whose
   `pre_start` applies migrations is **recreated** so Compose runs those hooks (a
   plain restart skips them, and a release carrying migrations would then activate
   with nobody applying them). `web` is restarted. Any other service whose command
   runs `python -m dlux.updater.supervisor` without `--no-watch` (a project
   `celery`) is not: its supervisor restarts it when the activation advances
   `state/generation`, and a second restart from Composer stopped it mid-task.

`--dry-run` resolves and verifies without activating. Asking for the release that
is already active reports it and changes nothing; with `--version` nothing is
fetched or unpacked.

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
else the image's own release. `--version X` names the target instead: a staged
release below the active one, or the baked version for the image's own release.
Any other version is refused with nothing changed, and the message lists the
targets there are. DjangoLux's runtime supervisor runs a volume release
only when it is newer than the baked one, so an older staged release would be
activated and never run. Composer reads the baked version from the image in a fresh
`web` container (`docker compose run --rm --no-deps`, `DLUX_BAKED_VERSION` else
`dlux.__version__`); when it cannot, it takes the newest staged release below the
active one. When the image's own release is already running, there is nothing older
to return to: it says so and exits `0` without a backup or a restart.

A rollback runs older DjangoLux code on the newer database schema. It is refused
when the release being left, or a staged release between it and the target,
declares `migrations.rollback_compatible: false`: restore a pre-update backup, then
roll back with `--force`. Otherwise it prints the same warning as the Updates
card: a rollback changes code and static files only and never reverses
migrations, so the pre-update backup no longer matches the database; restoring it
later means updating back to its version first, or DjangoLux's **Ignore database
version mismatch**.

**An unusable pointer.** When `state/active.json` exists but cannot be used (not a
JSON object, unreadable, a symbolic link or FIFO, or naming a release that is not
staged), `dlux check` and `dlux channel` say so and exit `1`, and `dlux update`
refuses; each names `dlux rollback` as the way out. `dlux rollback` then renames the
file to `state/active.json.unusable-<UTC time>` (kept, never deleted), advances
`state/generation` and restarts on the image's own release. DjangoLux rebuilds the
pointer from the release it recorded on its next state tick.

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
- `requires.stack_schema` is the stack schema stamp in the project's `compose.yml`
  (its lowest, when its stamps disagree); below it the release is reported but not
  installed inline. `dlux update`, `dlux update --dry-run`, `dlux check` and the
  Updates card's reason then name the way forward, worked out from the release data:
  `check --fix` first when it can reach the schema on the running DjangoLux,
  otherwise the newest release this stack still takes inline (`dlux update --version
  X`), and a project image for what no inline release reaches
  ([the way out of an old stack](stack-migrations.md#the-way-out-of-an-old-stack)).

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
read before any Composer code runs, the `.composer-image` pin, and the image the
resident `composer-agent`/`composer-executor` pair run inside the stack, because a
deployer on `:beta` talking to an agent on `:latest` is a configuration nobody chose.
The new channel's release is resolved and verified first
([the Composer pin](#the-composer-pin)); a switch that cannot be verified changes
nothing. The retag is scoped to `debeski/composer` images inside the generated block, so
anything else in the block and anything outside the markers is untouched.

Both flags are confirmation-gated (`-y` honoured), mutually exclusive, and dry-run
first: the pin and the channel file are written only after the Compose edit
validates. A switch that changed the block then recreates `composer-agent` and
`composer-executor` on the new pin and waits for them, as `check --fix` does
([lifecycle](check.md#after-the-repairs)); `--no-recreate` leaves them, recorded as
[`pending-recreate`](check.md#pending-recreate) until `./start.sh` recreates them. Plain `composer check` reports drift between the halves without changing
anything, and `check --fix` preserves the channel and pins the block to the existing
lock. An explicit `COMPOSER_SELF_IMAGE` pin always wins over both: an operator who
pinned an image asked for that image.

How Composer's own releases reach each channel: [releasing](RELEASING.md#channels-stable-and-beta).
