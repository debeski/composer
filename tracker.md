# Project Tracker (composer)

## Part 1: Project Related
### Current Verified Snapshot:
- Composer is a Docker Compose orchestration tool for plaintext env secrets, health checks, post-start hooks, status files, and resident image updates.
- Current source is unreleased v1.2.2; latest tag is v1.2.1.
- Implementation lives under `composer/`; entrypoints are `python -m composer`, `python composer/main.py`, and wrapper scripts `start.sh`/`start.ps1`.
- `start.sh` targets `debeski/composer:latest`; only exact sole `--update` self-updates the Composer tool image, while `-u`/`-uo`/`-r` pass through.
- `agent` adds outbound HTTPS control and typed DLUX relay; `composer enable-agent` is the sole guarded/diffable legacy scaffold transformer.

### Current Project Adopted Standards:
- Use argparse for CLI handling; intercept `run`, `restart`, and `watch` before flat parse.
- Docker Compose operations go through `run_docker_compose()` / `run_docker_compose_streaming()`.
- Resolve active compose files early and store them in `self.active_compose_files`.
- Runtime metadata/env injection belongs in generated overrides, not repeated in project compose files.
- Keep edits scoped to existing mixin/helper boundaries.

### Adopted Standards' rules and policies:
- Secrets are plaintext-only (`.env` -> `secrets/.env` -> `.secrets/.env`); SOPS/AGE/keygen routes were removed in v1.1.5.
- `-d`/`--dev` adds `compose.dev.yml` and forces `DEBUG=True` / `DEBUG_STATUS=True`.
- `-p`/`--purge` is a `--down` child: compose down with local images, volumes, orphans, plus dangling builder cache prune.
- `-u` deploys after pull; `-uo`/`--update-only` is pull-only and exits after status `pulled`.
- Preserve user changes; do not commit generated caches such as `composer/__pycache__/`.
- Before editing root `CHANGELOG.md`, check tags and never modify a released version entry.

### Cross-Cutting Audits if any:
- 2026-07-24 security audit: protocol, local bridge, Docker boundary, registry, subprocess, supply chain, and release workflows reviewed with focused exploit probes.
- Prior audits resolved: compose fallback, Dockerfile `WORKDIR`/`PYTHONPATH`, no committed `__pycache__`.

### Current Project's Unsolved Known Bugs:
- Enrolled agents accept an unauthenticated shared-spool control URL rebind, exposing the existing bearer credential to an attacker-controlled HTTPS endpoint.
- The POST-enabled Docker socket proxy permits host-root-equivalent daemon operations if the networked agent is compromised.
- Control and registry clients preserve authorization across redirects; Bearer-style log redaction leaves the secret value visible.
- Predictable shared-volume temp files permit symlink clobbering; spool, registry response, subprocess output, and durable queues lack effective bounds.
- Version gating fails open on missing labels; mutable image/action references and Windows `shell=True` argument reconstruction widen execution risk.

### Incomplete Tasks:
- **Priority 1:**
  - [ ] Publish Composer v1.2.2 (`enable-agent` legacy-topology fix), then migrate resident updaters with `./start.sh --update` before `./start.sh enable-agent --apply`.
  - [ ] Pilot v1.2.0 end to end: enrollment -> DLUX backup -> maintenance -> Composer deploy -> DLUX finalization -> replayed central result.
  - [ ] Live verify cancellation, outage replay, revocation, safe restart, and data/full backup creation through docker-socket-proxy.
  - [ ] Verify plaintext resolution against a real compose project.
  - [ ] Verify `python -m composer` startup with `build:`, `COMPOSER_VERSION`, exit-1 diagnostics, and failing `post_start`.
- **Priority 2:**
  - [ ] Rebuild/push pending Composer images as needed and confirm runtime smoke tests.
  - [ ] Decrees redeploy note: `down` + `up -d` from project root; named volumes preserved.
- **Completed Recently:**
  - [x] v1.2.2: `enable-agent` carries the replaced updater's networks, `COMPOSER_VERSION_LABEL`, and `WEB_IMAGE` forward instead of deriving them from the Compose `name:`, so pre-1.5 scaffolds no longer emit undeclared `dlux_update_egress`/`<slug>_docker_proxy` refs; undeclared networks now fail by name pre-write.
  - [x] v1.2.0: Composer-owned `enable-agent` provides an exact dry-run diff, pre-write Compose validation, `.xpose` preservation, atomic replacement, and a one-cycle DLUX forwarding alias.
  - [x] v1.2.0: outbound `composer agent`, strict schema-v1 typed commands, SQLite credentials/commands/outbox, accepted-event execution gate, revocation re-enrollment, rotation replay, backup relay, operation IDs, redaction, safe restart, and `watch` compatibility.
  - [x] v1.1.15: moved restart to `composer restart [service]`; wrappers/runtime override now hand private secrets to the resident updater, with strict file fallback diagnostics.
  - [x] v1.1.14: resolve_secrets refuses unreadable/empty env candidates (parse_env_file raises; no vacuous success), stopping defaults-fallthrough deploys before pull/recreate; new tests/test_secrets.py. Widened decrees COMPOSER_EXCLUDE_SERVICES to exclude stateful svcs.
  - [x] v1.1.13: bounded URL-safe-base64 project manifest label decoding with raw JSON compatibility and unchanged malformed-metadata fallbacks.
  - [x] v1.1.12: one-pass remote label lookup publishes optional version + normalized schema-1 project release manifest without changing digest availability or deployment behavior.
  - [x] v1.1.11: Runtime overrides use verified writable system temp storage; watcher non-zero/spawn failures always publish token-matched `failed` status before ack and append a console failure.
  - [x] v1.1.10: watcher self-exclusion. `COMPOSER_EXCLUDE_SERVICES` filters discovery/runtime override/bulk pull/version-gate images/bulk `up -d`/health/diagnostics; `watch` defaults to excluding `composer-updater` (override `COMPOSER_WATCH_SELF_SERVICE`) so resident updater does not recreate itself after v1.1.9 `watch`→`-u`.
  - [x] v1.1.9: changed `watch` child to `-u`; made `-uo [service]` pull-only with status `pulled` and no gate/compose/health/post-start.
  - [x] composer v1.1.7 watch console log/status integration; switch_pos live progress proxy; dlux image update modal/card redesign.
  - [x] composer v1.1.5 plaintext-only secrets, status writer, version gate, trigger-driven `watch`, SOPS/AGE removal.
  - [x] v1.1.3-v1.1.4 `run` subcommand, `-u` scoped update/recreate, `-uo` legacy full startup update, `-r` restart branch.

### One-line info about last verified Tests:
- Verified 2026-07-24: 53/53 unittest and `git diff --check` pass; patched `enable-agent` output validated by a real `docker compose config` against the pre-1.5 switch_pos project.
- Security dependency/container CVE scanning remains pending because Bandit, Semgrep, pip-audit, Trivy, ShellCheck, and Hadolint are unavailable locally.

### One-line info about last time edited Docs:
- Edited `README.md`, `docs/agent-protocol-v1.md`, and `CHANGELOG.md` on 2026-07-24 for `enable-agent` legacy-topology carry-over.

## Part 2: Global
### Global Standard Helpers, Shortcuts, Info, etc.:
- `run_docker_compose(args)` wraps Compose commands and falls back from `docker compose` to `docker-compose`.
- `run_docker_compose_streaming(args)` streams progress while capturing output for failures.
- `collect_service_diagnostics()` gathers `docker compose ps --all` plus targeted failed-service logs.
- `read_composer_version()` reads the bundled/repo `VERSION` file and falls back to `0.0.0`.

### Global Rulesets:
- Down mode bypasses normal startup: no secrets, health checks, or post-start hooks.
- If compose config depends on env vars not yet loaded, allow initial service discovery to fail and retry after secrets load.
- Preserve existing user/worktree changes; do not revert unrelated modified files.
- Re-read `tracker.md` at the start of every turn and keep it under 100 lines.

### Agent Handoff Rules:
- `start.py` is deleted in the worktree as part of rename/migration state; do not restore unless requested.
- `composer/` is the modular implementation and may be untracked in some worktrees.
- Avoid bare `print()` during interactive panel mode unless redraw anchoring accounts for it.
- Re-run syntax checks after source edits; generated `composer/__pycache__/` must be moved under `.xpose/` rather than deleted.

### References and Links:
- Docker Compose CLI reference: https://docs.docker.com/engine/reference/commandline/
