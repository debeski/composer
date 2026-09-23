# Composer Agent Protocol v1

`composer agent run` is one outbound-only deployment sidecar per Compose project. Composer owns Docker execution, durable command/event relay, registry discovery, reconnection, and redaction. DLUX owns backup, maintenance, monitoring, update locks, and application state. The control panel owns enrollment, authorization, fleet routing, batches, and current relayed snapshots.

## Manual image availability refresh

`composer agent check` from a deployment root checks the resident agent/updater's images from the resolved Compose configuration and publishes the same result to its configured availability file. Publication uses `compose exec -T` and a standard-library atomic writer inside the running Composer service, so the CLI does not need the runtime volume mounted and the resident service does not need this CLI upgrade. The UI can read the new result on its next refresh; no service restart is needed. Registry polling otherwise retains its configured cadence (default: one hour).

`-f` and `-d` select Compose configuration using the normal launcher rules. Publication checks all configured `--check-image` entries together. Explicit image arguments are diagnostic and do not replace the deployment's document; `--no-publish` also opts out. `--availability-file PATH` overrides automatic publication and writes in the CLI's filesystem. Deployments without a configured image publisher keep diagnostic behavior. A stopped service or unwritable runtime file returns exit 1 and a stderr diagnostic; `--json` still prints the checked document when publication fails. This operation does not pull images, restart services, or send a control-plane command.

## Check interval and check requests

With DjangoLux 1.9.0b2+, the resident agent follows `state/check-policy.json` (`{"schema_version": 1, "interval_seconds": N}`, published by the DjangoLux worker from the administrator's Options choice) for both its registry and PyPI checks, re-read every loop tick with a 60-second floor; without it, `--check-interval` applies (default 900). A new token in `state/check-request.json` makes the agent re-check images and packages immediately and write `check-request.json.ack` with that token — once per token, including across restarts.

## Operations requests from DjangoLux

DjangoLux's Operations card asks the resident Composer to perform one **named** operation: it writes `state/ops-request.json` with a token and an operation name, and the agent (or `watch`) publishes `state/ops-result.json` and `ops-request.json.ack` under that token, once per token and across restarts. `composer/ops.py` owns the table of operations that exist; anything else is refused with a message rather than attempted, and the request carries no command, path, service or flag — the operation name is the entire input. Operations: `check` (read-only) runs the same checks as `composer check` through `collect_checkup()` **and returns the dry-run repairs with their compose digest**, so one operation answers both questions; `agent-update` replaces the resident pair through a detached helper that writes the run's ack itself (neither resident survives it); `check-fix-preview` runs each guarded transform in dry-run mode and returns a unified diff per file plus a SHA-256 digest of the deployment files it read, writing nothing; `check-fix-apply` runs the real `check --fix`, but only after the digest DjangoLux hands back — taken from that preview's result — still matches the files on disk, so the change that lands is the change the operator saw. Findings and diffs are redacted and bounded (200 findings, 2000 characters each, 20000 per diff) before publication, because DjangoLux renders them in a browser. A handler that raises, returns nothing usable, or cannot write its result is reported in the ack and never reaches the watch loop.

## Transport and authentication

- The control URL must use HTTPS. Plain HTTP is accepted only for explicit localhost development.
- Enrollment uses `POST /api/agent/v1/enroll/` with a one-use token whose server lifetime is 15 minutes.
- Enrollment returns a UUID agent ID and random bearer secret. The agent persists credentials, commands, event sequences, and replay outbox in mode-`0600` SQLite under `COMPOSER_AGENT_STATE_DIR`.
- Successful enrollment pins the normalized control URL in the same durable state. Active credentials reject conflicting environment or pairing-spool URLs; moving to another panel requires revocation and re-enrollment or an explicit local state reset.
- Every later request sends `X-Composer-Agent-ID` plus `Authorization: Bearer ...`. The server stores only password hashes.
- Control-plane requests reject HTTP redirects, including same-origin redirects, and therefore never forward agent authentication headers to a redirect target.
- Recovery deployment `force` is a strict JSON boolean; string-shaped values are rejected. Sanitized relay output removes complete Authorization values, including Bearer credentials.
- Commands are retrieved from `GET /api/agent/v1/commands/next/?wait=25`. Offline is a presentation state after 90 seconds without contact, never proof of deployment failure.
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
