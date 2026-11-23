# Composer Agent Protocol v1

`composer agent run` is one outbound-only deployment sidecar per Compose project.
Composer owns Docker execution, durable command/event relay, registry discovery,
reconnection, and redaction. DLUX owns backup, maintenance, monitoring, update
locks, and application state. The control panel owns enrollment, authorization,
fleet routing, batches, and current relayed snapshots.

This page is the contract: the state files DjangoLux and the agent exchange on the
runtime volume, and the control-plane protocol. How the pair runs is in
[the resident pair](resident-pair.md); who may do what is in
[executor hardening](executor-hardening.md).

## Check interval and check requests

With DjangoLux 1.9.0b2+, the resident agent follows `state/check-policy.json`
(`{"schema_version": 1, "interval_seconds": N}`, published by the DjangoLux worker
from the administrator's Options choice) for both its registry and PyPI checks,
re-read every loop tick with a 60-second floor; without it, `--check-interval`
applies (default 900). A new token in `state/check-request.json` makes the agent
re-check images and packages immediately and write `check-request.json.ack` with
that token, once per token, including across restarts.

## Operations requests from DjangoLux

DjangoLux's Operations card asks the resident Composer to perform one **named**
operation: it writes `state/ops-request.json` with a token and an operation name,
and the agent (or `watch`) publishes `state/ops-result.json` and
`ops-request.json.ack` under that token, once per token and across restarts.
`composer/ops.py` owns the table of operations that exist (`HANDLERS`); anything
else is refused with a message rather than attempted, and the request carries no
command, path or flag. The operation name is the whole input, except that the
service operations also carry a `service` (validated against the stack's own
services; optional for `logs-tail`) and `logs-tail` a `lines` count (1–1000).

| Operation | Kind | What it does |
| --- | --- | --- |
| `check` | read-only | runs the same checks as `composer check` (`collect_checkup()`) **and returns the dry-run repairs with their compose digest**, so one operation answers both questions. In the hardened topology it runs in the executor ([below](#read-only-deployment-checks-in-executor-mode)) |
| `check-fix-preview` | read-only | runs each guarded transform in dry-run mode, including the maintenance-page migration (`maintenance-page`), and returns a unified diff per file plus a SHA-256 digest of the deployment files it read (the compose files and the `.proxy/` files that migration rewrites), writing nothing |
| `check-fix-apply` | write | runs the real `check --fix`, but only after the digest DjangoLux hands back (taken from the preview's result) still matches the files on disk, so the change that lands is the change the operator saw |
| `agent-check` | read-only | reads the version label published on the Composer channel's tag and reports it beside the resident version as a `resident` block (`version`, `published_version`, `channel`, `checked`, `update_available`), so the panel can offer the update only when there is one. A registry it cannot read is reported as unknown, never as an update and never as up to date |
| `agent-update` | write | replaces the resident pair through a detached helper that writes the run's ack itself (neither resident survives it), and refuses outright when `agent-check` says the pair already runs the channel's current version |
| `services` | read-only | every Compose service with its state, health, `org.dlux.restart` label, `can_restart` (allow-listed in `COMPOSER_AGENT_RESTART_SERVICES`, not protected), and `can_lifecycle`, `can_start` and `can_stop` |
| `service-restart`, `service-recreate` | write | hand one such service to the executor (`restart`, or `recreate`: `docker compose up -d --no-deps --force-recreate SERVICE`, run with the deployment's secrets resolved like any deploy and refused when they cannot be read), which re-applies the same policy |
| `service-start`, `service-stop` | write | hand one required `service` to the executor's `start`/`stop`: `docker compose start SERVICE` / `stop SERVICE` |
| `logs-tail` | read-only | the last `lines` of one service's logs, or with no `service` the newest `lines` of every service merged in time order, each line with Compose's own `service-N \|` prefix, redacted and capped at 60000 characters |

Findings and diffs are redacted and bounded (200 findings, 2000 characters each,
20000 per diff) before publication, because DjangoLux renders them in a browser. A
handler that raises, returns nothing usable, or cannot write its result is reported
in the ack and never reaches the watch loop.

### Service lifecycle rules

- Start and stop accept no user command, flags, creation or volume deletion. Both
  sides independently check real Compose services, `COMPOSER_AGENT_RESTART_SERVICES`,
  protected labels and state: start for created/exited/stopped containers, stop for
  running/restarting/paused ones. Use Recreate for absent services.
- `PROTECTED_LIFECYCLE_SERVICES` additionally protects `web`, `celery`,
  `celery-beat`, `celery_beat`, `beat`, `worker`, `nginx`, `caddy`, `proxy` from start
  and stop; their restart/recreate capabilities stay unchanged. This preserves the
  browser and worker path needed for Start.
- DjangoLux requires superuser, POST/CSRF, password and audit for start and stop; its
  operation floor is Composer `1.8.0b1`. Older pairs fail closed for these actions.
- The generated resident block allow-lists `worker` right after `web`, and the
  optional `ocr` service (`SAFE_RESTART_CANDIDATES`, label `safe`), so `ocr` can be
  started and stopped like `smtp-relay`. The `check --fix` step that adds `worker`
  also adds it to `COMPOSER_AGENT_RESTART_SERVICES` in both resident services;
  `celery` stays on the list for project tasks. An existing deployment that adds the
  `ocr` service also adds `ocr` to that list in both resident services: `check
  --fix` does not rewrite an existing allow-list.

### Read-only deployment checks in executor mode

In the hardened topology the `check` operation goes over the private socket as the
executor's `deployment_check`. Its only inputs are `dev` (a strict boolean) and an
optional project-local Compose filename; commands, arbitrary paths and symlinks
escaping the project are rejected. It performs check/repair-preview inspection
without applying repairs, and returns already-redacted findings and diffs in a
bounded executor frame. A check that finds failures is a successfully completed
inspection, with failures retained in the report. A missing/older executor returns
an actionable failure, never a clean result. Update both resident services together.

The agent forwards this report to DjangoLux. It does not read deployment secrets or
gain Docker-exec authority. Post-repair verification follows the same route.

## Transport and authentication

- The control URL must use HTTPS. Plain HTTP is accepted only for explicit localhost
  development.
- Enrollment uses `POST /api/agent/v1/enroll/` with a one-use token whose server
  lifetime is 15 minutes.
- Enrollment returns a UUID agent ID and random bearer secret. The agent persists
  credentials, commands, event sequences, and replay outbox in mode-`0600` SQLite
  under `COMPOSER_AGENT_STATE_DIR`.
- Successful enrollment pins the normalized control URL in the same durable state.
  Active credentials reject conflicting environment or pairing-spool URLs; moving to
  another panel requires revocation and re-enrollment or an explicit local state
  reset.
- Every later request sends `X-Composer-Agent-ID` plus `Authorization: Bearer ...`.
  The server stores only password hashes.
- Control-plane requests reject HTTP redirects, including same-origin redirects, and
  therefore never forward agent authentication headers to a redirect target.
- Recovery deployment `force` is a strict JSON boolean; string-shaped values are
  rejected. Sanitized relay output removes complete Authorization values, including
  Bearer credentials.
- Commands are retrieved from `GET /api/agent/v1/commands/next/?wait=25`. Offline is a
  presentation state after 90 seconds without contact, never proof of deployment
  failure.
- While the panel is unreachable, both the command poll and outbox replay back off
  exponentially from 1 to 60 seconds (plus up to 1 second of jitter). Outbox replay
  resumes immediately once a post or a command poll succeeds.
- Credential rotation is two-phase: stage a pending secret, persist it, confirm with
  it, then revoke the old hash. An interrupted confirmation is retried from durable
  agent state.

## Document envelope and bounds

Every document is a JSON object with `schema_version: 1` and is limited to 65,536
encoded bytes. Commands contain a UUID `operation_id`, registered `action`,
timezone-aware ISO-8601 creation/deadline timestamps, bounded actor identity, and an
action-specific object payload. Unknown command or payload fields, unsupported
schemas/actions/transitions, invalid timestamp ordering, and oversized documents
fail closed.

Operation states are `queued → delivered → accepted → running →
succeeded|failed`. `queued` or `delivered` may become `cancelled`; cancellation is
rejected after acceptance. Agent events use a positive per-operation sequence with no
gaps. Exact replays are acknowledged, while conflicting or out-of-order sequences
return conflict.

## Action registry

| Action | Payload | Policy |
| --- | --- | --- |
| `dlux.image_update` | `backup_mode`: `data`, `full`, or `skip` | Delivered to the typed DLUX spool; terminal success waits for DLUX finalization. |
| `dlux.backup.create` | `backup_mode`: `data` or `full` | Creates a DLUX-owned backup; inspection is relayed in the canonical snapshot. Restore remains local. |
| `composer.restart` | optional generated allowlisted `service` | Never restarts protected stateful, database, proxy-gateway, or agent services. |
| `composer.recovery_deploy` | `force` boolean and mandatory reason | Control-panel superuser, dedicated permission, password step-up, warning acknowledgement, immutable audit. |
| `agent.rotate_credentials` | empty object | Internal two-phase machine-credential rotation. |

Arbitrary shell, `run`, `down`, `purge`, build, database restart, backup restore, and
unknown actions are rejected. Agent self-update is not a control-plane action
(DjangoLux's Operations card offers it as `agent-update`), and remote restore is not
a v1 action.

## DLUX bridge and failure semantics

The shared runtime spool is `/opt/dlux-runtime/state/agent/{requests,results,processed}`
plus `snapshot.json`. Files are written atomically, and DLUX archives each handled
request under `processed/` so later commands cannot be starved by historical files.
Pending snapshots use latest-state semantics: a new snapshot replaces the older
unsent snapshot, and startup collapses legacy snapshot backlogs. Commands and events
retain ordered durable replay. Documents contain typed operation/display metadata
only. No agent credential, application secret, environment dump, or unrestricted log
belongs in the spool or central database.

A central image update is successful only after DLUX has accepted the request,
completed the selected backup, enabled maintenance, triggered Composer, survived
recreation, finalized its durable `DluxImageUpdate`, and cleared maintenance. Backup
failure or DLUX rejection produces no Composer deployment. Agent crash and
control-plane outage rely on SQLite/outbox replay and do not turn connectivity loss
into a deployment result.

## Redaction and compatibility

Inherited secret values and authorization/password/secret/token-shaped console
output are replaced before leaving the host. The control panel sanitizes typed
documents again. Agents report supported schema versions, capabilities, and agent
version through enrollment and `PUT /api/agent/v1/capabilities/`. A command is
delivered only when the agent advertises its capability.

Canonical examples live in `tests/fixtures/agent-protocol-v1/` and are copied
unchanged into the DjangoLux bridge and control-panel test suites.
