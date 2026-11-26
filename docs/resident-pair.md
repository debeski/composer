# The resident pair

One image (`debeski/composer`) has three faces:

| face | runs | job |
| --- | --- | --- |
| **deployer** | on demand, from `start.sh`/`start.ps1` | deploy, update, check, stop, logs, run, migrate |
| **agent** | resident (`composer agent run`) | watches triggers and registries, stages verified packages, publishes availability, delegates every write |
| **executor** | resident (`composer executor run`) | applies updates, restarts, repairs and package activations over a private socket |

The agent and the executor run inside the stack as `composer-agent` and
`composer-executor`, so an update or a repair can be triggered from the DjangoLux
admin UI without giving the web-facing side the Docker socket. Who may do what
between them: [executor hardening](executor-hardening.md). The files they exchange
with DjangoLux: [Agent Protocol v1](agent-protocol-v1.md).

## `composer-agent`

`composer agent run` is the network-facing half. It answers, from one loop:

- the package-update trigger DjangoLux writes on the runtime volume: it stages
  verified DjangoLux wheels and hands them to the executor
  ([inline DjangoLux updates](updates.md#inline-djangolux-updates)). Image-update
  requests are the executor's own trigger; the agent reports their status;
- registry and PyPI availability checks, published for DjangoLux's Updates card
  ([image availability](updates.md#image-availability));
- the Operations card's requests ([operations](agent-protocol-v1.md#operations-requests-from-djangolux));
- the egress relay: the application's named outbound calls ([egress relay](relay.md));
- the maintenance page, published on every start ([maintenance page](maintenance-page.md)).

It preserves the local trigger/status/ack and registry-availability files of
`agent watch`, and adds a typed DLUX spool, SQLite command/outbox replay, operation
correlation, safe restart allowlisting, and optional outbound HTTPS long polling to a
control panel. Configure `COMPOSER_CONTROL_URL`, a 15-minute one-use
`COMPOSER_ENROLLMENT_TOKEN`, and `COMPOSER_AGENT_STATE_DIR` (default
`/var/lib/composer-agent`). Enrollment pins the normalized control URL in local state;
changing panels requires revocation and re-enrollment or an explicit local state
reset. Control requests reject redirects. Pending snapshots coalesce to the newest
state, while commands and events retain durable replay. Local DLUX-triggered updates
continue while the control plane is unavailable or the machine credential is
revoked. Protocol: [Agent Protocol v1](agent-protocol-v1.md#transport-and-authentication).

## `composer-executor`

`composer executor run` runs the privileged half. The executor is the sole holder of
Docker write authority: it owns the real `docker.sock`, runs the trigger-watched
image-update loop, and serves typed operations to the agent over a private Unix
socket (`COMPOSER_EXECUTOR_SOCKET`). The network-facing `composer-agent` has no
Docker access: it asks the executor for every write and for its few reads, which the
executor answers with bare fields
([the agent's reads](executor-hardening.md#the-agents-reads)). The generated Compose block starts it; it is not for
interactive use.

Both halves read `DLUX_UPDATE_POLICY` from their own environment and refuse the
update requests it does not allow, the executor even when the agent asks for them
([what the application may request](updates.md#what-the-application-may-request)).

## `agent watch`

`composer agent watch --trigger-file PATH [--interval N]` is the plain watcher that
`agent run` succeeds; it performs updates itself, in its own process. It watches
the trigger file and, on each new request (a changed `token`, or the file's `mtime`),
runs the full `composer update` pipeline (pull → version gate → downgrade guard →
recreate → health → post_start). The processed token and child exit code are recorded in
`<trigger-file>.ack`, so a request is applied once and survives a restart.

- `--status-file PATH` has each run publish [deploy status](deploying.md#deploy-status);
  if the child exits before publishing its own terminal failure, the watcher
  guarantees a token-matched `failed` status so maintenance consumers are never left
  waiting on a dead process. That status, a policy refusal's included, describes
  only its request: it keeps what the file says when that names the request (its
  `request_token`, the `run_token` of the DjangoLux run that wrote it, its
  `operation_id`) or was written while it ran, and otherwise replaces the earlier
  run's record instead of merging into it.
- `--check-image`, `--availability-file` and `--check-interval` make it publish image
  availability ([image availability](updates.md#image-availability)).

### It never updates itself

When the watcher runs inside the same Compose project it is updating, it excludes
the resident services from child runs, so pull/config/up/health/post-start operate
on the application services and do not recreate the container that is supervising
the update:

- with `COMPOSER_WATCH_SELF_SERVICE` unset it excludes `composer-updater` and
  `composer-agent`; setting it (also to `""`) replaces that list, and the generated
  block sets it to each role's own name (`composer-agent`, `composer-executor`);
- `COMPOSER_EXCLUDE_SERVICES` adds exclusions and never replaces them. The generated
  block lists `composer-agent`, `composer-executor` and the protected services the
  stack defines, and the executor always excludes the Composer services and a
  leftover `docker-socket-proxy`.

## Lifecycle commands

The resident services have three host-side lifecycle commands, run by the deployer:

- `composer agent update` pins the channel's signed release
  ([the Composer pin](updates.md#the-composer-pin)), pulls and recreates; with
  nothing to pin it pulls and recreates nothing;
- `composer agent restart` restarts and health-checks;
- `composer agent off` stops.

None of them touches the application or the Compose project. When the compose file
defines `composer-executor`, all three target the `composer-agent` +
`composer-executor` pair together: both run the same image, so they can never drift
to different versions. Legacy stacks resolve to just `composer-agent`. The dedicated
commands ignore the resident services' normal self-exclusion only for those explicit
targets. `agent update` does not run application version gates or post-start hooks.
DjangoLux's Operations card offers the same update as `agent-update`. It is refused
unless the channel publishes a release newer than the resident one; the executor
runs it in a helper container from its own image, with the project mounted
read-write over the inherited read-only mount so the pin can be written.

Adding or hardening the pair on an existing stack (`agent enable`,
`executor enable`, `check --fix`): [topology migrations](stack-migrations.md#topology-migrations).
