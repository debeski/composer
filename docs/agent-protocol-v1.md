# Composer Agent Protocol v1

`composer agent run` is one outbound-only deployment sidecar per Compose project. Composer owns Docker execution, durable command/event relay, registry discovery, reconnection, and redaction. DLUX owns backup, maintenance, monitoring, update locks, and application state. The control panel owns enrollment, authorization, fleet routing, batches, and current relayed snapshots.

## Manual image availability refresh

`composer agent check` from a deployment root checks the resident agent/updater's images from the resolved Compose configuration and publishes the same result to its configured availability file. Publication uses `compose exec -T` and a standard-library atomic writer inside the running Composer service, so the CLI does not need the runtime volume mounted and the resident service does not need this CLI upgrade. The UI can read the new result on its next refresh; no service restart is needed. Registry polling otherwise retains its configured cadence (default: one hour).

`-f` and `-d` select Compose configuration using the normal launcher rules. Publication checks all configured `--check-image` entries together. Explicit image arguments are diagnostic and do not replace the deployment's document; `--no-publish` also opts out. `--availability-file PATH` overrides automatic publication and writes in the CLI's filesystem. Deployments without a configured image publisher keep diagnostic behavior. A stopped service or unwritable runtime file returns exit 1 and a stderr diagnostic; `--json` still prints the checked document when publication fails. This operation does not pull images, restart services, or send a control-plane command.

## Check interval and check requests

With DjangoLux 1.9.0b2+, the resident agent follows `state/check-policy.json` (`{"schema_version": 1, "interval_seconds": N}`, published by the DjangoLux worker from the administrator's Options choice) for both its registry and PyPI checks, re-read every loop tick with a 60-second floor; without it, `--check-interval` applies (default 900). A new token in `state/check-request.json` makes the agent re-check images and packages immediately and write `check-request.json.ack` with that token — once per token, including across restarts.

## Operations requests from DjangoLux

DjangoLux's Operations card asks the resident Composer to perform one **named** operation: it writes `state/ops-request.json` with a token and an operation name, and the agent (or `watch`) publishes `state/ops-result.json` and `ops-request.json.ack` under that token, once per token and across restarts. `composer/ops.py` owns the table of operations that exist; anything else is refused with a message rather than attempted, and the request carries no command, path or flag. The operation name is the whole input, except that the service operations also carry a `service` (validated against the stack's own services; optional for `logs-tail`) and `logs-tail` a `lines` count (1–1000). Operations: `check` (read-only) runs the same checks as `composer check` through `collect_checkup()` **and returns the dry-run repairs with their compose digest**, so one operation answers both questions; `agent-check` (read-only) reads the version label published on the Composer channel's tag and reports it beside the resident version as a `resident` block (`version`, `published_version`, `channel`, `checked`, `update_available`), so the panel can offer the update only when there is one — a registry it cannot read is reported as unknown, never as an update and never as up to date; `agent-update` replaces the resident pair through a detached helper that writes the run's ack itself (neither resident survives it), and refuses outright when that check says the pair already runs the channel's current version; `check-fix-preview` runs each guarded transform in dry-run mode — since 1.7.0b4 including the maintenance-page migration (`maintenance-page`) — and returns a unified diff per file plus a SHA-256 digest of the deployment files it read (the compose files and, since 1.7.0b4, the `.proxy/` files that migration rewrites), writing nothing; `check-fix-apply` runs the real `check --fix`, but only after the digest DjangoLux hands back — taken from that preview's result — still matches the files on disk, so the change that lands is the change the operator saw. Since 1.7.0b2: `services` (read-only) returns every Compose service with its state, health, `org.dlux.restart` label and `can_restart` (allow-listed in `COMPOSER_AGENT_RESTART_SERVICES`, not protected); `service-restart` and `service-recreate` hand one such service to the executor (`restart`, or the new `recreate` executor op: `docker compose up -d --no-deps --force-recreate SERVICE`, run with the deployment's secrets resolved like any deploy and refused when they cannot be read), which re-applies the same policy; `logs-tail` (read-only) returns the last `lines` of one service's logs, or with no `service` (since 1.7.0b3) the newest `lines` of every service merged in time order, each line with Compose's own `service-N |` prefix, redacted and capped at 60000 characters. Findings and diffs are redacted and bounded (200 findings, 2000 characters each, 20000 per diff) before publication, because DjangoLux renders them in a browser. A handler that raises, returns nothing usable, or cannot write its result is reported in the ack and never reaches the watch loop.

## Transport and authentication

Composer 1.8.0b1 / DjangoLux 1.14.0b3 adds `service-start` and `service-stop`, carrying one required `service`, delegated to executor `start`/`stop` requests. They run `docker compose start SERVICE` / `stop SERVICE`; no user command, flags, creation or volume deletion is accepted. Both sides independently check real Compose services, `COMPOSER_AGENT_RESTART_SERVICES`, protected labels and state. `services` adds `can_lifecycle`, `can_start` and `can_stop`: start for created/exited/stopped, stop for running/restarting/paused containers. Use Recreate for absent services.

`PROTECTED_LIFECYCLE_SERVICES` additionally protects `web`, `celery`, `celery-beat`, `celery_beat`, `beat`, `worker`, `nginx`, `caddy`, `proxy`; their restart/recreate capabilities stay unchanged. This preserves the browser and worker path needed for Start. DjangoLux requires superuser, POST/CSRF, password and audit; its operation floor is `1.8.0b1`. Older pairs fail closed for these actions. Accept the paired beta before promotion.

Local validation, 2026-10-24: 861 tests OK/four skips and the candidate image's repository smoke script passed. A separate 8096 stack passed actual SMTP-relay Stop/Start through the paired DjangoLux panel in English/Arabic at 1440/390, preserving Web/Celery/proxy protection; SMTP was left running and healthy. The final combined DjangoLux package matches Web/Celery byte-for-byte, with unchanged user/settings fingerprints. Local build exports use candidate versions only; nothing new is tagged or published. Evidence: `pkg-django-lux/service_controls/.xclude/validation/service-controls/`; combined DjangoLux validation is in its `docs/operations.md`. Published paired-beta acceptance remains required before promotion.

- The control URL must use HTTPS. Plain HTTP is accepted only for explicit localhost development.
- Enrollment uses `POST /api/agent/v1/enroll/` with a one-use token whose server lifetime is 15 minutes.
- Enrollment returns a UUID agent ID and random bearer secret. The agent persists credentials, commands, event sequences, and replay outbox in mode-`0600` SQLite under `COMPOSER_AGENT_STATE_DIR`.
- Successful enrollment pins the normalized control URL in the same durable state. Active credentials reject conflicting environment or pairing-spool URLs; moving to another panel requires revocation and re-enrollment or an explicit local state reset.
- Every later request sends `X-Composer-Agent-ID` plus `Authorization: Bearer ...`. The server stores only password hashes.
- Control-plane requests reject HTTP redirects, including same-origin redirects, and therefore never forward agent authentication headers to a redirect target.
- Recovery deployment `force` is a strict JSON boolean; string-shaped values are rejected. Sanitized relay output removes complete Authorization values, including Bearer credentials.
- Commands are retrieved from `GET /api/agent/v1/commands/next/?wait=25`. Offline is a presentation state after 90 seconds without contact, never proof of deployment failure.
- While the panel is unreachable, both the command poll and outbox replay back off exponentially from 1 to 60 seconds (plus up to 1 second of jitter). Outbox replay resumes immediately once a post or a command poll succeeds.
- Credential rotation is two-phase: stage a pending secret, persist it, confirm with it, then revoke the old hash. An interrupted confirmation is retried from durable agent state.

## Document envelope and bounds

Every document is a JSON object with `schema_version: 1` and is limited to 65,536 encoded bytes. Commands contain a UUID `operation_id`, registered `action`, timezone-aware ISO-8601 creation/deadline timestamps, bounded actor identity, and an action-specific object payload. Unknown command or payload fields, unsupported schemas/actions/transitions, invalid timestamp ordering, and oversized documents fail closed.

Operation states are `queued → delivered → accepted → running → succeeded|failed`. `queued` or `delivered` may become `cancelled`; cancellation is rejected after acceptance. Agent events use a positive per-operation sequence with no gaps. Exact replays are acknowledged, while conflicting or out-of-order sequences return conflict.

## Action registry

| Action | Payload | Policy |
| --- | --- | --- |
| `dlux.image_update` | `backup_mode`: `data`, `full`, or `skip` | Delivered to the typed DLUX spool; terminal success waits for DLUX finalization. |
| `dlux.backup.create` | `backup_mode`: `data` or `full` | Creates a DLUX-owned backup; inspection is relayed in the canonical snapshot. Restore remains local. |
| `composer.restart` | optional generated allowlisted `service` | Never restarts protected stateful, database, proxy-gateway, or agent services. |
| `composer.recovery_deploy` | `force` boolean and mandatory reason | Control-panel superuser, dedicated permission, password step-up, warning acknowledgement, immutable audit. |
| `agent.rotate_credentials` | empty object | Internal two-phase machine-credential rotation. |

Arbitrary shell, `run`, `down`, `purge`, build, database restart, backup restore, and unknown actions are rejected. Agent self-update remains a later capability; remote restore is not a v1 action.

## DLUX bridge and failure semantics

The shared runtime spool is `/opt/dlux-runtime/state/agent/{requests,results,processed}` plus `snapshot.json`. Files are written atomically, and DLUX archives each handled request under `processed/` so later commands cannot be starved by historical files. Pending snapshots use latest-state semantics: a new snapshot replaces the older unsent snapshot, and startup collapses legacy snapshot backlogs. Commands and events retain ordered durable replay. Documents contain typed operation/display metadata only. No agent credential, application secret, environment dump, or unrestricted log belongs in the spool or central database.

A central image update is successful only after DLUX has accepted the request, completed the selected backup, enabled maintenance, triggered Composer, survived recreation, finalized its durable `DluxImageUpdate`, and cleared maintenance. Backup failure or DLUX rejection produces no Composer deployment. Agent crash and control-plane outage rely on SQLite/outbox replay and do not turn connectivity loss into a deployment result.

## Redaction and compatibility

Inherited secret values and authorization/password/secret/token-shaped console output are replaced before leaving the host. The control panel sanitizes typed documents again. Agents report supported schema versions, capabilities, and agent version through enrollment and `PUT /api/agent/v1/capabilities/`. A command is delivered only when the agent advertises its capability.

Canonical examples live in `tests/fixtures/agent-protocol-v1/` and are copied unchanged into the DjangoLux bridge and control-panel test suites.

## Generated-project migration

Composer owns the only Compose transformer. After pulling Composer 1.2.0, run
`./start.sh agent enable` to review the exact diff and then
`./start.sh agent enable --apply`. Apply recognizes only generated DLUX updater
markers, verifies the DjangoLux bridge version when a dependency manifest is
present, validates the proposed
document through Docker Compose before any write, preserves the original beneath
`.xclude/dlux-agent-bootstrap/`, and replaces it atomically. Networks, the version
label, and the web image reference are carried over from the block being replaced
instead of being derived from the Compose `name:`, so pre-1.5 scaffolds keep
`egress`/`docker_proxy` and their deployment-specific baked-version label. The
DjangoLux `agent enable` command is a one-cycle compatibility forwarder, not a
second implementation. The transformer reads and writes only the Compose file,
so it runs unchanged on a deployment host that carries no project sources.
