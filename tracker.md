# Project Tracker (composer)

## Part 1: Project Related
### Current Verified Snapshot:
- Composer v1.3.13 is tagged/published at b5af7df (2026-09-07). The tree is **v1.3.14b1 (UNTAGGED)** — the beta-channel rehearsal, paired with Dlux 1.8.14b1. `preflight_version_gate()` accepts only a `keep` verdict from `dlux_image_gate` for the older-image exception.
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
- A compromised networked agent can abuse the POST-enabled Docker proxy with host-root-equivalent impact.
- Shared-volume temp paths permit symlink clobbering; spool, output, event, and command queues need effective bounds.
- Version gating fails open on missing labels; mutable refs and Windows `shell=True` reconstruction widen risk.

### Incomplete Tasks:
- **Priority 1:**
  - [ ] TAG THE REHEARSAL: `v1.3.14b1` must be published BEFORE Dlux `v1.8.14b1` — that manifest requires `>=1.3.14b1`. Nothing pushed yet. Then verify `:beta` and `:v1.3.14b1` both appear and `:latest` did NOT move.
  - [ ] `release_channels_plan.md` remaining: the shared reference-stack acceptance harness (§7), published-artifact acceptance as a dependent job (§3.8), promotion serialization/alias comparison under concurrency (§3.6), and a Composer 1.4.0 retirement inventory (§9 — none exists yet). 1.9.0b1/1.4.0b1 stay the first feature betas.
  - [ ] Untested in a real registry: the `:beta` alias read (`docker buildx imagetools inspect`) that decides whether a stable release may advance `:beta`. Absent/unreadable falls back to "advance", which is right for a first publish; confirm on the first stable after a beta.
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
- 2026-09-08: 1.3.14b1 — local arm64 `composer:release-1.3.14b1` build and `scripts/smoke-test.sh` passed (exit 0), including the real agent lifecycle and the new `dlux channel` help sweep; `packaging` 26.3 confirmed present in the image and `1.3.14b1 >= 1.3.14` correctly False inside it. GitHub amd64 smoke still runs on tag.
- 2026-09-08: channels — 599 tests OK (43 new in `tests/test_channels.py`), 4 skips. Key tests confirmed failing against the pre-change modules: the `migration_baseline` refusal, `select_candidate(channel=…)`, and `1.3.14b1` satisfying `>=1.3.14`. No image build or smoke test run yet for 1.3.14b1.
- Verified 2026-09-07: full unittest discovery ran 556 tests, OK (6 standalone Dlux interop skips); local arm64 `composer:release-1.3.13` build and `scripts/smoke-test.sh` passed, including real agent lifecycle. Logs under `.xpose/release-v1.3.13-*.log`.
- Verified 2026-09-05: 185 targeted CLI/wrapper/DLUX/agent/checkup tests pass; rebuilt `composer:ci-test` and `./scripts/smoke-test.sh composer:ci-test` passes; full discovery blocked by missing PyYAML.
- Verified 2026-09-05: 544 tests pass (4 new on release ordering); rollback target and prune verified against a staged 1.8.9/1.8.10 pair.
- Verified 2026-09-04: 540 tests pass; real e2e in the built image — stage 1.8.7 from PyPI, offline apply activates it (`active.json` -> volume), a tampered wheel is refused; DLUX check/update worked from the sales project root.
- Verified 2026-09-04: transformed `project-trademarks/compose.yml` candidate has no DLUX stack-contract drift except intentionally preserved `pgadmin_data`.
- Verified 2026-08-29: 485 tests passed with 6 expected skips; local image smoke and the v1.3.8 GitHub release workflow passed.
- Verified 2026-08-07: 333/333 tests; live project-archive compatibility discovery + `composer migrate -d -nm` exited 0 and replaced 171 static files with 172.
- Verified 2026-08-07: missing-label `check --fix` dry-run against project-archive produces only the `web` label insertion.
- Verified 2026-08-18: Docker Scout on published v1.3.6 digest = 4C/22H (2C/16H fixable); plain rebuild -> 3C/17H; +pip removal -> 3C/14H (1C/8H fixable) and smoke-test passes.
- Verified 2026-08-18: residual fixable C/H all live in Docker's own `docker-ce-cli` 29.7.2 binary (Go stdlib 1.26.5, x/mod 0.38.0, docker/docker 28.5.2); alpine base is worse (docker-cli 29.5.3 = 9C/16H fixable).
### One-line info about last time edited Docs:
- 2026-09-07: README version-gate section documents `dlux_image_gate`, Dlux 1.8.12+, `COMPOSER_RUNTIME_GATE_SERVICE` and fail-closed behavior; concise v1.3.13 changelog prepared.
- 2026-09-05: README, agent protocol, executor hardening, and release docs use nested `agent`/`dlux`/`self`/`executor` command paths.

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
