# Project Tracker (composer)

## Part 1: Project Related
### Current Verified Snapshot:
- 2026-11-19: **1.10.2b2 published + accepted** (#37 merged on the owner's OK, tag `v1.10.2b2` on main d0ffb07; `:v1.10.2b2` = `:beta` `8366dea9`, `:latest` stays 1.10.1) = #35 `relay-version` + #36 `dlux rollback` no-op, with DjangoLux 1.16.2b2. Pre-publish (2026-11-18) and beta-channel acceptance PASS on testbed-dlux (`testbeds/dlux/.xclude/1.16.2b2-*/report.md`: `update self` 1.10.2b1 -> 1.10.2b2, `relay-version` OK, rollback on the image release a no-op). **1.10.2 stable prepared** on `1.10/release_1_10_2` = 1.10.2b2 renumbered, paired with DjangoLux 1.16.2; not tagged.
- 2026-11-18: **1.10.2b1 published + accepted** (tag on main c0d59cf = #32 relay-env + #34 certificate settings + python 3.15 base; `:v1.10.2b1` = `:beta` `00ec99585ea9`, `:latest` stays 1.10.1) with DjangoLux 1.16.2b1: pre-publish and beta-channel acceptance PASS on testbed-dlux. The beta-channel run found that an inline 1.16.2b1 leaves smtp-relay on the image's DjangoLux (no certificate check) while the Email step shows the check on: `1.10/relay_image_check` adds the `relay-version` finding (local, not pushed).
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
  - [ ] **`1.10/fetch_metadata` (2026-11-18, local commit, not pushed)**: `fetch-metadata` WARN when `.proxy/Caddyfile` / `.proxy/default.conf.template` lacks DjangoLux's `cross_site_request` policy (paired DjangoLux `1.16/fetch_metadata`). **Owner 2026-11-18: ships with DjangoLux 1.16.3** (beside #92), not in 1.10.2b2; rebase onto main after the 1.10.2b2 release PR, then push/PR on the owner's word.
  - [ ] **Private registry login (`1.10/private_registry`, for DjangoLux 1.16.3; owner 2026-11-19)**: `registry_login` (`COMPOSER_REGISTRY_USERNAME`/`PASSWORD`, scoped to WEB_IMAGE's registry or `COMPOSER_REGISTRY_HOST`), Basic auth on the token realm, private `DOCKER_CONFIG` for every compose run, `registry-login` finding, resident-block lines; pairs with DjangoLux `1.16/private_registry`. Open: office CA question (eservices). 1.10.2 stable published 2026-11-19. Later: `relay-version` prints versions without the beta suffix. The Fetch Metadata check (`1.10/fetch_metadata`) ships beside DjangoLux 1.16.3.
  - [ ] **App data permissions for non-root DjangoLux images (`1.10/non_root_app`, pairs with DjangoLux `1.16/non_root_app`; owner decisions 2026-11-19: `USER 10001:0`, Composer fix + pre-`up` grant)**: built 2026-11-19, PR #40 open with django-lux#98. `composer/app_permissions.py`, `app-permissions` finding + `check --fix` grant (no-network sibling, modes only, manifest + `app-permissions revert`), launcher `prepare_app_data()` before `compose up` (grant or refuse), umask 0002 / 0664 runtime writes, agent opens owned runtime dirs to group 0. Live run on copied testbed-dlux data PASS (report `pkg-django-lux/non_root_app/.xclude/validation/non-root-app-2026-11-19/report.md`). 1.10.3b1 stress-review fixes committed locally, not pushed: pre-up pulls an absent app image before reading its user, recreates run the grant, runtime files need only read, `dlux`/agent-check/update-helper writers group-shared.
  - [ ] **`1.10/deps_refresh` (2026-11-23, committed, not pushed; owner: in 1.10.3 with DjangoLux 1.16.3b1)**: `Dockerfile` `apt-get -y upgrade` + `python -m pip uninstall --yes pip`; `scripts/smoke-test.sh` fails an image with pip. Trivy 0.76.2 gate: 1.10.2 had 8 HIGH; 2 HIGH left in Docker's newest `docker-buildx-plugin` 0.39.1 (moby/go-archive v0.2.0) and `docker-compose-plugin` 5.8.0 (x/net v0.60.0), no upstream fix yet; re-check at 1.10.3 assembly. DjangoLux pair: `1.16/deps_refresh`.
  - [ ] Accept `1.10/update_guards` on a testbed, then PR it: two `dlux update --version X --staged-wheel …` a second apart (one refused "already running; nothing was changed"), a rollback during an update, a release that fails its health check (refused next time with its reason; `--retry-quarantined` retries; a healthy retry leaves `failed/X.retried-<time>/`), and `dlux check` / the Updates card no longer offering it.
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
### One-line info about last verified Tests:
- 2026-11-18: `1.10/fetch_metadata` with `pkg-django-lux/main/.venv` (Python 3.13, PyYAML 6.0.3, CI's set) and `DLUX_CHECKOUT=../pkg-django-lux/fetch_metadata`: suite 1143 OK (14 skips); `test_fetch_metadata` 15 OK incl. the scaffold passing and failing without its block.
- 2026-11-23: `1.10/non_root_app` review fixes in the scratch venv (CI's set): suite 1173 OK (17 skips; the DjangoLux contract mirror passes against `pkg-django-lux/non_root_app`), compileall OK; unit tests only. 2026-11-19 smoke test + live run PASS.
- 2026-11-18: `1.10/relay_image_check` in the scratch venv (Python 3.14, CI's set): suite 1127 OK (16 skips), compileall OK; the `python -m dlux --version` probe read 1.16.0b1 / 1.16.2b1 from the testbed-dlux images (no stack started).
- 2026-11-17: `1.10/relay_env` in a scratch venv (Python 3.14, CI's set): suite 1115 OK (16 skips; main 6b5e071: 1073 OK, 15 skips); real `docker compose config` rewrite of schema-4 + testbed-dlux copies (nothing started); `composer:test-relay-env` (c6b297bf857b) `scripts/smoke-test.sh` passed. Host-suite caveat: `test_agent.AgentOnlyPackageUpdateTests` opens a live PyPI HTTPS connection and can stall the run for minutes.
- 2026-11-19: `1.10/private_registry`: suite 1156 OK (14 skips) with the sibling `pkg-django-lux/main/.venv`; `tests/test_registry_login.py` (28).
- 2026-11-23: `1.10/deps_refresh` in a scratch venv (Python 3.14, CI's set): suite 1128 OK (16 skips). amd64 image (`--no-cache`): `smoke-test.sh` PASS (no pip); Trivy 0.76.2 gate rc=1 on 2 HIGH, both in Docker's plugins (buildx 0.39.1 moby/go-archive, compose 5.8.0 x/net), plus 4 MEDIUM there; Debian and Python clean (1.10.2: 8 HIGH). Evidence `deps_refresh/.xclude/validation/1.10.3-deps-refresh-2026-11-23/`.
- Host Python cannot run the suite (no cryptography/packaging/yaml): use a venv with CI's set (`PyYAML==6.0.3 "packaging>=23.2" "cryptography>=42"`), or run it in the built image with `pip install PyYAML==6.0.3` and `~/Desktop/depy` mounted at `/depy`, so the sibling `pkg-django-lux` comparisons run.
### One-line info about last time edited Docs:
- 2026-11-18: `1.10/fetch_metadata`: README check sentence + "Cross-site requests (Fetch Metadata)" section (markers table, fix); CHANGELOG `## Unreleased` (1 entry).
- 2026-11-23 `1.10/non_root_app`: README App data permissions (state files read-only, pre-up pull, recreates, writers); earlier command map, check summary, `docs/executor-hardening.md` §5, CLI usage; CHANGELOG Unreleased.
- 2026-11-18: `1.10/relay_image_check`: README "SMTP relay settings for the Email step" (relay runs the image's DjangoLux, `relay-version` finding); CHANGELOG `## Unreleased` (1 entry).
- 2026-11-17: `1.10/relay_env`: README check sentence + "SMTP relay settings for the Email step" section (states table, fix rules); CHANGELOG `## Unreleased` (1 entry).
- 2026-11-19: `1.10/private_registry`: README **Private registry login** (findings table), `agent check` and `watch` paragraphs; CHANGELOG `## Unreleased` (1 entry).
- 2026-11-19: `1.10/non_root_app` in a scratch venv (Python 3.14, CI's set): suite 1160 OK (16 skips), compileall OK, `scripts/smoke-test.sh composer:test-non-root` passed; live migration run on `testbeds/nonroot-migration` PASS.
- 2026-11-19 `1.10/non_root_app`: README App data permissions + command map + check summary, `docs/executor-hardening.md` §5 group-writable runtime files, CLI usage, CHANGELOG Unreleased.
- 2026-11-23: `1.10/deps_refresh`: CHANGELOG `## Unreleased` (1 entry); no doc owns the image's packages.

## Part 2: Global
### Global Standard Helpers, Shortcuts, Info, etc.:
- `run_docker_compose()` and its streaming variant wrap Compose; `read_composer_version()` reads `VERSION`.

### Global Rulesets:
- Down mode bypasses startup checks; retry discovery after secrets when interpolation blocks the initial pass.
- Preserve unrelated worktree changes; never delete files; keep `tracker.md` below 100 lines.

### Agent Handoff Rules:
- 1.10.2 stable: release branch `1.10/release_1_10_2` (`~/Desktop/depy/app-composer/release_1_10_2`); after green CI and the owner's OK: merge, tag `v1.10.2` on main, confirm `:v1.10.2` = `:latest` = `:beta`, then DjangoLux tags `v1.16.2`.
- Re-run syntax/tests after edits; latest generated caches moved to `.xclude/test-cache-20260905-nested-commands/`.
- `1.10/update_guards` (`~/Desktop/depy/app-composer/update_guards`, 3 commits on 757384f) is local only: no push, PR, tag or version change until the owner's testbed acceptance passes. Its `tracker.md` will conflict with `1.10/record_1_10_0b3`; merge that first, then rebase this one.

### References and Links:
- Docker Compose CLI reference: https://docs.docker.com/engine/reference/commandline/
- Executor hardening design: `docs/executor-hardening.md`. Fleet-mgmt plan (dlux repo): `panelPLAN.md` §2.3.
