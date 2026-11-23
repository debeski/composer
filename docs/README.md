# Composer documentation

Start with the [README](../README.md) for what Composer is and a quick start.

## Using Composer

- [Commands](commands.md): every command and flag, the confirmation guard, and closing the terminal mid-run.
- [Deploying](deploying.md): the wrappers and their versioning, secrets, post-start tasks and the migrator contract, deploy status, how a run behaves.
- [Manual first-deploy setup](first-deploy.md): `--skip-config`, leaving DjangoLux's setup wizard available.
- [Updates and channels](updates.md): image updates and the version gate, updating Composer, inline DjangoLux updates and rollback, image availability, release manifests, both release channels.
- [`composer check`](check.md): what `check` and `check --fix` do, and one entry per finding.

## The stack

- [Stack migrations](stack-migrations.md): stack schema stamps, each schema step, topology migrations, retired wiring `check --fix` normalizes.
- [The generated proxy](proxy.md): stored files, cross-site requests (Fetch Metadata), trusted front proxies.
- [Maintenance page](maintenance-page.md): the bundled page, a project's own page, the schema-3 migration.
- [App data permissions](app-permissions.md): non-root DjangoLux images, the group-access grant and how to undo it.
- [Private registry login](private-registry.md): reading and pulling a private `WEB_IMAGE`.
- [SMTP relay settings](smtp-relay-settings.md): what the Email step sees, the relay's certificate check, the relay's version.

## The resident pair

- [The resident pair](resident-pair.md): `composer-agent` and `composer-executor` at runtime, `agent watch`, lifecycle commands.
- [Executor hardening](executor-hardening.md): the security model: who holds Docker authority, the socket operations, capabilities, the read-only proxy.
- [Agent Protocol v1](agent-protocol-v1.md): the state files DjangoLux and the agent exchange, the Operations card's operations, the control-plane protocol.
- [The egress relay](relay.md): named outbound operations the application asks the agent to make, and sealed secrets.

## Maintaining Composer

- [Releasing](RELEASING.md): cutting a release, stable and beta channels, CI.
- [CHANGELOG](../CHANGELOG.md): what changed in each release.
