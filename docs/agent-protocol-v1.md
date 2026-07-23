# Composer Agent Protocol v1

`composer agent` is one outbound-only deployment sidecar per Compose project. Composer owns Docker execution, durable command/event relay, registry discovery, reconnection, and redaction. DLUX owns backup, maintenance, monitoring, update locks, and application state. The control panel owns enrollment, authorization, fleet routing, batches, and current relayed snapshots.

## Transport and authentication

- The control URL must use HTTPS. Plain HTTP is accepted only for explicit localhost development.
- Enrollment uses `POST /api/agent/v1/enroll/` with a one-use token whose server lifetime is 15 minutes.
- Enrollment returns a UUID agent ID and random bearer secret. The agent persists credentials, commands, event sequences, and replay outbox in mode-`0600` SQLite under `COMPOSER_AGENT_STATE_DIR`.
- Every later request sends `X-Composer-Agent-ID` plus `Authorization: Bearer ...`. The server stores only password hashes.
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

The shared runtime spool is `/opt/dlux-runtime/state/agent/{requests,results,processed}` plus `snapshot.json`. Files are written atomically, and DLUX archives each handled request under `processed/` so later commands cannot be starved by historical files. Documents contain typed operation/display metadata only. No agent credential, application secret, environment dump, or unrestricted log belongs in the spool or central database.

A central image update is successful only after DLUX has accepted the request, completed the selected backup, enabled maintenance, triggered Composer, survived recreation, finalized its durable `DluxImageUpdate`, and cleared maintenance. Backup failure or DLUX rejection produces no Composer deployment. Agent crash and control-plane outage rely on SQLite/outbox replay and do not turn connectivity loss into a deployment result.

## Redaction and compatibility

Inherited secret values and authorization/password/secret/token-shaped console output are replaced before leaving the host. The control panel sanitizes typed documents again. Agents report supported schema versions, capabilities, and agent version through enrollment and `PUT /api/agent/v1/capabilities/`. A command is delivered only when the agent advertises its capability.

Canonical examples live in `tests/fixtures/agent-protocol-v1/` and are copied unchanged into the DjangoLux bridge and control-panel test suites.

## Generated-project migration

Composer owns the only Compose transformer. After pulling Composer 1.2.0, run
`./start.sh enable-agent` to review the exact diff and then
`./start.sh enable-agent --apply`. Apply recognizes only generated DLUX updater
markers, verifies the declared DjangoLux bridge version, validates the proposed
document through Docker Compose before any write, preserves the original beneath
`.xpose/dlux-agent-bootstrap/`, and replaces it atomically. The DjangoLux
`enable-agent` command is a one-cycle compatibility forwarder, not a second
implementation.
