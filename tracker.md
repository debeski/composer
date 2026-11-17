# Project Tracker (composer)

## Part 1: Project Related
### Current Verified Snapshot:
- 2026-11-17: **`1.10/relay_env` committed locally** (not pushed; targets 1.10.2b1, paired with DjangoLux 1.16.2b1 `1.16/mail_setup`): `composer/relay_env.py` + `relay-env` check (WARN legacy, WARN shared password, OK custom/current/no relay) + `check --fix` + `ops._relay_env_repair`. Moves smtp-relay's generated `SMTP_RELAY_HOST/PORT/USE_TLS/USER` into `x-environment` after `DEFAULT_FROM_EMAIL` with the stack's own defaults, adds `SMTP_RELAY_USE_SSL` and `SMTP_RELAY_PASSWORD_SET: "${SMTP_RELAY_PASSWORD:+yes}"`; password stays on smtp-relay; validates with `docker compose config` (web/worker values = relay's, no other service gets the password); archives `.xclude/composer-check/relay-env/`; recreates running web/worker/smtp-relay. No schema bump. Migrating DjangoLux 1.16.1's scaffold compose gives 1.16.2's byte for byte; every consumer/testbed compose classifies `legacy`.
- 2026-11-16: **1.10.0 stable published** (#28 `99a498e`, tag `v1.10.0`; `:v1.10.0` = `:latest` = `:beta` `79324b99702b`) = 1.10.0b4 renumbered, paired with DjangoLux 1.16.0 and ScanLink 0.8.0. **1.10.1b1 published + accepted** (#30 `3dc071e`, tag `v1.10.1b1`; `:v1.10.1b1` = `:beta` `47df8f34c779`): #29 `6dcf9b9`, the `front-proxy` check and `check --fix` rewrite for `DLUX_FRONT_PROXY` (DjangoLux 1.16.1 F1). **1.10.1 stable prepared** on `1.10/release_1_10_1` (not pushed) = 1.10.1b1 renumbered, paired with DjangoLux 1.16.1.
- 2026-11-13: **1.10.0b3 prepared** on `1.10/env_restamp`, PR #24 (7279814; not merged or tagged). The 2→3, 3→4 and 4→5 `check --fix` migrations restamp the secrets file `check` reads (`stack_schema.restamp_secrets` + `write_updates`: in place, byte for byte, never copied to the backup, staged as a proxy candidate or shown in a preview). `worker_migration` also catches up files still stamped 4 under a compose.yml at 5. Found by the 1.16 consumer acceptance: the sales CRM `.env` stamped 4 kept a `stack-schema` WARN after b2's fix. Accepted on testbed-crm-sales with `composer:test-1.10.0b3` (b96b3dbd9982) from the restored schema-4 files: `stack-schema` OK at 5, `.env` mode and owner kept and otherwise byte-identical, migrated compose/Caddyfile identical to b2's.
- 2026-11-10: **v1.10.0b1 published** (owner OK relayed by the DjangoLux session; tag on `1.10/dlux_worker` 4d82a9d, PR #18 merged later as b0a7ae9; release run 42758527941 PASS incl. smoke test): `:v1.10.0b1`=`:beta` digest 39720b2b717d (amd64+arm64), GitHub prerelease; `:latest` stays 1.9.2 (10dbc4029875). Paired with DjangoLux 1.16.0b1 (`1.16/task-runner`, django-lux PR #59; its manifest: `requires.stack_schema >=5`, `services.composer >=1.10.0b1`, `install.inline: forbidden`). Stack schema 5: `check --fix` adds `worker` (`composer/worker_migration.py`) once the stack's DjangoLux has `dlux_worker` (running 1.16+, or `-d` + pinned requirement), keeps `celery` project-only, stamps an unstamped old layout 5 once schemas 2-4 hold; `task-worker` check; `requires.stack_schema` in `KNOWN_REQUIREMENT_KEYS`; restart/backup/init-container lists know `worker`. Release order: Composer `v1.10.0b1` tag first, then DjangoLux `v1.16.0b1`. Pre-publish acceptance PASS (reported by the DjangoLux session, testbed-dlux + trademarks): check --fix 4->5, deploy, ops restart of worker, deployment check, inline update and rollback.
- 2026-10-31: **v1.9.2 SECURITY published** (tag on main f780f30; `:latest`=`:beta`=`:v1.9.2` digest 10dbc4029875, GitHub stable) with DjangoLux 1.15.3: `check` stored-files FAIL for a proxy serving `/media/` without auth (hand-edited = WARN), `check --fix` migrates an unmodified generated proxy to stack schema 4 once DjangoLux is 1.15.3+ and exits 0 after fixing. Betas 1.9.2b1/b2 accepted on testbed-dlux. Advisory GHSA-8c56-w8rg-wcff (django-lux) published. Consumer 1.15.2 releases with own /media/ rules classify custom (WARN).
- 2026-10-30: **v1.9.1b2 published** (tag on main 0e0be4a, PRs #9–#11; release run 41042084881 PASS): `:v1.9.1b2`=`:beta` digest 426d11601f59 (amd64+arm64), GitHub prerelease; `:latest` stays 1.9.0. Wrapper 6 (`update self` with -f/-d anywhere, image-ID refresh compare) + `display` release notes in `package-available.json`. Paired with DjangoLux v1.15.2b2 (published same day). **Accepted on testbed-dlux 2026-10-30** pre- and post-publish: deployer/resident on 1.9.1b2, check --fix → wrapper 6, `update self` -f either side, ID compare (rebuilt image under same version reported), unscoped update refreshes deployer first, resident `dlux check` publishes `display`; report in django-lux `main/.xclude/validation/1.15.2b2-acceptance-2026-10-30/report.md`. **Stable 1.9.1** (= 1.9.1b2 renumbered, owner OK 2026-10-30) on `1.9/release_1_9_1`, with DjangoLux 1.15.2.
- **v1.7.0 stable published 2026-10-23**: latest remains1.7.0 after1.8.0b1; maintenance-page ownership, schema3, restart/recreate/logs and image manifest support retained. Paired DjangoLux stable1.13.0 accepted earlier; official testbed now1.13.2.
- **v1.7.0b1 (beta, published + accepted 2026-10-21; `:v1.7.0b1` + `:beta`, tag `d51d7bf` on `1.7/maintenance_page`)**: maintenance page in the image (`composer/assets/maintenance/`, agent publishes on start, `composer maintenance publish [--override]`), `check`/`check --fix` migration to stack schema 3 (validated proxy rewrite, mount dropped, custom page kept as `.proxy/maintenance/`, restamp 2->3), override publishing for new stacks, `release-manifest.json` replaces `VERSION` (image derives `/app/VERSION`, label `org.dlux.composer.release-manifest`). 834 tests OK; smoke test OK; pre-publish testbed migration accepted (evidence `pkg-django-lux/main/.xclude/validation/composer-1.7.0b1-prepublish-2026-10-21/`). Pairs with DjangoLux 1.13.0b1 (schema 3 scaffold).
- 2026-10-24 **`1.9/ocr` locally accepted**, paired DjangoLux `1.15/ocr` 686a5ca: OCR allow-list/lifecycle, unscoped project-image updates and ASCII wrappers v4. OCR now runs from the project image, not a separate registry image. Dev testbed `depy/testbed-dlux-ocr` :8097 uses Composer `--dev`; real settings Stop/Start and queued-scan recovery PASS. No push/tag; Archive unchanged.
- **v1.6.0 (stable, 2026-09-30)** = 1.6.0b1 (re-cut once after a CI failure before publishing): the egress relay (`composer/relay.py`, `relay list|approve`, built-in weather ops, sealed secrets), accepted on `testbed-dlux` and the sales CRM dev stack.
- **v1.5.x (2026-09)**: 1.5.1 `check-fix-apply` via the executor's `check_fix` op; 1.5.3 `--skip-config` + channel-aware unpinned `dlux update`; 1.5.4b1 adds `docker-buildx-plugin` to the image.
- Entrypoints: `python -m composer`, `python composer/main.py`, and Composer-owned `start.sh`/`start.ps1` wrappers.
- Post-start is label-owned; init-container stacks strip updater-era native/label hooks and `check --fix` normalizes compatible legacy forms.
- Nested agent/DLUX CLI is canonical: `agent ...`, `dlux ...`, `update self` (`self update` deprecated alias), `executor ...`.
- Deploy/update runs survive SIGHUP; Compose children use their own session and Ctrl+C still cancels.

### Current Project Adopted Standards:
- Use argparse and existing mixin/helper boundaries; resolve active Compose files early.
- Route Compose operations through the shared command helpers.
- Keep runtime metadata/environment in generated overrides.
- Agent control traffic is outbound HTTPS; localhost HTTP is development-only.
- Preserve deployment originals under `.xclude/` before guarded rewrites.

### Adopted Standards' rules and policies:
- **Beta first, every release, no exceptions** (2026-09-24): `requires_beta_first()` now covers patches too, in this repo and in django-lux. Tag `vX.Y.ZbN`, test it on `:beta`, then tag the stable.
- Secrets are plaintext-only: `.env` -> `secrets/.env` -> `.secrets/.env`.
- Destructive flags require typed confirmation unless `-y` or `COMPOSER_ASSUME_YES=1`; non-TTY fails closed.
- `update` refreshes the deployer (wrapper 5+) then deploys, `pull` only downloads, `update self` updates Composer only, and `-u` is the sole compact update argument.
- Never modify a tagged changelog entry; append changes to the next unreleased version.
- Preserve user changes and move generated caches under `.xclude/`.

### Cross-Cutting Audits if any:
- 2026-07-24 security audit covered protocol, bridge, Docker boundary, registry, subprocess, supply chain, and releases.
- v1.2.7 pins the control origin, blocks credentialed redirects, and validates strict recovery booleans plus full Authorization redaction.

### Current Project's Unsolved Known Bugs:
- A stable `vX.Y.0` tag fails its own unit tests unless the job checks out tags (fixed in 1.4.1 with `fetch-depth: 0`). `v1.4.0` was burned needlessly: nothing had been published, and the owner's admin bypass can delete a tag (`git push origin :refs/tags/vX.Y.Z`), so it should have been re-cut as 1.4.0. Do that next time a release fails before publication; only a published version is unrecoverable.
- A compromised networked agent can abuse the POST-enabled Docker proxy with host-root-equivalent impact.
- Shared-volume temp paths permit symlink clobbering; spool, output, event, and command queues need effective bounds.
- Version gating fails open on missing labels; mutable refs and Windows `shell=True` reconstruction widen risk.

### Incomplete Tasks:
- **Priority 1:**
  - [ ] Accept `1.10/relay_env` with DjangoLux 1.16.2b1 on testbed-dlux (check WARN, `check --fix`, Email step shows the relay's values, mock-provider send), then PR + 1.10.2b1.
  - [ ] Accept `1.10/update_guards` on a testbed, then PR it: two `dlux update --version X --staged-wheel …` a second apart (one refused "already running; nothing was changed"), a rollback during an update, a release that fails its health check (refused next time with its reason; `--retry-quarantined` retries; a healthy retry leaves `failed/X.retried-<time>/`), and `dlux check` / the Updates card no longer offering it.
  - [ ] `dlux rollback` while already on the image's release still restarts web/worker/celery and takes a backup; it should report there is nothing to roll back (1.16.0b2 stress run 2026-11-11, finding 1).
  - [ ] A pinned `dlux update --version X` right after X is published fails "not published" while PyPI's CDN serves a stale simple index (seen 2026-10-09 with 1.11.0b6); the stale-index re-read only covers unpinned updates. Retry pinned resolution the same way.
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
### One-line info about last verified Tests:
- 2026-11-17: `1.10/relay_env` in a scratch venv (Python 3.14, CI's set): suite 1115 OK (16 skips; main 6b5e071: 1073 OK, 15 skips); real `docker compose config` rewrite of schema-4 + testbed-dlux copies (nothing started). Host-suite caveat: `test_agent.AgentOnlyPackageUpdateTests` opens a live PyPI HTTPS connection and can stall the run for minutes.
- 2026-11-14: `1.10/update_guards` in a host venv (Python 3.14, CI's dependency set; sibling `pkg-django-lux/main` read): suite 1031 OK (15 skips; baseline on 757384f 996 OK, 15 skips). No Docker, image build or smoke test run.
- 2026-11-13: `1.10/env_restamp` in `composer:test-1.10.0b3` (b96b3dbd9982; PyYAML 6.0.3, `depy/` mounted): suite 996 OK (16 skips, the new one is root-only); `scripts/smoke-test.sh` passed; `composer.release_tag v1.10.0b3` → prerelease, `:beta` advances. Logs in the checkout's `.xclude/release-1.10.0b3/`.
- Host Python cannot run the suite (no cryptography/packaging/yaml): use a venv with CI's set (`PyYAML==6.0.3 "packaging>=23.2" "cryptography>=42"`), or run it in the built image with `pip install PyYAML==6.0.3` and `~/Desktop/depy` mounted at `/depy`, so the sibling `pkg-django-lux` comparisons run.
### One-line info about last time edited Docs:
- 2026-11-17: `1.10/relay_env`: README check sentence + "SMTP relay settings for the Email step" section (states table, fix rules); CHANGELOG `## Unreleased` (1 entry).
- 2026-11-14: `1.10/update_guards`: README `dlux check`/`update`/`rollback` (operation lock, quarantine rule, `--retry-quarantined`, `quarantined` in the availability report), executor-hardening §C step 4; CHANGELOG `## Unreleased` (2 entries).
- 2026-11-13: `1.10/env_restamp`: README "stack schema stamps" (how migrations restamp the secrets file), stored-files and schema 5 bullets; CHANGELOG `## v1.10.0b3`; manifest 1.10.0b3 + highlight.

## Part 2: Global
### Global Standard Helpers, Shortcuts, Info, etc.:
- `run_docker_compose()` and its streaming variant wrap Compose; `read_composer_version()` reads `VERSION`.

### Global Rulesets:
- Down mode bypasses startup checks; retry discovery after secrets when interpolation blocks the initial pass.
- Preserve unrelated worktree changes; never delete files; keep `tracker.md` below 100 lines.

### Agent Handoff Rules:
- `1.10/relay_env` (`~/Desktop/depy/app-composer/relay_env`) is local only: accept it with DjangoLux 1.16.2b1 on testbed-dlux (owner OK first), then PR; cut 1.10.2b1 with the DjangoLux beta. Hand-over note: side-session-notes `local_56b4c762…/composer-1.10.2-relay-env-done.md`.
- Re-run syntax/tests after edits; latest generated caches moved to `.xclude/test-cache-20260905-nested-commands/`.
- `1.10/update_guards` (`~/Desktop/depy/app-composer/update_guards`, 3 commits on 757384f) is local only: no push, PR, tag or version change until the owner's testbed acceptance passes. Its `tracker.md` will conflict with `1.10/record_1_10_0b3`; merge that first, then rebase this one.

### References and Links:
- Docker Compose CLI reference: https://docs.docker.com/engine/reference/commandline/
- Executor hardening design: `docs/executor-hardening.md`. Fleet-mgmt plan (dlux repo): `panelPLAN.md` §2.3.
