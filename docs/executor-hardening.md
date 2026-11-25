# Executor hardening

The resident pair splits Docker authority. The network-facing `composer-agent`
holds none and has no Docker API at all: it asks for every write and for its few
reads. The `composer-executor` holds the Docker socket, is not network-facing, and
performs only a fixed set of typed operations, re-validating each one.

**Threat model.** The agent holds an outbound connection to a control plane and to
package and image registries, and processes requests from DjangoLux and remote
commands. A compromise of, or an injection bug in, that component must not turn
into host-Docker control: `EXEC` on any container, or a container created with
host mounts, is root-equivalent on the host in most deployments. Nor may it turn
into a read of the stack's secrets: a container inspect carries every service's
environment (the database password, `SECRET_KEY`), and `archive`/`export` carry its
files. So the agent gets no Docker access at all, and the executor answers its reads
with only the fields it uses.

## Architecture

```
 Control Plane, registries, PyPI
     │ outbound only
     ▼
 composer-agent
 (no docker.sock, no DOCKER_HOST)
     │ typed request: a write, or a read
     │ (private Unix socket)
     ▼
 composer-executor ──── docker.sock (rw) ───▶ dockerd
 (watches the image-update trigger;
  serves typed operations;
  re-validates every request;
  returns bare fields for reads)
```

| Service | Docker access | Network | Job |
| --- | --- | --- | --- |
| `composer-agent` | none: no socket mount, no `DOCKER_HOST` | `egress` and `docker_proxy` | triggers, registry and PyPI checks, staging verified packages, the Operations card, the egress relay, the control plane |
| `composer-executor` | the only service with `/var/run/docker.sock`, read-write | `docker_proxy` only | image updates, restarts, recreates, start/stop, repairs, package activation, the agent's reads |

`docker_proxy` is generated `internal: true` when the stack has an `egress`
network, so the executor has no route out: giving it egress would put the
host-root-equivalent process on a routed network. Migrated stacks keep the networks
they already had (see [topology migrations](stack-migrations.md#topology-migrations)).

The executor is the **sole holder of write authority** and the **security
boundary**. The agent pre-validates for a useful error; the executor re-validates as
the authority and never trusts the caller.

The generated block lives in [`agent_installer.py`](../composer/agent_installer.py)
(`_hardened_stack`).

### Capabilities

All three services run `cap_drop: ALL` with `no-new-privileges:true`. The executor
runs `docker compose up`, which reads the project's `0600` `.secrets/.env` locally to
resolve secrets before handing the config to dockerd. `cap_drop: ALL` strips
`CAP_DAC_OVERRIDE`, so even UID 0 cannot read a file it does not own: the deploy
would fail the secrets guard with a Permission-denied error, and the only workaround
would be a manual per-host `setfacl`, which defeats unattended inline updates. The
executor therefore carries `cap_add: [DAC_OVERRIDE]`, which is part of Docker's
default capability set. The project mount is `:ro`, so it grants no write there. The
network-facing agent never deploys and keeps no file capabilities. Dropping that
`cap_add` breaks every inline deploy, and the stack tests assert it.

Never `DAC_READ_SEARCH`, which stacks generated through 1.5.3 carried. Docker leaves
it out of the default set because it also unlocks `open_by_handle_at`, which can
reach any file on the host filesystem behind a bind mount (the "Shocker" escape; the
default seccomp profile allows the call once the cap is granted). `composer check
--fix` swaps it for `DAC_OVERRIDE` on every role. (The short-lived app-permissions
walk is a separate container with `--network none`; see
[app data permissions](app-permissions.md#what-check---fix-grants).)

### No Docker proxy

The agent needs three things from Docker:
- each service's state, health and status line, for the Operations card;
- a service's log tail;
- the local `RepoDigests` of the image it checks for updates.

`docker compose ps` and `docker compose logs` both inspect every container
(`GET /containers/{id}/json`), and that answer carries the container's environment.
A proxy can allow or refuse a path, but it cannot strip fields from a response. So a
read-only `docker-socket-proxy` lets a compromised agent read every secret the
stack's containers hold, plus their files through `archive` and `export`. The
generated block has no proxy. The executor answers the agent's reads
([below](#the-agents-reads)).

A stack that still runs a proxy beside `composer-executor` gets a `docker-proxy`
WARN from `composer check`. A write-enabled proxy is a FAIL: any write endpoint lets
a client start a privileged container, which is root on the host.

`composer check --fix` removes the proxy from the generated block. That covers the
service with the comment lines above it, the agent's `DOCKER_HOST` that points at it,
and its `depends_on` entry. Before the file is written, `docker compose rm -s -f
docker-socket-proxy` takes down its container: a deploy does not remove orphans, so a
proxy left running would stay reachable from the agent on `docker_proxy`. If the
removal fails, no file is written. A proxy outside the generated block is the
operator's to remove.

## What the executor owns

The executor reuses Composer's own operation code (`docker_compose_manager`,
`version_gate`, the restart allowlist) rather than reimplementing it. One lease
serializes every write: a write request that arrives while another operation runs
is refused ("Executor is busy with another operation."). Reads do not take the
lease, so the Operations card and the update check keep working during a deploy.
At most two reads run at once; a third is refused ("Executor is busy with other
reads.").

### The trigger-watched image update

DjangoLux (the inline updater, or the agent bridging a remote `dlux.image_update`)
writes `state/image-update-request.json` on the runtime volume. The executor
(`executor run --trigger-file … --interval 2`) runs the `composer update` pipeline
for each new request: pull → `version_gate` downgrade-reject → recreate → health →
post-start. The agent performs no updates; it reports status by reading the
request's ack and `deploy-status.json`.

### Socket operations

Every request is a JSON object of at most 64 KiB with exactly `protocol_version`
(which must be `1`), a UUID `operation_id`, `op` and an object `payload`; each
operation refuses payload fields it does not list
([`executor_protocol.py`](../composer/executor_protocol.py), handlers in
[`executor_ops.py`](../composer/executor_ops.py)).

| `op` | Payload | Executor-side checks | Sent for |
| --- | --- | --- | --- |
| `restart` | `service` (empty: the allowlist) | not a protected service; on `COMPOSER_AGENT_RESTART_SERVICES`; an empty service needs a non-empty allowlist | `composer.restart`, the Operations card |
| `start`, `stop` | `service` | not in `PROTECTED_LIFECYCLE_SERVICES`; on the allowlist; the container's state allows it | the Operations card |
| `recreate` | `service` | not protected; on the allowlist; the deployment's secrets readable; app data ready ([app permissions](app-permissions.md#before-every-start-of-the-app)) | the Operations card |
| `recovery_deploy` | `force` (strict boolean), `reason` (non-empty) | runs `composer update` (with `--force` when set) on the deployment's own Compose files; the request names no image or file | `composer.recovery_deploy` |
| `dlux_package_apply` | `version`, `filename`, `sha256` | the filename must be a `django_lux-…whl` name (so it can never carry a path separator); the digest SHA-256 hex; the staged bytes must hash to it; the manifest inside the wheel must name that version and permit inline install | the agent's package trigger |
| `dlux_package_rollback` | none | restores the previous release already on the volume | the agent's package trigger |
| `deployment_check` | `dev` (strict boolean), `file` (a bare `*.yml`/`*.yaml` name in the project) | check and repair preview only; returns redacted, bounded findings | the Operations card's `check` |
| `check_fix` | `compose_digest` | the files must still hash to the digest the preview returned | the Operations card's `check-fix-apply` |
| `agent_update` | `token` | starts a detached helper that replaces the resident pair | the Operations card's `agent-update` |
| `services_state` | `dev`, `file` (as `deployment_check`) | read; returns `name`, `state`, `health`, `status` and `restart_label` per Compose service, nothing else | the Operations card's `services`, and the service check before a restart, start, stop or log tail |
| `logs_tail` | `dev`, `file`, `service` (empty: every service, merged), `lines` (an integer, 1–1000) | read; the service must be one of the deployment's; the text is redacted in the executor, capped at 60 000 characters, and its oldest lines dropped until the result fits one frame | the Operations card's `logs-tail` |
| `image_digests` | `image` (an image reference) | read; the image must be `WEB_IMAGE`, `COMPOSER_CHECK_IMAGE` or a `--check-image` of the generated block; returns its `RepoDigests` only | the agent's update check and its local-digest probe |

Protected services: `PROTECTED_RESTART_SERVICES` (`service_selection.py`) covers the
databases, Redis, backups, `pgadmin`, `dlux-updater`, the Composer services and
`docker-socket-proxy`; `PROTECTED_LIFECYCLE_SERVICES` adds `web`, `worker`, `celery`, the beat
services and the proxies, so start/stop can never take down the path the panel needs
to start them again.

### Inline DjangoLux packages: fetched by the agent, swapped by the executor

An inline release swap needs two capabilities this split deliberately keeps apart:
**egress** to fetch and verify the wheel, and **Docker authority** to restart and
health-gate the services running it. The executor has the second and none of the
first. So the halves each do what they can:

1. the agent owns `package-update-request.json` (the executor's loop ignores it);
2. on a request the agent resolves the release, verifies PyPI's Trusted Publisher
   attestation and the SHA-256, and writes the wheel to `downloads/` on the shared
   runtime volume;
3. it sends `dlux_package_apply` with the version, filename and digest;
4. the executor re-hashes the staged bytes, re-reads the manifest inside the wheel,
   unpacks, activates, restarts, health-gates, and rolls back if the deployment does
   not come back healthy, without one outbound connection.

The digest travels over the socket rather than in a file beside the wheel because
`worker` and `celery` mount that volume read-write too: a record kept next to the
bytes is a record a planted wheel's author could write. Bytes may come from the
volume; what they must hash to may not.

The agent still holds no Docker authority: it stages a file and asks. Exit 3
("rolled back and still unhealthy") crosses back unchanged, so a caller that retries
on failure does not retry a deployment that needs a human. The lock, quarantine and
backup rules are the same as for an operator's `dlux update`
([inline DjangoLux updates](updates.md#inline-djangolux-updates)).

### Not executor operations

- **`dlux.backup.create`** runs a DjangoLux management command inside the DLUX
  container (no Docker authority); the agent bridges it to DLUX unchanged.
- **`agent.rotate_credentials`** is agent-local (credential store, no Docker).
- **Fetching a DjangoLux release** needs the network the executor does not have; the
  agent stages the verified wheel and the executor swaps it.

### Hard rejections

- Arbitrary Docker requests, raw `compose` arguments, shell strings, container
  definitions, mounts, images, volumes, networks.
- Any `exec`: the only exec paths (backups) are DjangoLux-side, not in the executor.
- Any service outside the allowlist, or a protected one.
- Unknown operations, payload fields or protocol versions.

### The agent's reads

The three reads are executor operations (`READ_OPS` in `executor_protocol.py`). The
agent calls them through `executor_client.read`, and the agent-side code in
`ops.py` (`_service_table`, `_tail_logs`) and `watcher.py` (`_local_repo_digest`)
never runs `docker` itself. The agent still applies its own restart policy to the
service table; the executor re-applies it on every write.

An executor that does not know a read, because only half of the resident pair is
updated, is reported as "Update both Composer resident services and retry". The
agent does not fall back to a proxy. A digest probe the executor cannot answer is
*unknown*, never a change.

The executor sets `COMPOSER_IN_EXECUTOR` in its own environment, so its own code and
its children read Docker directly instead of asking themselves over the socket.

## The private socket

- **Transport:** a Unix domain socket, `COMPOSER_EXECUTOR_SOCKET` (default
  `/run/composer-exec/composer-exec.sock`), on the dedicated `composer_exec_sock`
  volume mounted into the agent and the executor only. Never TCP, never the runtime
  volume. The agent uses executor mode whenever that variable is set.
- **Authorization:** the socket is mode `0660` and binding refuses a symlinked path.
  Every connection's uid is checked (`SO_PEERCRED` on Linux, `LOCAL_PEERCRED` on
  macOS). The expected uid is `COMPOSER_EXECUTOR_PEER_UID` when set, otherwise the
  executor's own uid: the generated pair runs the agent and the executor from the same
  image as the same user, so the check is on without configuration. A peer whose uid
  cannot be read is refused, and a value that is not a uid stops `executor run` with
  exit 2 instead of turning the check off. Set the variable only when the agent runs
  as another user.
- **Framing:** a 4-byte big-endian length prefix and a JSON body, requests and results
  each capped at 64 KiB. A result that would exceed the cap is replaced by a `failed`
  result for the same operation that gives its size, so the agent always gets an
  answer. One request per connection; the executor closes after the result. At most
  four connections are handled at once.
- **Versioning:** every request carries `protocol_version`; the executor rejects an
  unknown or mismatched version with a typed error instead of guessing. A transient
  agent/executor skew (mid-update, say) therefore fails safe: the agent reports the
  mismatch rather than handing an operation to an executor that might interpret it
  differently.
- **Liveness:** the agent treats an unavailable socket as "executor down" and reports
  it. There is no direct Docker path for it to fall back to. The client waits up to
  30 minutes for a result (`COMPOSER_EXECUTOR_TIMEOUT`).

## The resident pair updates together

Composer has two Docker-authority paths, and the split keeps them apart:

- **Operator / deployer path:** `start.sh` → `docker run debeski/composer …` with the
  host socket. Operator-invoked, transient, not network-facing; it legitimately holds
  full Docker access. `update`, `agent update`, `agent restart` and `agent off` run
  here.
- **Resident path:** the long-lived pair. Only the network-facing half is
  de-privileged.

No resident service recreates itself through its own authority: `agent update` is
performed by the transient deployer (or, from the Operations card, by a detached
helper the executor starts), so the executor is never asked to replace itself.
`agent update`, `agent restart` and `agent off` act on `composer-agent` and
`composer-executor` together: they run the same image, which is pulled once, so
they can never drift to different versions. With the socket's `protocol_version`
check, a partly applied update fails safe instead of running a new agent against an
old executor.

## Bridge files and queues

The runtime volume is shared by the app services, the agent and the executor:

- group write on runtime files is the contract, not a defect: files are `0664` in
  `0775` directories, because a non-root app (uid 10001, group 0) and the agent
  (uid 0 without capabilities) reach each other's entries only through group 0
  ([app data permissions](app-permissions.md#composers-own-writes));
- documents are written to a temporary file in the same directory, flushed and
  renamed over the target, so a reader never sees half a file. The temporary file
  is created exclusively under a random name (`composer/safe_files.py`): a fixed
  name such as `.x.json.tmp` could already be a symlink an app service planted,
  and writing through it would clobber its target. The rename replaces the target
  itself, a planted symlink included, without following it;
- documents another service writes (the bridge results, `snapshot.json`,
  `enroll-request.json`) are read with `O_NOFOLLOW`, only when they are a regular
  file, and only up to 64 KiB (256 KiB for the snapshot);
- the package-operation lock opens with `O_NOFOLLOW`, refusing a planted symlink;
- inputs the agent consumes are bounded where it parses them: control-plane commands
  (64 KiB), relay requests (64 KiB each, at most 64 pending,
  [egress relay](relay.md#what-the-agent-enforces-on-every-call)), downloaded indexes
  and wheels, and Operations results (200 findings, 2,000 characters each, 20,000 per
  diff);
- the control-plane outbox keeps only the newest pending snapshot; commands and
  events replay in order from SQLite;
- at most 32 commands wait or run at once (`MAX_QUEUED_COMMANDS`); a command beyond
  that fails at once with an event saying the queue is full;
- one operation queues at most 64 events (`MAX_EVENTS_PER_OPERATION`). Further
  progress events are dropped before they take a sequence number, so the sequence
  stays gapless, and the terminal event is always recorded. Nothing is queued after
  a terminal event;
- the newest 1,000 finished commands are kept so a redelivered operation is still
  recognised; older ones are forgotten once all their events are sent;
- an event carries only the last 32 KiB of the deploy log, and a streamed command
  keeps the last 5,000 lines of its output (every line is still printed).

## Child processes

Composer runs every child process from an argument list, never a shell string. On
Windows the executable is resolved through `PATH`/`PATHEXT` instead of `shell=True`,
so `cmd.exe` never re-parses an argument, and a batch file (`.bat`, `.cmd`) is
refused because Windows would run it through `cmd.exe` anyway.
