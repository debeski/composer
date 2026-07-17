# Project Tracker (composer)

## Part 1: Project Related
### Current Verified Snapshot:
- Composer is a Docker Compose orchestration tool for plaintext env secrets, health checks, post-start hooks, status files, and resident image updates.
- Current repo version is unreleased `1.1.11`; `v1.1.10` is the latest local tag.
- Implementation lives under `composer/`; entrypoints are `python -m composer`, `python composer/main.py`, and wrapper scripts `start.sh`/`start.ps1`.
- `start.sh` targets `debeski/composer:latest`; only exact sole `--update` self-updates the Composer tool image, while `-u`/`-uo`/`-r` pass through.
- `watch` shells `python -m composer -u`, guarantees token-matched terminal failure + ack, writes deploy log/registry availability, and defaults `COMPOSER_EXCLUDE_SERVICES=composer-updater`.

### Current Project Adopted Standards:
- Use argparse for CLI handling; intercept `run` and `watch` before flat parse.
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
- Prior audits resolved: Windows shell-safe fallback, compose fallback, Dockerfile `WORKDIR`/`PYTHONPATH`, no committed `__pycache__`.

### Current Project's Unsolved Known Bugs:
- No unresolved code-level project-mount permission or missing terminal watcher-status bug remains after v1.1.11 regression tests.
- Manual Docker runtime validations still require a real compose project and Docker daemon behavior.

### Incomplete Tasks:
- **Priority 1:**
  - [ ] Publish/tag Composer 1.1.11, then pull and recreate resident `composer-updater` services before retrying failed image updates.
  - [ ] Live verify image update path: dlux queue -> backup -> maintenance -> composer pull/gate/recreate/migrator -> new dlux boot finalizes.
  - [ ] Verify plaintext resolution against a real compose project.
  - [ ] Verify `python -m composer` startup with `build:`, `COMPOSER_VERSION`, exit-1 diagnostics, and failing `post_start`.
- **Priority 2:**
  - [ ] Rebuild/push pending Composer images as needed and confirm runtime smoke tests.
  - [ ] Decrees redeploy note: `down` + `up -d` from project root; named volumes preserved.
- **Completed Recently:**
  - [x] v1.1.11: Runtime overrides use verified writable system temp storage; watcher non-zero/spawn failures always publish token-matched `failed` status before ack and append a console failure.
  - [x] v1.1.10: watcher self-exclusion. `COMPOSER_EXCLUDE_SERVICES` filters discovery/runtime override/bulk pull/version-gate images/bulk `up -d`/health/diagnostics; `watch` defaults to excluding `composer-updater` (override `COMPOSER_WATCH_SELF_SERVICE`) so resident updater does not recreate itself after v1.1.9 `watch`→`-u`.
  - [x] v1.1.9: changed `watch` child to `-u`; made `-uo [service]` pull-only with status `pulled` and no gate/compose/health/post-start.
  - [x] composer v1.1.7 watch console log/status integration; switch_pos live progress proxy; dlux image update modal/card redesign.
  - [x] composer v1.1.5 plaintext-only secrets, status writer, version gate, trigger-driven `watch`, SOPS/AGE removal.
  - [x] v1.1.3-v1.1.4 `run` subcommand, `-u` scoped update/recreate, `-uo` legacy full startup update, `-r` restart branch.

### One-line info about last verified Tests:
- Verified 2026-07-17: 5/5 `unittest` regressions pass for read-only project placement, tempfile diagnostics, terminal watcher status/ack/log, detailed errors, and spawn failure; diff check clean.
- Verified 2026-07-17: AST syntax parse for `composer/*.py`; stubbed exclusion assertions for bulk `pull`/`up`/version-gate images + `watch` child env; `python3 -m composer --help`; `python3 -m composer watch --help`.

### One-line info about last time edited Docs:
- Edited `README.md`, `CHANGELOG.md` (`v1.1.11`), `VERSION`, and tracker on 2026-07-17 for mount-independent overrides and terminal watcher failure.

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
