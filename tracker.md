# Project Tracker (composer)

## Part 1: Project Related
### Current Verified Snapshot:
- **v1.5.2 (in development)**: `check` returns the repair preview + digest with its findings, so DjangoLux's card runs one operation; new `agent-update` replaces the resident pair through a detached helper that writes the run's ack after recreating both containers. Not released.
- **v1.5.1 (stable, released 2026-09-23)**: Operations phase 2 — `check-fix-apply` delegated to the executor as a typed `check_fix` op because both residents mount the project read-only; the executor re-checks the preview digest and starts a short-lived container that can write. `:latest` and `:beta` are on it.
- **v1.5.0 (stable)** (2026-09-23): promoted from 1.5.0b1 after live acceptance with DjangoLux 1.9.1b1. `composer/ops.py` answers the Operations card — one named operation per request, token-matched result, read-only `check` via `collect_checkup()`. `:latest` moves to 1.5.0; no retirements (§9 inventory carries to 1.6.0).
- **v1.4.1 (stable)** (2026-09-22): the 1.4.0 line's stable release. `v1.4.0` was tagged but NEVER published — its shallow test-job checkout left the beta-first gate with no tags, so the first stable X.Y.0 failed its own test; the tag was burned instead of being deleted and re-cut (it could have been — see Known Bugs), so 1.4.1 carries it plus `fetch-depth: 0` in both workflows. Carries channels, channel-aware resident checks, `check-policy.json` interval + check-now, `resident-commands`, `check --fix` exit 0, and the migration-applier recreate with secrets. No retirements (§9 postponed to 1.5.0).
- Composer **v1.3.14b2 is tagged and published** (2026-09-10): `:v1.3.14b2` + `:beta` (amd64+arm64), `:latest` still 1.3.13, `v1.3.14b1` untouched, GitHub release prerelease with `latest` still v1.3.13. Carries the `:beta` alias-read fix and the beta-first gate. `preflight_version_gate()` accepts only a `keep` verdict from `dlux_image_gate` for the older-image exception.
- Entrypoints: `python -m composer`, `python composer/main.py`, and Composer-owned `start.sh`/`start.ps1` wrappers.
- Post-start is label-owned; init-container stacks strip updater-era native/label hooks and `check --fix` normalizes compatible legacy forms.
- Nested agent/DLUX CLI is canonical: `agent ...`, `dlux ...`, `self update`, `executor ...`.
- Deploy/update runs survive SIGHUP; Compose children use their own session and Ctrl+C still cancels.

### Current Project Adopted Standards:
- Use argparse and existing mixin/helper boundaries; resolve active Compose files early.
- Route Compose operations through the shared command helpers.
- Keep runtime metadata/environment in generated overrides.
- Agent control traffic is outbound HTTPS; localhost HTTP is development-only.
- Preserve deployment originals under `.xclude/` before guarded rewrites.

### Adopted Standards' rules and policies:
- Secrets are plaintext-only: `.env` -> `secrets/.env` -> `.secrets/.env`.
- Destructive flags require typed confirmation unless `-y` or `COMPOSER_ASSUME_YES=1`; non-TTY fails closed.
- `update` deploys, `pull` only downloads, `self update` updates Composer, and `-u` is the sole compact update argument.
- Never modify a tagged changelog entry; append changes to the next unreleased version.
- Preserve user changes and move generated caches under `.xclude/`.

### Cross-Cutting Audits if any:
- 2026-07-24 security audit covered protocol, bridge, Docker boundary, registry, subprocess, supply chain, and releases.
- v1.2.7 pins the control origin, blocks credentialed redirects, and validates strict recovery booleans plus full Authorization redaction.

### Current Project's Unsolved Known Bugs:
- Docker Hub `:beta` points at the withdrawn `1.6.0b1` image. A stable release never moves `:beta` backwards, so it stays there until the tag is deleted or repointed in Docker Hub by hand.
- A stable `vX.Y.0` tag fails its own unit tests unless the job checks out tags (fixed in 1.4.1 with `fetch-depth: 0`). `v1.4.0` was burned needlessly: nothing had been published, and the owner's admin bypass can delete a tag (`git push origin :refs/tags/vX.Y.Z`), so it should have been re-cut as 1.4.0. Do that next time a release fails before publication; only a published version is unrecoverable.
- A compromised networked agent can abuse the POST-enabled Docker proxy with host-root-equivalent impact.
- Shared-volume temp paths permit symlink clobbering; spool, output, event, and command queues need effective bounds.
- Version gating fails open on missing labels; mutable refs and Windows `shell=True` reconstruction widen risk.

### Incomplete Tasks:
- **Priority 1:**
  - [ ] TAG THE REHEARSAL: `v1.3.14b1` must be published BEFORE Dlux `v1.8.14b1` — that manifest requires `>=1.3.14b1`. Nothing pushed yet. Then verify `:beta` and `:v1.3.14b1` both appear and `:latest` did NOT move.
  - [ ] `release_channels_plan.md` remaining: the shared reference-stack acceptance harness (§7), published-artifact acceptance as a dependent job (§3.8), promotion serialization/alias comparison under concurrency (§3.6), and a Composer 1.4.0 retirement inventory (§9 — none exists yet). 1.9.0b1/1.4.0b1 stay the first feature betas.
  - [ ] Live verify the hardened inline update on a real stack: panel-triggered apply, agent stages, executor swaps, and DjangoLux reports the new version.
  - [ ] After publishing v1.3.6, run `./start.sh check --fix -y` on project-archive and confirm the missing label is installed with a `.xclude/` backup.
  - [ ] Live verify full startup via the published wrapper/image: `-d`, `-d -mm`, and `-d -nm`; each must run one migrator and return its failure status.
  - [ ] Run `./start.sh self update` from each deployment root once v1.3.12 is tagged.
  - [ ] Live verify on a real deployment: after `./start.sh update`, DLUX's image-update indicator clears within ~30s (agent must see the new local digest through the read-only proxy).
  - [ ] Live verify detach: close the terminal mid-`update` (native + `start.sh`), confirm the deploy finishes and `composer-detached.log` fills; Ctrl+C still exits 130.
- **Priority 2:**
  - [ ] Derive restart safety from DLUX `org.dlux.restart=safe|protected` labels instead of hardcoded names.
  - [ ] Add shared `check` drift checks for raw Docker socket mounts and the `dlux_runtime` rw/ro split.
  - [ ] Drop pip/setuptools from the image AFTER the `pypi-attestations` install layer (it is now the only pip dependency; +95MB, 347->442MB) - clears 3 fixable HIGH from pip's vendor tree.
  - [ ] Add `provenance: mode=max` + `sbom: true` to the release build-push step (Scout attestation policy).
- **Completed Recently:**
  - [x] 2026-09-19: manual image check publication, resolved Compose image/path discovery, `--no-publish`, publication failure reporting, and regression tests.
  - [x] Beta-first gate (2026-09-10): `validate_beta_first()` in `composer.release_tag` refuses a stable `vX.Y.0` without a pushed `bN`/`rcN` image of that version in history; `classify` checks out with `fetch-depth: 0`. Ships in 1.3.14b2 with the `:beta` alias fix.
  - [x] BLOCKER: `KNOWN_REQUIREMENT_KEYS` lacked `migration_baseline`, and that allow-list fails closed — **every published Composer would have refused the Dlux 1.8.14 manifest outright**, so the release was uninstallable as written. Proven by a test run against the pre-fix module (2026-09-08).
  - [x] v1.3.14b1 channels: `versions.py` (real PEP 440 via `packaging`, now a declared image dep) replaces three hand-rolled regexes that each broke on prereleases — the candidate sort tied `b2`/`b10`, `version_sort_key` tied a beta with its own final (breaking rollback and prune), and `_version_at_least` let `1.3.14b1` satisfy `>=1.3.14`. Plus `dlux_channel.py` (read-only policy + request), `channel_config.py` + wrappers at marker 3, `dlux channel`, `check --beta|--stable` as one operation over wrapper *and* resident pair, and `release_tag.py` tag classification with the `:beta`-never-moves-backwards rule (2026-09-08).
  - [x] Released v1.3.13: 556 tests (6 skips), local arm64 runtime smoke, GitHub amd64 smoke and multi-architecture publication passed; no channel code included.
  - [x] v1.3.12: flat agent/DLUX/self routes removed in favor of `agent check/update/restart/off/watch/run/enable`, `dlux check/update/rollback`, `self update`, and `executor run/enable`; leading `-f`/`-d`, generated role commands and wrapper v2 history updated.
  - [x] v1.3.11: staged releases order numerically (`1.8.10` sorted below `1.8.9` as a string), and a rollback target must be strictly below the active release — it could roll forward onto the release just left.
  - [x] v1.3.10: inline updates work end to end — agent stages the verified wheel and the executor swaps it offline over `dlux_package_apply` (`dlux_package_stage.py`), `verify_release` accepts schema-2 manifests, `dlux update` from the project root re-runs itself with the runtime volume attached (`dlux_runtime_access.py`), and the image ships `pypi-attestations`. +47 tests.
  - [x] v1.3.9: `check --fix` normalizes retired local DLUX tools wiring, restart labels, one-pass legacy hardening, native post_start stripping, and `-d` dev overrides.
  - [x] v1.3.8 released: schema-2 DjangoLux manifests enforce inline install safety, migration rollback compatibility, supported service requirements, and the Composer version floor.
  - [x] v1.3.6: missing-label DLUX compatibility migrator + guarded label repair; post-start uses `exec -T`, streams progress, and fails the run; direct `migrate` subcommand with service/arg passthrough. +9 tests.
  - [x] v1.3.5: one migrator run per start — `org.dlux.post-start` label replaces the native Compose `post_start` hook (which Compose ran itself, unflagged, overlapping composer's `-mm` run and clearing STATIC_ROOT mid-collect). Label discovery via `compose_config_json()`, legacy blocks still run + announced, `enable_post_start_label` migration in `check --fix`. `-nm` now means "skip migrations, still collect static" and passes through to the migrator; the old "no hooks at all" meaning moved to `skip_post_start` (`agent update`). `-mm`/`-nm` mutually exclusive. +21 tests.

### One-line info about last verified Tests:
- 2026-09-23: 1.5.2 — 681 tests OK (9 new): check carries repairs + digest, a clean stack offers none, agent-update is delegated and left unacked for its helper, not started twice, refused without an executor, and an over-long run token is rejected rather than truncated.
- 2026-09-23: ops phase 2 — 667 tests OK (7 new): apply refused without/with malformed digest, refused when the files moved since the preview, `check --fix` actually run when it matches, preview lists a repair once and reports a refusing transform as a note.
- 2026-09-23: ops responder live on decrees — panel check answered in ~9 s with the CLI's own 15 findings, a reintroduced flat `agent` command came back as a `fail` finding with its fix hint, an unknown operation was refused, and a 1.4.1 pair (no responder) left the run to time out with DjangoLux's version message. 660 tests OK.
- 2026-09-23: ops responder — 660 tests OK (14 new in `tests/test_ops.py`): token matching, answered-once across restart, unknown operation refused, handler crash/None reported, redaction and bounding of findings, watch loop survives a failing operation.
- 2026-09-22: 1.4.0b4 live pair acceptance on decrees with DjangoLux 1.9.0b2 — apply 1.8.14b2->1.9.0b2 in 35 s (0022 applied, web+celery on the new release, site 200); interval 15->5 min reaches `check-policy.json`; Check-now acked in ~12 s with a fresh report; channel opt-out/in reaches the resident report in <20 s with no CLI; Options renders the interval select. 646 tests OK.
- 2026-09-22: 1.4.0b3 pre-tag — 645 tests OK; the 3 new applier-recreate tests fail on the previous restart path. Live on decrees with 1.4.0b2: pair updated, resident check published the BETA channel with no manual CLI, DjangoLux 1.9.0b2 apply rolled back cleanly when its migration could not be applied (the bug b3 fixes).
- 2026-09-22: 1.4.0b1 live on decrees: pair updated via `agent update`, `check` all-pass, `resident-commands` FAIL + `--fix` repair on a flat copy, `dlux check` wording, `agent check` publication, rollback 1.9.0b1->1.8.14b2 in 20 s. Found: resident check always stable; `--fix` exit 1 after repair. 1.4.0b2 pre-tag: 642 tests OK.
- 2026-09-22: 1.4.0b1 pre-tag — 630 tests OK (6 skips), `release_tag v1.4.0b1` -> beta/advance `:beta`, local `composer:release-1.4.0b1` build + `scripts/smoke-test.sh` exit 0; wrappers unchanged since b2. New `resident-commands` check verified read-only against project-decrees/v2 (flags both services).
- 2026-09-19: full unittest suite passed (619 tests, 6 skips) using isolated CI dependencies; CLI version/help and diff checks passed. Log: `.xpose/manual-agent-check-tests.log`; no production or Docker image validation.
### One-line info about last time edited Docs:
- 2026-09-19: README and agent protocol document manual image publication, opt-out, explicit paths/images, and failure behavior; v1.3.14b3 changelog opened after checking tags.

## Part 2: Global
### Global Standard Helpers, Shortcuts, Info, etc.:
- `run_docker_compose()` and its streaming variant wrap Compose; `read_composer_version()` reads `VERSION`.

### Global Rulesets:
- Down mode bypasses startup checks; retry discovery after secrets when interpolation blocks the initial pass.
- Preserve unrelated worktree changes; never delete files; keep `tracker.md` below 100 lines.

### Agent Handoff Rules:
- `start.py` is intentionally absent; do not restore it.
- Re-run syntax/tests after edits; latest generated caches moved to `.xclude/test-cache-20260905-nested-commands/`.

### References and Links:
- Docker Compose CLI reference: https://docs.docker.com/engine/reference/commandline/
- Executor hardening design: `docs/executor-hardening.md`. Fleet-mgmt plan (dlux repo): `panelPLAN.md` §2.3.
