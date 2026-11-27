# App data permissions (non-root DjangoLux images)

A DjangoLux image can run as uid `10001` with group `0` (`USER 10001:0`). Data a
root image wrote is `root:root` with directories at `0755`, so that user can read
it but cannot create, delete or rename anything; it reaches the data through group
`0` instead.

## The `app-permissions` finding

- The image runs as root: **OK** when the data is ready for uid 10001, **WARN**
  when it is not (fixable at any time, because root ignores the bits).
- The image is not on this host: its user cannot be read, so the finding says so
  and judges the data for uid 10001, as for a root image.
- The image runs as a non-root uid: **OK** when it can use its data, **FAIL** when
  it cannot, because DjangoLux's entrypoint then refuses to start (exit 78).

A plain `check` looks at the top two levels of every mount; `--deep` and `--fix`
walk everything. The mounts come from the merged compose config: every bind
directory and named volume a service on the app image mounts, so a project's own
mounts are included. A named volume that does not exist yet is not checked; before
a start Composer has Compose create it first ([before every start](#before-every-start-of-the-app)).

The app image is the one `web` (or else `worker`) runs, and the app services are
every service running that image. In development mode (`-d`) the dev compose file
resets `image:` and builds instead. There a built service's image is the name
Compose gives what it builds, `<project>-<service>` (`eservices-web`), and the app
services are every service built from the same `build:` section as `web`, so
`worker` and `celery` count too.

## What `check --fix` grants

`check --fix` grants what is missing:

- group read, write and search bits on directories, group write on log files, and
  group read where a file is not readable;
- on the runtime volume only the top-level directories and the `state/` tree get
  group write: the app replaces a file there by renaming a new one over it, so the
  files themselves need only group read;
- the group changes only on an entry owned by neither group 0 nor the app.

Nothing is moved, renamed or rewritten, links are never followed, the walk stays on
each mount's filesystem, and a file with more than one hard link is reported, never
changed. The one file Composer adds is `.composer-keep` (`0664`, a line that says
why) in a writable named volume that is still empty once the app's containers exist
([empty volumes](#empty-volumes)). The walk runs in a short-lived container from
Composer's own image with `--network none`, `no-new-privileges` and only
`DAC_READ_SEARCH`, plus `FOWNER` and `CHOWN` when it grants. It carries the label
`io.dlux.composer.app-permissions=<project>`; a walk container left `Created` or
`Exited` by a deployer killed mid-walk is removed at the next walk.

An entry the grant cannot change (a host folder on Docker Desktop, a read-only
filesystem) is named with its owner, mode, what it needs and the OS error, with the
command to run on the host: `sudo chmod g+rwX <path>` for a bind-mounted folder
(`sudo chgrp 0 <path>` first when its group is neither 0 nor the app's). On a named
volume it names the volume driver, since the walk already runs as root there.

After a grant that changed something, app containers that stopped on DjangoLux's
refusal are restarted: a container looping in Docker's restart backoff keeps its
definition, so neither `up` nor `check --fix` would recreate it, and the health check
would read the old loop. `check --fix` also starts the ones that exited 78.
`docker restart` creates no container, so Docker does not refill a volume.

## Empty volumes

Docker fills a named volume from the image's mount point whenever it creates a
container that mounts the volume while the volume is empty, and gives the volume's
root that mount point's owner and mode again. On an image whose mount point is root
`0755` (a plain `USER 10001:0` image, or an operator mount point such as
`/app/exports` that the Dockerfile does not prepare) that takes back the grant at
every create: `up` after an image update, a force-recreate, a `compose run`. A volume
that holds one entry is never filled again, so a grant adds `.composer-keep` to each
writable named volume still empty after Docker filled it, and the grant stays. Leave
the file in place; `app-permissions revert` leaves it too.

## Undoing a grant

Every change is listed in
`.xclude/composer-check/<UTC>/app-permissions/app-permissions.tsv.gz`:

```bash
./start.sh app-permissions revert .xclude/composer-check/<UTC>/app-permissions/app-permissions.tsv.gz
```

restores the previous mode and group of each entry that still has the granted ones.

## Before every start of the app

Before every `up` (`./start.sh`, `update`, and the executor's image update) and
every recreate of an app service (`dlux update`, the Operations card's recreate),
Composer reads the app image's `Config.User`:

- an app image that is not on the host yet is pulled first, through the same
  compose environment as `up` (and built first for a service with a `build`
  section, as `up --build` would), so the uid comes from the image about to run.
  This holds in development mode too: the stack's own build is built and read, and
  a `USER 10001:0` dev image gets its data granted before `up`, as in production;
- for a non-root uid, when a named volume an app service mounts does not exist yet,
  or an app service has no container yet (a fresh install, also after a `check` or
  `check --fix` that left volumes behind), Compose creates the missing app
  containers without starting them (`up --no-start --no-deps --no-recreate` for
  those services; an existing container is left as it is). Docker fills each empty
  volume from the image and gives its root the owner and mode of the image's mount
  point, which for a plain `USER 10001:0` image is root `0755`; the volumes carry
  Compose's own labels. The grant then reaches them, and `up` starts the same
  containers;
- for a non-root uid it scans and, when something is missing or a writable named
  volume is still empty, grants access first ([empty volumes](#empty-volumes)), and
  restarts app containers looping on the refusal;
- when the image cannot be read or the grant fails, it reports `failed` and
  recreates nothing, so the running stack keeps serving;
- a user name instead of a numeric `uid:gid` is refused, because Composer cannot
  tell which uid needs access.

## Composer's own writes

Composer's own writes on the runtime volume are `0664` files in `0775` directories
(umask `0002` in the agent, the executor, the watcher, every `composer dlux` command
and the resident-update helper; `agent check` publishes `0664`), and the agent opens
the directories it owns there to group 0 when it starts. Why group write is the
contract there: [executor hardening](executor-hardening.md#bridge-files-and-queues).
