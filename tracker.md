# Project Tracker (composer)

## Part 1: Project Related
### Current Verified Snapshot:
- Composer is a Compose orchestrator plus outbound DLUX agent; v1.3.3 is tagged/published — in-progress work is v1.3.4 (VERSION bumped). NEVER append to a tagged version — check `git tag` first.
- Code lives under `composer/`; entrypoints are `python -m composer`, `python composer/main.py`, and `start.sh`/`start.ps1`.
- Update surface: `update`, `pull`, `update-self`, `agent-check`, `agent-update`, `agent-restart`, and `agent-off`; `-u` remains compact.
- `COMPOSER_VERSION` is the last deployer version; `agent-status.json` reports the resident `composer_version` separately.
- Runs are hangup-proof: `composer/session.py` guards SIGHUP (log redirect), Compose children use `start_new_session`, only Ctrl+C cancels; `run`/`log`/`logs` stay terminal-bound.
- Composer OWNS `start.sh`/`start.ps1` (`# composer-wrapper: N`, integer, NOT the release version); dlux `scaffold_templates` are mirrors. Bump both files + `wrappers-history.json` + the dlux mirror together — see docs/RELEASING.md step 3.

### Current Project Adopted Standards:
- Use argparse and existing mixin/helper boundaries; resolve active Compose files early.
- Route Compose operations through the shared command helpers.
- Keep runtime metadata/environment in generated overrides.
- Agent control traffic is outbound HTTPS; localhost HTTP is development-only.
- Preserve deployment originals under `.xpose/` before guarded rewrites.

### Adopted Standards' rules and policies:
- Secrets are plaintext-only: `.env` -> `secrets/.env` -> `.secrets/.env`.
- Destructive flags require typed confirmation unless `-y` or `COMPOSER_ASSUME_YES=1`; non-TTY fails closed.
- `update` deploys, `pull` only downloads, `update-self` updates Composer, and `-u` is the sole compact update argument.
- Never modify a tagged changelog entry; append changes to the next unreleased version.
- Preserve user changes and move generated caches under `.xpose/`.

### Cross-Cutting Audits if any:
- 2026-07-24 security audit covered protocol, bridge, Docker boundary, registry, subprocess, supply chain, and releases.
- v1.2.7 pins the control origin, blocks credentialed redirects, and validates strict recovery booleans plus full Authorization redaction.

### Current Project's Unsolved Known Bugs:
- A compromised networked agent can abuse the POST-enabled Docker proxy with host-root-equivalent impact.
- Shared-volume temp paths permit symlink clobbering; spool, output, event, and command queues need effective bounds.
- Version gating fails open on missing labels; mutable refs and Windows `shell=True` reconstruction widen risk.

### Incomplete Tasks:
- **Priority 1:**
  - [~] Executor hardening (design in `docs/executor-hardening.md`): typed `composer-executor` holds docker.sock, agent loses `DOCKER_HOST`, `docker-socket-proxy` → POST=0/EXEC=0. Socket surface is only restart+recovery (image_update stays file-triggered → executor watcher; backup is DLUX-side). Standalone release ahead of remote-settings; coordinated dlux scaffold + `enable-executor` migration.
    - [x] Slice 1 (DONE, additive/inert): `executor_protocol.py` (validate/result/framing, protocol_version handshake), `executor.py` (unix-socket server: 0660 bind, SO_PEERCRED auth, one-op lease, bounded), `executor_ops.py` (restart/recovery mirror `_run_child`), `EXECUTOR_SERVICE`, `composer executor` command; `PROTECTED_RESTART_SERVICES` moved to `service_selection` (+composer-executor). +28 tests.
    - [x] Slice 2a/b (DONE, dormant until exec-mode set): `executor_client.py` (agent→socket, gated on `COMPOSER_EXECUTOR_SOCKET`, legacy fallback); `_run_child` delegates restart/recovery to executor when configured, re-validated executor-side; `check`/checkup is topology-aware (hardened/legacy/incomplete) with helpful hints + backwards compat. +12 tests (suite 190).
    - [x] Slice 2c (DONE): executor runs the trigger-watched update loop (`_run_watch_loop`, shares `op_lease`; availability disabled — a Docker READ stays with the agent). Async handoff via the existing `<trigger>.ack`: agent `_observe_executor_update` reads the ack (no Docker), reports completion, de-dups by `last_reported_ack_token` (seeds on first sight). In exec-mode the agent performs NO Docker writes (image-update observed, restart/recovery delegated). +8 tests.
    - [x] Slice 3 (DONE): `_resident_pair_scope()` discovers services via `compose config --services`; agent-update/restart/off now target composer-agent + composer-executor together (pull/up/monitored, restart_services, down_services) so the pair can't drift on self-update; agent-only fallback for legacy stacks. agent-check unchanged (shared image covers both). +6 tests.
    - [x] Slice 4 (DONE): hardening topology + migration + check-fix wiring.
      - `_hardened_stack()` generator: read-only proxy (POST=0/EXEC=0), composer-executor holds real docker.sock rw + not network-facing, agent KEEPS DOCKER_HOST→read-only proxy (reads) + COMPOSER_EXECUTOR_SOCKET + shared `composer_exec_sock` volume. NOTE: agent keeps DOCKER_HOST (proxy read-only) — security is POST/EXEC=0, not removing access (design doc corrected).
      - `enable_executor` migration (shared `_apply_stack_migration` with enable_agent): transform marked agent block → hardened, add volume anchor, backup(.xpose)+`compose config` validate+atomic write; idempotent; refuses legacy/unknown. Generated compose passes real `docker compose config`.
      - CLI `composer enable-executor` (dry-run default) + launcher dispatch.
      - `check --fix` runs `enable-executor` (`_maybe_fix`); agent-without-executor topology is now WARN (non-blocking) so the hint prints. +15 tests (5 gen + 9 migration + 1 check-fix), suite 218.
    - [x] Slice 5 (DONE, in dlux repo): scaffold generates the hardened topology (executor + read-only proxy + agent delegates), stack_contract + topology tests updated, generated compose passes real `docker compose config`, dlux suite 1040 GREEN. Executor hardening now COMPLETE end-to-end (composer + scaffold). Release-coordinated: dlux scaffold ships AFTER composer v1.3.0.
  - [ ] Run `./start.sh update-self` from each deployment root once v1.3.4 is tagged.
  - [ ] After v1.3.4 is tagged and pulled, run `./start.sh check` in each deployment to confirm the wrapper contract reports `current` from the real image.
  - [ ] Live verify on a real deployment: after `./start.sh update`, DLUX's image-update indicator clears within ~30s (agent must see the new local digest through the read-only proxy).
  - [ ] Live verify detach: close the terminal mid-`update` (native + `start.sh`), confirm the deploy finishes and `composer-detached.log` fills; Ctrl+C still exits 130.
  - [ ] Live verify `stop -v` TTY/non-TTY behavior and `log -F`.
  - [ ] Pilot enrollment -> backup -> maintenance -> deploy -> DLUX finalization -> central replay.
  - [ ] Verify cancellation, outage replay, revocation, safe restart, and backup through docker-socket-proxy.
  - [ ] Verify plaintext resolution and `python -m composer` failure diagnostics against a real project.
- **Priority 2:**
  - [ ] Derive restart safety from DLUX `org.dlux.restart=safe|protected` labels instead of hardcoded names.
  - [ ] Add shared `check` drift checks for raw Docker socket mounts and the `dlux_runtime` rw/ro split.
  - [ ] Run pending dependency/container CVE scanners and image smoke tests when tools/images are available.
- **Completed Recently:**
  - [x] v1.3.4: wrapper version contract — `# composer-wrapper: N` marker, `composer/wrappers.py`, reference copies + `wrappers-history.json` baked at `/app/wrappers/`, `_check_wrappers` in `check`, atomic self-replacing `--fix`. Composer owns the wrappers; dlux templates are pinned mirrors.
  - [x] v1.3.4: wrappers attach `-i` unconditionally (`-t` only with a terminal) so piped stdin reaches the container, plus a bash 3.2 empty-array guard on `secret_flags`.
  - [x] v1.3.3: pull bar names only in-flight images (+cached layer count) and 🔵 marks services being replaced.
  - [x] v1.3.3: aggregated image-pull progress bar (`progress.py`) + wrappers stop pulling silently behind a discarded stderr.
  - [x] v1.3.3: availability self-corrects after an out-of-band deploy (30s local-digest probe + forced re-check on executor ack).
  - [x] v1.3.3: terminal-hangup survival (`session.py` guard + detach log, detached Compose children, relayed Ctrl+C, `start.sh` `trap '' HUP`).
  - [x] v1.3.1: `agent-check` falls back to compose-file image discovery (agent block `--check-image`/`WEB_IMAGE`, `${VAR:-default}` resolution, `-f` scoping).
  - [x] v1.2.9: normalize update/pull/self/agent commands and add typed `agent-check` image availability.
  - [x] v1.2.8: coalesce pending snapshots to the latest state and collapse existing snapshot backlogs during store initialization.
  - [x] v1.2.7: pin control origin, reject redirects, harden typed/redacted relay data, block mixed topologies, and safely reconcile legacy proxy routes.
  - [x] v1.2.6: targeted obsolete-service cleanup with candidate validation, original archive, and named-volume postflight.

### One-line info about last verified Tests:
- Verified 2026-08-04: 303/303 tests. v1.3.4 wrapper contract: `tests/test_wrappers.py` (17) — marker/history/shared-version manifest guards plus current/stale/modified/unversioned/ahead/missing classification and atomic install (inode swap, mode repair, no staging leftovers). LIVE through the built image: a pre-marker project reported unversioned, `./start.sh check --fix -y` replaced start.sh *while executing it* (inode 9438997→9439015, exit 0, mode 755 kept), re-check GREEN, backup kept.
- Verified 2026-08-04: 286/286 tests. v1.3.4 wrapper stdin: `test_wrapper_secrets.py` +2 (no-tty argv has `-i` and no `-t`; pty argv has both) — the pty case first exposed the bash 3.2 `set -u` empty-array abort on a secretless project. Live-verified against archive: piped `yes` now arrives (`ISATTY=False GOT='yes\n'`) and the pty path still reads interactively.
- Verified 2026-08-03: 284/284 tests. v1.3.3 pull UX: `test_progress.py` scope tests (names in-flight images only, abbreviates >2, cached count, monotonic/no early 100%) + `test_service_icons.py` (6: distinct 🔵 updating icon, scoped/excluded marking, health resolves it).
- Verified 2026-08-03: 272/272 tests. v1.3.3 progress: `tests/test_progress.py` (16) — docker/compose pull parsing, monotonic fraction, byte totals, service-only pulls, non-pull lines ignored, in-place vs detached rendering; `update-self` streaming + failure exit re-covered in `test_agent_commands.py`.
- Dependency/container CVE scanning remains pending because the scanners are unavailable locally.
### One-line info about last time edited Docs:
- 2026-08-04 README "wrapper versioning" (ownership, marker, status table, `--fix` semantics) + docs/RELEASING.md step 3 (bump marker, history, dlux mirror together).
- 2026-08-04 README "the surface": `composer run` TTY auto-detection now documents the piped-stdin path and the nested-pty type-ahead caveat (prefer `--noinput`).
- 2026-08-02 README: new "surviving the terminal" section (detach behavior, `COMPOSER_DETACH_LOG`, Ctrl+C, terminal-bound commands).

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
- Executor hardening design: `docs/executor-hardening.md`. Fleet-mgmt plan (dlux repo): `panelPLAN.md` §2.3.
