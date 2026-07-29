# Composer Executor Hardening

Standalone security release. Replaces the network-facing agent's broad Docker
authority with a small, typed **executor** that holds the Docker socket and
exposes only the operations Composer actually performs.

> **Independent of the fleet-management plan.** This hardening needs none of the
> remote-settings / snapshot-v2 / alerts / profiles work and must ship, canary,
> and roll back on its own track — ahead of, and decoupled from, any remote
> mutation feature. It is a *prerequisite* for those features, not part of them.

---

## 1. Current state and why this matters

Today the network-facing `composer-agent` is handed near-total Docker authority:

- It reaches Docker through `DOCKER_HOST=tcp://docker-socket-proxy:2375`
  ([agent_installer.py](../composer/agent_installer.py)).
- The `docker-socket-proxy` (tecnativa) is granted
  `CONTAINERS, IMAGES, NETWORKS, VOLUMES, EVENTS, EXEC, POST` all `=1`.
  `EXEC=1` + `POST=1` across every resource is effectively **arbitrary code
  execution in any container** plus full create/destroy of
  containers/images/volumes/networks.

Yet the agent's *actual* Docker-mutating surface is a small named set
([`agent_protocol.REMOTE_ACTIONS`](../composer/agent_protocol.py)):

| Action | What it does | Existing guard |
|:--|:--|:--|
| `dlux.image_update` | `compose pull` + `up -d --force-recreate` (scoped) | `version_gate` rejects downgrades; scoped service list |
| `composer.restart` | `compose restart <service>` | `PROTECTED_RESTART_SERVICES` + `COMPOSER_AGENT_RESTART_SERVICES` allowlist |
| `composer.recovery_deploy` | recreate from known-good state | requires a reason |
| `dlux.backup.create` | management command via `compose exec`/`run` | scoped one-off |
| `agent.rotate_credentials` | credential store only | **no Docker authority** |

Crucially, on a remote command the agent does **not** call Docker itself — it
spawns a `python -m composer restart|update …` **child process**
([agent.py `_run_child`](../composer/agent.py)) that drives `docker compose`.
The privilege is over-provisioned relative to what's used: the whole attack
surface exists to run four bounded operations.

**Threat model.** The agent holds an outbound connection to a Control Plane and
processes untrusted-ish remote commands. A compromise or a command-injection bug
in that network-facing component currently converts to full host-Docker control
(`EXEC` on any container = root-equivalent on the host in most deployments). The
goal is to make the network-facing component hold **zero** Docker authority.

---

## 2. Target architecture (decided)

Two decisions are locked:

1. **Separate executor, same image.** A new `composer-executor` role (reuses the
   `debeski/composer` image, different command) holds the real Docker socket and
   performs every Docker **write**. The `composer-agent` keeps all its
   control-plane, enrollment, validation, and status logic and its **read-only**
   Docker access — its `DOCKER_HOST` still points at `docker-socket-proxy`, but
   that proxy is demoted so the agent can inspect and never mutate. The agent
   holds **no real Docker socket and no write authority**; it asks the executor
   to perform writes over a private Unix socket on a shared volume.
2. **`docker-socket-proxy` demoted to read-only.** It stays for the agent's
   health/inventory reads (`ps`, `inspect`, `image inspect`, events) but with
   `POST=0, EXEC=0` — GET-only. A compromised agent can therefore enumerate but
   never mutate; all write authority lives only in the executor.

```
 Control Plane
     │ outbound only
     ▼
 composer-agent ──── reads ───▶ docker-socket-proxy   (POST=0, EXEC=0)
 (no real socket;                  │ ro (GET only)
  DOCKER_HOST → RO proxy)          ▼
     │ typed write request      dockerd
     │ (private unix socket)       ▲
     ▼                             │ docker.sock (rw)
 composer-executor ───────────────┘
 (watches image-update trigger;
  socket: restart, recovery_deploy;
  re-validates every request)
```

The executor is the **sole holder of write authority** and the **security
boundary**. The agent pre-validates for UX; the executor re-validates as the
authority (defense in depth — never trust the caller).

---

## 3. What the executor owns

The executor is the only process with Docker authority. It owns **two channels**,
both reusing the existing proven op code (`docker_compose_manager`,
`version_gate`, the restart allowlist) rather than reimplementing it. A single
one-operation-in-flight lease serializes across both channels.

### A. Trigger-watched image update (relocated, not new)

Image updates are already file-triggered: DLUX — the inline updater, or the agent
bridging a remote `dlux.image_update` — writes `image-update-request.json`, and a
resident watcher shells the `composer update` pipeline (pull → `version_gate`
downgrade-reject → recreate → health → post-start). Today that watcher runs
inside the agent (`WatchRuntime`, honoring `DOCKER_HOST`). This work **moves the
watcher's Docker-executing loop into the executor**; the trigger contract with
DLUX is unchanged, only the process holding the socket changes. The agent no
longer performs updates — it reports status by reading `deploy-status.json`, as it
already does.

### B. Typed socket for control-plane-initiated ops

Only operations the agent initiates from a remote command need the socket:

| Socket request | Validated fields | Executor-side re-validation |
|:--|:--|:--|
| `restart` | `operation_id`, `service?` | Reject `PROTECTED_RESTART_SERVICES`; require membership in `COMPOSER_AGENT_RESTART_SERVICES`; empty service ⇒ the configured allowlist only. |
| `recovery_deploy` | `operation_id`, `reason` (non-empty) | Recreate only from the known-good active state; never an arbitrary image/compose. |

Each request carries `protocol_version`; the executor rejects an unknown version
(the fail-safe handshake) and runs one operation at a time.

### Not executor operations

- **`dlux.backup.create`** runs a DjangoLux management command inside the DLUX
  container (no Docker authority); the agent bridges it to DLUX unchanged.
- **`agent.rotate_credentials`** is agent-local (credential store, no Docker).
- **Reads** — availability via `docker image inspect`, `ps`/`inspect` for health —
  stay with the agent through the **read-only** `docker-socket-proxy`.

### Hard rejections (return typed error, never execute)

- Arbitrary Docker requests, raw `compose` args, shell strings, container
  definitions, mounts, services, images, volumes, networks.
- Any `exec` (the only exec paths — backups — are DLUX-side, not in the executor).
- Any image without verified release metadata or an immutable digest (image-update
  path, enforced by `version_gate`).
- Any service outside the configured scope/allowlist.

---

## 4. Private Unix socket protocol

- **Transport:** a Unix domain socket on a dedicated shared volume
  (`composer_exec_sock`), mounted into agent and executor only. Never TCP, never
  the runtime bridge dir.
- **Authorization:** filesystem permissions (`0770`, dedicated uid/gid shared by
  the two roles) **plus** `SO_PEERCRED` peer-uid check in the executor. Optional
  shared secret handed to both roles via the same mechanism as existing agent
  env, as belt-and-suspenders.
- **Framing:** length-prefixed JSON request → JSON result, both bounded (reuse
  the existing 64 KiB command limit). One request per connection; the executor
  closes after the terminal result.
- **Versioning:** every request carries a `protocol_version`; the executor
  rejects an unknown or mismatched version with a typed error instead of
  guessing. A transient agent/executor skew (e.g. mid-update) therefore fails
  safe — the agent surfaces "executor version mismatch" rather than handing an
  operation to an executor that might interpret it differently.
- **Liveness:** the agent treats socket-unavailable as "executor down" and
  surfaces it as a health/agent-status condition — it must never fall back to a
  direct Docker path (there is none).

---

## 5. Bridge-file and queue hardening

Applies to the shared runtime bridge the agent and executor use, and is part of
this release even where it's not executor-specific:

- Symlink-safe writes: private unique temp file in the same dir, `O_NOFOLLOW`
  reads, restrictive perms, atomic `rename` replacement.
- Reject following symlinks on read; reject world/group-writable inputs.
- Size and count caps on every file consumed.
- Bounded command, event, result, snapshot, and artifact queues (drop-oldest or
  reject-new with a typed error; never unbounded growth).

---

## 6. DjangoLux (scaffold) side changes

The `docker-socket-proxy` service, the `docker_proxy` network, and the agent's
Docker wiring are generated by dlux and asserted by its stack contract, so this
release is a coordinated **composer + dlux-scaffold** change.

- [`compose.yml.tmpl`](../../pkg-django-lux/dlux/scaffold_templates/project/compose.yml.tmpl):
  - Add the `composer-executor` service (same image, `executor` command, holds
    the real `docker.sock` rw, not network-facing).
  - Keep `composer-agent`'s `DOCKER_HOST` pointing at the (now read-only) proxy
    for its reads; add `COMPOSER_EXECUTOR_SOCKET` and the shared
    `composer_exec_sock` volume so it can send typed writes to the executor.
  - Flip the proxy env to `POST=0, EXEC=0` (and drop `VOLUMES`/`NETWORKS`; keep
    GET for containers/images/events).
  - Add the `composer_exec_sock` volume, mounted into agent + executor only.
- [`stack_contract.json`](../../pkg-django-lux/dlux/stack_contract.json): add
  `composer-executor` (networks, restart class, read/write volume split so the
  executor — not the agent — is the docker-authority holder); adjust
  `docker-socket-proxy` to read-only.
- [`test_scaffold.py`](../../pkg-django-lux/dlux/tests/test_scaffold.py) network
  topology tests: assert the agent has no docker path, the executor is the only
  service on the docker-authority path, and the proxy publishes no write grant.
- Docs: security/DSRP, inline-updater deployment architecture, reference stack
  table.

## 7. Composer install/migrate path

- [`agent_installer.py`](../composer/agent_installer.py): emit the executor
  service + read-only proxy + socket volume; keep the guarded, diff-printed
  migration UX. Add `composer-executor` to the exclusion/self-management sets
  alongside `composer-agent`, `docker-socket-proxy`.
- Migration for live deployments (decrees, dhub, sales-crm, …): an
  `enable-executor` one-cycle forwarder that prints the migration diff from
  "broad-proxy agent" to "executor + read-only proxy", mirroring how
  `enable-agent` migrated `composer-updater` → `composer-agent`. Carry forward
  the existing caveat: a stale pre-migration proxy/agent must be reconciled
  before the first post-migration update.
- **`enable-executor` MUST be wired into `composer check --fix`**, exactly like
  `enable-agent` is today. `check` already detects the legacy-agent topology
  (`_check_topology`) and hints at the fix; `_maybe_fix` gains an "agent present,
  no executor" condition that — after the standard consequences/confirm prompt —
  runs the `enable-executor` migration. So an operator never has to know the
  command name: `composer check --fix` both diagnoses and hardens, and
  `composer enable-executor --apply` remains the direct path. This is a
  non-optional acceptance criterion for the slice.

### Self-update and version consistency (the resident pair)

Composer already separates two Docker-authority paths, and the split preserves
that separation rather than adding a new self-recreation problem:

- **Operator / deployer path** — `start.sh` → `docker run debeski/composer …`
  with the host socket. Operator-invoked, transient, not network-facing;
  legitimately holds full Docker access. This is how `update`, `agent-update`,
  `agent-restart`, and `agent-off` already run.
- **Resident / agent path** — the long-lived, network-facing agent that processes
  remote Control-Plane commands. This is the only path being de-privileged.

Consequences to design in:

- **No resident service recreates itself.** `agent-update` is an operator action
  performed by the transient deployer, which recreates the resident services — so
  the executor holding the socket is never asked to replace itself, and no
  network-facing component needs Docker authority to self-update.
- **`agent-update` must target the resident *pair*.** Today `AGENT_SERVICE` is a
  single `composer-agent` ([launcher.py](../composer/launcher.py)); with the
  split, `agent-update`, `agent-restart`, `agent-off`, and `agent-check` must act
  on `composer-agent` **and** `composer-executor` together — pull the shared image
  once and recreate both, so they can never drift to different versions.
- **`agent-check`** checks one image for the pair (both are `debeski/composer`), so
  a single availability check still covers both.

Together with the socket `protocol_version` handshake (§4), a partially-applied
update fails safe instead of running a new agent against an old executor.

---

## 8. Rollout and canary

Own release train, deployed before any remote-settings work:

- Composer `v1.3.0` (executor role + agent client + installer/migration).
- DjangoLux scaffold `v1.6.0` (executor service, read-only proxy, contract, tests).

Deploy order — **Composer first.** A freshly-generated project references the
`executor` command; if the dlux scaffold shipped first, that project would pull a
`composer:latest` without the executor and fail to start. So:

1. **Composer `v1.3.0` published first** — `debeski/composer:latest` now has the
   `executor` role, so both new stacks and `enable-executor` migrations resolve.
2. DjangoLux scaffold `v1.6.0` published — new generations get the hardened stack.
3. Existing deployments migrate via `composer check --fix` / `enable-executor`
   (diff-reviewed), then a normal update recreates the agent + executor.
4. Non-production canary: run image update, `restart`, and `recovery_deploy`
   end-to-end through the executor; confirm a raw Docker mutation from the agent
   fails (its proxy is read-only).
5. Low-risk production canary, then fleet-wide.

Rollback: revert Composer `v1.3.0` (executor withdrawn, agent returns to the prior
proxy model) independently of dlux; no feature depends on this, so rollback is
clean.

---

## 9. Test plan

Executor:

- Rejects every non-typed request: raw docker/compose args, shell, container
  defs, mounts, arbitrary images/volumes/networks, non-backup exec.
- `version_gate` downgrade rejection; scope/exclusion enforcement.
- Restart allowlist + protected-service rejection.
- `recovery_deploy` requires a reason and uses only known-good state.
- Requires verified release metadata or immutable digest.
- Single in-flight lease; second concurrent request rejected.

Socket & bridge:

- Peer-uid rejection; wrong-perms socket rejection.
- Symlink/traversal/oversized/queue-exhaustion inputs rejected.
- Length-prefix bounds; malformed frames rejected.

Agent:

- Its `DOCKER_HOST` proxy is read-only: a mutating Docker call (create/recreate/
  exec) from the agent is rejected by the proxy; only reads succeed.
- Holds no real Docker socket; all writes go through the executor.
- Executor-down surfaces as a health condition, never a direct-Docker fallback.

dlux scaffold:

- Topology tests: agent off the docker-authority path; executor is the only
  writer; proxy publishes no write grant.
- Generated stack matches `stack_contract.json`.

End-to-end:

- Old-agent → migrate → executor path for update/restart/recovery/backup.
- Composer/executor restart mid-operation; durable replay is a no-op.
- Live `image_update` + rollback with the executor in place.

---

## 10. Non-goals

- No remote-settings, snapshot-v2, alerts, or profiles work (separate plan).
- No change to the Control-Plane protocol beyond what the split requires.
- No new remote capability — the executor only relocates existing authority.
