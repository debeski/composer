Implemented the DLUX control-plane foundation across all three projects.

### Delivered

- Composer v1.2.0: outbound `composer agent`, HTTPS enrollment, SQLite durability/replay, typed commands, rotation/re-enrollment, redaction, cancellation handshake, DLUX bridge, and protected restart allowlists.
- DjangoLux v1.5.0: agent spool bridge, operation correlation migration, canonical snapshots, central backup creation, request archival, shared update locking, and `dlux enable-agent --apply`.
- New [DLUX Control Plane](/Users/debeski/Desktop/depy/project-dlux-control/README.md): scoped fleet UI, Uvicorn/ASGI agent API, enrollment/revocation/rotation, operations, batches, snapshots, retention, and audited break-glass recovery.
- Generated deployments now use `composer-agent` with a dedicated state volume, read-only project mount, outbound-only networking, and `docker-socket-proxy`.
- Protocol documentation: [Composer protocol v1](/Users/debeski/Desktop/depy/app-composer/docs/agent-protocol-v1.md) and [DLUX bridge guide](/Users/debeski/Desktop/depy/pkg-django-lux/docs/composer-agent.md).

### Verification

- Composer: 42 tests passed.
- DjangoLux: full 872-test suite passed.
- Control panel: 13 tests passed, including 50 simultaneous ASGI long polls and three-wide fleet batches.
- Migration drift checks passed.
- Docker Compose validation passed.
- All trackers remain below 100 lines; diff and whitespace checks are clean.

### Remaining rollout gates

The code foundation is complete, but these intentionally require real infrastructure:

1. Publish Composer v1.2.0 and DjangoLux v1.5.0.
2. Run the staging update/outage/replay matrix.
3. Enroll and self-update the control panel.
4. Pilot one noncritical project, then Decrees and Trademarks.
5. Add agent self-update only after fleet stability.
6. Deploy the separate SSO project afterward; agent credentials remain independent from OIDC.