# App data permissions (non-root DjangoLux images)

A DjangoLux image can run as uid `10001` with group `0` (`USER 10001:0`). Data a
root image wrote is `root:root` with directories at `0755`, so that user can read
it but cannot create, delete or rename anything; it reaches the data through group
`0` instead.

## The `app-permissions` finding

- The image runs as root: **OK** when the data is ready for uid 10001, **WARN**
  when it is not (fixable at any time, because root ignores the bits).
- The image runs as a non-root uid: **OK** when it can use its data, **FAIL** when
  it cannot, because DjangoLux's entrypoint then refuses to start (exit 78).

A plain `check` looks at the top two levels of every mount; `--deep` and `--fix`
walk everything. The mounts come from the merged compose config: every bind
directory and named volume a service on the app image mounts, so a project's own
mounts are included. Missing named volumes are left for Docker to create.

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
changed. The walk runs in a short-lived container from Composer's own image with
`--network none`, `no-new-privileges` and only `DAC_READ_SEARCH`, plus `FOWNER` and
`CHOWN` when it grants.

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
- for a non-root uid it scans and, when something is missing, grants access first;
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
