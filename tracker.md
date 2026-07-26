# Project Tracker (composer)

## Part 1: Project Related
### Current Verified Snapshot:
- Composer is a Compose orchestrator plus outbound DLUX agent; current source is unreleased v1.2.7 and latest tag is v1.2.6.
- Code lives under `composer/`; entrypoints are `python -m composer`, `python composer/main.py`, and `start.sh`/`start.ps1`.
- Early subcommands: `run`, `restart`, `update`, `stop`/`down`, `log`/`logs`, `check`, `watch`, `agent`, and `enable-agent`.
- `COMPOSER_VERSION` is the last deployer version; `agent-status.json` reports the resident `composer_version` separately.
- v1.2.7 hardens agent/registry auth and `check --fix`; template-backed Nginx is restarted so rendered proxy config is refreshed.

### Current Project Adopted Standards:
- Use argparse and existing mixin/helper boundaries; resolve active Compose files early.
- Route Compose operations through the shared command helpers.
- Keep runtime metadata/environment in generated overrides.
- Agent control traffic is outbound HTTPS; localhost HTTP is development-only.
- Preserve deployment originals under `.xpose/` before guarded rewrites.

### Adopted Standards' rules and policies:
- Secrets are plaintext-only: `.env` -> `secrets/.env` -> `.secrets/.env`.
- Destructive flags require typed confirmation unless `-y` or `COMPOSER_ASSUME_YES=1`; non-TTY fails closed.
- `-u` pulls and deploys; `-uo` is pull-only; exclusions apply to unscoped restarts.
- Never modify a tagged changelog entry; append changes to the next unreleased version.
- Preserve user changes and move generated caches under `.xpose/`.

### Cross-Cutting Audits if any:
- 2026-07-24 security audit covered protocol, bridge, Docker boundary, registry, subprocess, supply chain, and releases.
- v1.2.7 pins the control origin, blocks credentialed redirects, and validates strict recovery booleans plus full Authorization redaction.

### Current Project's Unsolved Known Bugs:
- A compromised networked agent can abuse the POST-enabled Docker proxy with host-root-equivalent impact.
- Shared-volume temp paths permit symlink clobbering; spool/output/queue sizes need effective bounds.
- Version gating fails open on missing labels; mutable refs and Windows `shell=True` reconstruction widen risk.

### Incomplete Tasks:
- **Priority 1:**
  - [ ] Publish Composer v1.2.7, then run `./start.sh --update` and `composer check --fix` from each deployment root.
  - [ ] Live verify `stop -v` TTY/non-TTY behavior and `log -F`.
  - [ ] Pilot enrollment -> backup -> maintenance -> deploy -> DLUX finalization -> central replay.
  - [ ] Verify cancellation, outage replay, revocation, safe restart, and backup through docker-socket-proxy.
  - [ ] Verify plaintext resolution and `python -m composer` failure diagnostics against a real project.
- **Priority 2:**
  - [ ] Derive restart safety from DLUX `org.dlux.restart=safe|protected` labels instead of hardcoded names.
  - [ ] Add shared `check` drift checks for raw Docker socket mounts and the `dlux_runtime` rw/ro split.
  - [ ] Run pending dependency/container CVE scanners and image smoke tests when tools/images are available.
- **Completed Recently:**
  - [x] v1.2.7: pin control origin, reject redirects, harden typed/redacted relay data, block mixed topologies, and safely reconcile legacy proxy routes.
  - [x] v1.2.6: targeted obsolete-service cleanup with candidate validation, original archive, and named-volume postflight.
  - [x] v1.2.5: guarded destructive actions plus `update`, `stop`, `log`, and `check` subcommands.
  - [x] v1.2.0: outbound typed agent, SQLite replay/deduplication, safe restart, DLUX relay, and guarded `enable-agent`.

### One-line info about last verified Tests:
- Verified 2026-07-26: 131/131 unittests; release image build/runtime smoke; live Nginx-template cleanup/restart/rendered-config postflight; Docker Hub digest lookup from the image; workflow YAML/version/changelog gates.
- Dependency/container CVE scanning remains pending because the scanners are unavailable locally.

### One-line info about last time edited Docs:
- Edited README, release guide, protocol, CLI help, and changelog on 2026-07-26 for the final v1.2.7 release candidate.

## Part 2: Global
### Global Standard Helpers, Shortcuts, Info, etc.:
- `run_docker_compose()` and its streaming variant wrap Compose; `read_composer_version()` reads `VERSION`.

### Global Rulesets:
- Down mode bypasses startup checks; retry discovery after secrets when interpolation blocks the initial pass.
- Preserve unrelated worktree changes; never delete files; keep `tracker.md` below 100 lines.

### Agent Handoff Rules:
- `start.py` is intentionally absent; do not restore it.
- Re-run syntax/tests after edits and move generated `__pycache__` directories under `.xpose/`.

### References and Links:
- Docker Compose CLI reference: https://docs.docker.com/engine/reference/commandline/
