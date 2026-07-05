# Project Tracker (composer)

## Part 1: Project Related
### Current Verified Snapshot:
- Composer is a Docker Compose orchestration tool for plaintext env secrets, health checks, and post-start hooks.
- (v1.1.5) SOPS/AGE removed entirely: secrets resolve ONLY from a plaintext env file (`.env`→`secrets/.env`→`.secrets/.env`). Dropped from `secrets_manager.py`: `decrypt/encrypt_secrets_raw()`, `encrypted_secrets_path()`, `ENCRYPTED_CANDIDATES`, `parse_dotenv_text()`, `enc_file`; `resolve_secrets()` takes NO args. CLI dropped `-k`/`--encrypt`/`--decrypt`/`-i`/`-o`/`key_positional`. `Dockerfile` no longer installs `age`/`sops`; `entrypoint.sh` = `exec python -m composer "$@"` only; `smoke-test.sh` crypto checks removed. `secrets_source` is now a plaintext path string; render always shows `🔓 PLAINTEXT <path>`.
- The modular implementation now lives under `composer/` with entrypoints `python -m composer` and `python composer/main.py`.
- `composer/main.py` delegates to `DockerComposeLauncher` in `composer/launcher.py`.
- Mixins split behavior into CLI parsing, config extraction, Docker Compose operations, health monitoring, rendering, secrets handling, subprocess running, post-start hooks, output utilities, version loading, deploy-status writing (`status_writer.py`), and the preflight version gate (`version_gate.py`). Subcommands intercepted before flat parse: `run` (exec/run) and `watch` (resident updater → `watcher.py:run_watch()`).
- `watch` subcommand (v1.1.5): `composer watch --trigger-file PATH [--interval N][--status-file PATH][-f][-d][--once]` watches a trigger file; on a new token (or file mtime) shells `python -m composer -uo` in a child (keeps one-shot semantics), writes `<trigger>.ack` (atomic, token+exit+ts) so a request runs once + survives restart. Child owns `COMPOSER_STATUS_FILE`; watcher owns ack. Trigger-driven, NOT registry-polling. `cli.parse_watch_args()`.
- `composer/version.py` reads the repo-level `VERSION` file; the current repo version is `1.1.7` (`v1.0.0`→`v1.1.6` all tagged+published; `v1.1.7` in-progress, NOT yet tagged). ALWAYS re-run `git tag` before editing CHANGELOG — releases get tagged mid-session; never edit a tagged version's entry.
- (v1.1.7) `watch --log-file` console: child appends clean ANSI-free lines to `COMPOSER_LOG_FILE` (watcher sets it; default `deploy-log.txt` beside `--status-file`, truncated per run) via `OutputUtilsMixin.append_console()` in `emit_progress`/`emit_status` + `write_status()` phase/error markers. Feeds a proxy-served live progress page during the recreate window. Panel/docker-logs unchanged; opt-in.
- (v1.1.6) `watch` registry availability check: `--check-image IMAGE`(repeatable) + `--availability-file PATH` [+`--check-interval` default 3600] → `composer/registry.py` reads remote tag digest (registry v2 Bearer flow; `COMPOSER_REGISTRY_TOKEN` for private) vs local `docker image inspect RepoDigests`, writes `{available,checked_at,images:[{image,remote_digest,local_digest,update_available}]}`. Unreadable remote=unknown (no false positive). Opt-in; runs on start/interval/after-update. dlux reads this file to offer "app update available" (registry-driven trigger, replacing the old PyPI-inline-unsafe trigger).
- Update/restart flags (v1.1.3): `-u`/`--update [svc]` pulls then recreates immediately (scoped pull+`up -d <svc>` when a service is named; native Compose recreate-if-changed, no force, dependents not cascaded). `-uo`/`--update-only [svc]` = the old `-u` (pull then full `up -d`, no scoped recreate). `-r`/`--restart [svc]` runs `docker compose restart [svc]` via a dedicated launcher branch (resolve secrets → restart → health; no post-start hooks), preserving containers/env. Launcher fields `up_service`/`restart_mode`/`restart_service`; `DockerComposeMixin.restart_containers()`; render restart-aware. Wrapper `start.sh`/`start.ps1` self-update only when `--update` is the SOLE arg, so `--update <svc>` passes through.
- `run` subcommand (v1.1.3): `composer run [-m][-s][-F][-f FILE][-d] <service> <command...>` → `docker compose exec <svc> <cmd...>` (or `run --rm` with `-F`). `-m`=prepend `python manage.py`, `-s`=`sh -c` wrap, TTY auto (`-T` when non-interactive). Intercepted as `argv[1]=="run"` BEFORE flat `parse_args()` (avoids `key_positional` clash) → `handle_run()` → `exec_in_service()`; new `SubprocessRunnerMixin.run_command_interactive()` (inherited stdio) + `DockerComposeMixin.resolve_compose_cli()` (one-shot plugin/legacy probe). Compose-file resolution extracted to `launcher.resolve_active_compose_files()` (reused by both paths). No secret resolution (exec uses container's baked env; Compose only warns on unset vars). `cli.parse_run_args()`; documented in main `--help` epilog + `composer run --help`.
- Secrets flow is plaintext-only: `SecretsMixin.resolve_secrets()` tries `.env`→`secrets/.env`→`.secrets/.env`, using the first that satisfies `ConfigMixin.required_compose_vars()`; no encrypted fallback (removed v1.1.5). On failure it reports the missing vars from the first incomplete candidate, or "No secrets source found".
- `required_compose_vars()` = `${VAR}` refs (skips `:-`/`-`/`:+`/`+` defaults) MINUS YAML-comment refs (strips full-line + trailing ` #…`, keeps mid-token `url#frag`) MINUS `$$`-escaped shell vars MINUS vars the compose supplies itself. `_compose_env_keys()` collects `environment:` keys assigned a concrete literal value (mapping + list form); bare `- KEY` pass-throughs and interpolated `KEY: ${KEY}` values stay required. `-sd`/`--skip-decrypt` removed entirely. `launcher.secrets_source` drives the UI label/flag.
- `-d`/`--dev` = compose.dev.yml two-file override AND forces debug on: `sync_runtime_compose_override()` injects `DEBUG: "True"` + `DEBUG_STATUS: "True"` into every service (override applied last, wins over compose), `build_compose_env()` exports `DEBUG`/`DEBUG_STATUS=True` (for `${...}` interpolation), launcher forces `debug_mode` UI flag, and `DEBUG`+`DEBUG_STATUS`+`NGINX_PORT` are in `resolve_secrets()`'s injected set so they never read as missing required vars.
- Tag-driven release via `.github/workflows/release.yml` (`v*` tags → verify tag==VERSION → build amd64 + `scripts/smoke-test.sh` gate → multi-arch buildx push `debeski/composer:<ver>` + `:latest` to Docker Hub → GitHub Release from CHANGELOG section). `.github/workflows/ci.yml` runs compileall + CLI smoke + amd64 build + smoke tests on main. Needs repo secrets `DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN`. See `docs/RELEASING.md`.
- `scripts/smoke-test.sh <image>` runtime-gates pushes: version==VERSION, `--help` flags (`--down`/`--purge`/`--volumes`/`--update`/`--update-only`/`--restart`/`--build`), `run` subcommand + `run --help`, docker/compose runnable. (Crypto checks removed v1.1.5.)
- Runtime compose override injection now exports `COMPOSER_VERSION` in `composer/docker_compose_manager.py`.
- Wrapper scripts now target `debeski/composer:latest`.
- Dockerfile copies `composer/` to `/app/composer`, sets `WORKDIR /app`, exports `PYTHONPATH=/app`, copies `VERSION`, and uses `/app/entrypoint.sh`.
- `entrypoint.sh` is now just `exec python -m composer "$@"` (all crypto/keygen routes removed v1.1.5).
- `start.sh` now targets `debeski/composer:latest` and only passes `-it` when run from an interactive terminal.
- `composer/__pycache__/` is generated by local Python checks and should not be committed.
- `-p`/`--purge` is a `--down` child: `down --rmi local -v --remove-orphans` + `docker builder prune -f` (dangling BuildKit cache); implies `-v`.

### Current Project Adopted Standards:
- Use argparse for CLI argument handling.
- Docker Compose operations go through `run_docker_compose()` / `run_docker_compose_streaming()`.
- Render-based UI uses section states: idle, running, ok, error.
- Service health monitoring uses `docker compose ps --format json`; services with no healthcheck count as ready once running.
- Active compose files must be resolved early and stored in `self.active_compose_files`.
- Launcher-owned runtime metadata should be injected centrally, not repeated in project compose files.
- Keep work scoped to `composer/` and runtime wiring unless the user explicitly asks otherwise.

### Adopted Standards' rules and policies:
- Keep edits scoped and preserve existing launcher behavior unless fixing verified breakage.
- Prefer standard-library subprocess/env handling over global environment mutation for secret material.
- Reuse existing helper/mixin boundaries instead of adding unrelated abstractions.
- Do not commit generated caches such as `__pycache__`.

### Cross-Cutting Audits if any:
- Prior audits (resolved): Windows `shell=True` argv → shell-safe string; compose fallback to `docker-compose` when `docker` exe missing; Dockerfile copies `composer/`, sets `WORKDIR /app`+`PYTHONPATH=/app`; no `__pycache__` committed. (SOPS/keygen/passphrase audit items obsolete after v1.1.5 crypto removal.)

### Current Project's Unsolved Known Bugs:
- No new code-level bugs verified in `composer/` after the current review/build pass.
- Manual Docker runtime validations are still pending because they require a compose fixture/project and Docker daemon behavior.

### Incomplete Tasks (Composer-as-updater project — cross-repo; backend DONE+tested):
- Live update-progress feature (proxy-served; all 3 phases DONE + tested):
  - [x] composer v1.1.7: `watch` writes clean console `deploy-log.txt` (COMPOSER_LOG_FILE / `--log-file`) via `append_console`. Tested.
  - [x] switch_pos v0.2.1: Caddyfile `/_update/status.json`+`/_update/log.txt` (above @maintenance, no-store); `.nginx/maintenance.html`→live progress page (bar+console, stale-ready guard, reload-in-place on ready, error on failed); composer pin →`v1.1.7`; `--interval`→5s. compose+Caddy validate.
  - [x] dlux v1.3.3: image-update modal polls proxy `data-deploy-status-url`/`-log-url` (survives web-down, reload on ready); `_begin_image_update` writes initial `deploy-status.json`=`preparing` (kills stale-ready). 45 tests + lifecycle pass; JS parses; migrations clean.
  - [ ] RELEASE/DEPLOY: build+push `debeski/composer:v1.1.7`; release dlux `v1.3.3` (inline update on VPS delivers updater.js); redeploy switch_pos (`compose up` — Caddyfile/maintenance.html bind-mounted, no image rebuild) then bump switch_pos image pin to django-lux≥1.3.3 on next build.
- Priority 1 (remaining — need a live stack / registry):
  - [ ] LIVE verification (needs a real stack + Docker daemon): end-to-end image update — dlux queue → backup → maintenance → composer pull/gate/recreate/migrator → new dlux boot finalizes from deploy-status.json; confirm maintenance lifts correctly (reconcile clears on baked advance; finalize clears on gate-block/no-recreate). Browser-check the Updates UI image button/status.
  - [ ] Build/push `debeski/composer:1.1.5` (compose pins it) and confirm slimmer image runs the smoke test. (User is pushing composer 1.1.5 now.)
  - [ ] Decrees redeploy note: `down` + `up -d` from project root (network topology change; ${PWD} mount); named volumes preserved. composer-updater/socket-proxy stay idle until decrees pins django-lux>=1.3.1.
- Completed (this session, all repos): composer v1.1.5 (SOPS/AGE removal, status writer, version gate, watch); decrees v2.1.6 (3+1 networks, updater services, baked label); dlux v1.3.1 (image-update backend + Options UI).
- Design notes (verified):
  - dlux `reconcile()`→`_reset_to_baked_image` already resets runtime→baked AND clears maintenance (service.py:341) on image swap; composer does NOT manage the runtime volume.
  - HAZARD avoided: `_recover_interrupted_run` (service.py:373-426) false-fails any active non-queued run on boot → image updates use a SEPARATE `DluxImageUpdate` path (no `active_run_token`), so that recovery never touches them.
- Priority 2 (verification, still pending):
  - [ ] Verify plaintext resolution against a real compose project (complete `.env` → ok; incomplete `.env` → clear missing-vars error).
  - [ ] Verify `python -m composer` startup with a service `build:` step; container reads `COMPOSER_VERSION`; failure output for exit-1 container and failing `post_start`.
  - [ ] Rebuild/push `debeski/composer:1.1.5` and confirm the slimmer image (no age/sops) still runs the smoke test.
- Completed Recently:
  - [x] (cross-repo) decrees `v2.1.6`: 3+1 network model (frontend/egress/internal/docker_proxy), `docker-socket-proxy` (least-priv) + `composer-updater` (`watch`, pinned 1.1.5, `${PWD}:${PWD}`, gate-wired) services; `docker compose config` validates clean. Dockerfile `LABEL org.decrees.dlux_baked_version` + release.yml build-arg (→1.2.10). CHANGELOG + config/VERSION→2.1.6.
  - [x] (cross-repo) dlux `v1.3.1`: SEPARATE lightweight image-update path — `DluxImageUpdate` model + migration 0008 (additive/inline-safe), `updater/image_update.py` (availability/queue/trigger/serialize), `UpdateService.tick_image_update/_begin/_finalize/_complete/_fail`, worker-loop wiring, `POST .../dlux-update/image/` view + state-endpoint fields. Reuses `_create_backup`/maintenance/check-interval. Does NOT touch DluxUpdateRun recovery. TESTED vs real DB (availability, queue guard, begin→trigger, finalize wait/ready/failed + maintenance) and existing 45 updater tests still pass; `makemigrations --check` clean. Manifest→1.3.1 + CHANGELOG. Frontend UI pending.
  - [x] (v1.1.5) `watch` resident-updater subcommand (`watcher.py`, `cli.parse_watch_args`, launcher `argv[1]=="watch"` intercept): trigger-file → child `-uo` → `<trigger>.ack`. Verified via stubbed subprocess: token detect (json/mtime/missing), process-once+ack, idempotent same-token, re-run new-token. README/CHANGELOG/smoke-test updated.
  - [x] (v1.1.5) Phase 1 status writer: `status_writer.py` `StatusWriterMixin.write_status()` (atomic JSON temp+replace, opt-in via `--status-file`/`COMPOSER_STATUS_FILE`); launcher writes starting/pulling/recreating/migrating/ready/failed (+ restart branch restarting/ready/failed). Payload: status, updated_at(UTC ISO), composer_version, compose_files, +gate fields. Verified via stubbed unit test.
  - [x] (v1.1.5) Phase 2 version gate: `version_gate.py` `VersionGateMixin.preflight_version_gate()` runs after pull (before recreate) on `-u`/`-uo`; reads target via `docker image inspect` label (`COMPOSER_VERSION_LABEL`, default `org.opencontainers.image.version`) + active via JSON `COMPOSER_ACTIVE_VERSION_FILE`+`COMPOSER_ACTIVE_VERSION_KEY`(default `version`). Blocks target<active unless `--force`; disabled if no active-version file; dep-free `parse_version`. Added `--force`+`--status-file` CLI. Verified: block/pass/force/disabled/no-label + status write all pass.
  - [x] (v1.1.5) Phase 2 decrees hook: `project-decrees/Dockerfile` adds `ARG DLUX_BAKED_VERSION` + `LABEL org.decrees.dlux_baked_version`; `release.yml` derives it from pinned `django-lux[updater]==1.2.10` in requirements.txt and passes `build-args`. Extraction verified →`1.2.10`.
  - [x] (v1.1.5) Removed SOPS/AGE entirely (secrets_manager/cli/launcher/rendering, Dockerfile age+sops, entrypoint routes, smoke-test crypto, README); plaintext-only secrets; VERSION→1.1.5, `## v1.1.5` CHANGELOG (v1.1.4 already tagged/published). Verified: `compileall` OK, no leftover crypto refs, `--version`=1.1.5, `--help` clean.
  - [x] (v1.1.2-1.1.4) `run` subcommand, `-u`/`-uo`/`-r` update/restart flags, `required_compose_vars()` YAML-comment/`$$`/literal-env handling, plaintext-first default (`-sd` removed), `-d`/`--dev` forces `DEBUG=True`. (See snapshot.)
  - [x] (v1.0.x) Composer rebrand, `-p`/`--purge`, tag-driven release + CI workflows + `docs/RELEASING.md`, `scripts/smoke-test.sh` gate.

### One-line info about last verified Tests:
- Verified 2026-07-04 (v1.1.5): `compileall composer` OK; no leftover crypto refs; `--version`→1.1.5; `--help` has `--force`/`--status-file`, no crypto flags. Stubbed unit tests: `parse_version`/`_version_lt`, `read_active_version`(+dotted key/broken json), gate block/pass/force/disabled/no-label, atomic status write w/ gate fields. Decrees baked-version grep→`1.2.10`. Docker image rebuild + live runtime path still pending.

### One-line info about last time edited Docs:
- Edited `README.md` (status file + version gate + new flags; SOPS/AGE removed), `CHANGELOG.md` (`## v1.1.5`: status writer + version gate + SOPS/AGE removal), `tracker.md` on 2026-07-04; `VERSION`→`1.1.5`.

## Part 2: Global
### Global Standard Helpers, Shortcuts, Info, etc.:
- `run_docker_compose(args)` wraps Compose commands and falls back from `docker compose` to `docker-compose`.
- `run_docker_compose_streaming(args)` streams progress while capturing output for failures.
- `collect_service_diagnostics()` gathers `docker compose ps --all` plus targeted failed-service logs.
- `read_composer_version()` reads the bundled/repo `VERSION` file and falls back to `0.0.0`.
- `sync_runtime_compose_override()` writes a temporary compose override to inject `COMPOSER_VERSION`.

### Global Rulesets:
- Down mode bypasses normal startup flow: no secrets, no health checks, no post-start hooks.
- If compose config depends on env vars not yet loaded, allow initial service discovery to fail and retry after secrets load.
- If launcher-owned values should reach all containers, prefer generating a temporary compose override.
- Preserve existing user/worktree changes; do not revert unrelated modified files.
- Re-read `tracker.md` at the start of every turn and update it after meaningful project state changes.

### Agent Handoff Rules:
- `start.py` is currently deleted in the worktree as part of the rename/migration state; do not restore it unless requested.
- `composer/` is currently untracked as the new modular implementation.
- `start.sh` and `start.ps1` now launch `debeski/composer:latest`.
- Avoid bare `print()` calls during interactive panel mode unless redraw anchoring accounts for them.
- Re-run syntax checks after edits: `python -m compileall composer`.
- Remove `composer/__pycache__/` before handoff if local checks generated it.

### References and Links:
- Docker Compose CLI reference: https://docs.docker.com/engine/reference/commandline/
