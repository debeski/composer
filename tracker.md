# Project Tracker (composer)

## Part 1: Project Related
### Current Verified Snapshot:
- 2026-10-25:1.9.0b1 candidate ready locally after stable e8a5f70 merge20a2c7e, paired DjangoLux1.15.0b1. Full865 OK/4skips, beta classifier, Docker build+runtime smoke PASS. Actual resident agent/executor1.9.0b1 on8097 accepted with installed693-file DjangoLux wheel: EN/AR lifecycle, burst/batch/race extraction and stopped-service recovery PASS; deep0errors/0fixes. Stable8096/official8088 untouched; no push/tag yet.
- **v1.7.0 stable published 2026-10-23**: latest remains1.7.0 after1.8.0b1; maintenance-page ownership, schema3, restart/recreate/logs and image manifest support retained. Paired DjangoLux stable1.13.0 accepted earlier; official testbed now1.13.2.
- **v1.7.0b1 (beta, published + accepted 2026-10-21; `:v1.7.0b1` + `:beta`, tag `d51d7bf` on `1.7/maintenance_page`)**: maintenance page in the image (`composer/assets/maintenance/`, agent publishes on start, `composer maintenance publish [--override]`), `check`/`check --fix` migration to stack schema 3 (validated proxy rewrite, mount dropped, custom page kept as `.proxy/maintenance/`, restamp 2->3), override publishing for new stacks, `release-manifest.json` replaces `VERSION` (image derives `/app/VERSION`, label `org.dlux.composer.release-manifest`). 834 tests OK; smoke test OK; pre-publish testbed migration accepted (evidence `pkg-django-lux/main/.xclude/validation/composer-1.7.0b1-prepublish-2026-10-21/`). Pairs with DjangoLux 1.13.0b1 (schema 3 scaffold).
- **Composer 1.7.0 plan (owner, 2026-10-21)**: betas first (`v1.7.0bN`, `:beta`), paired with DjangoLux 1.13.0 betas on `testbed-dlux`. Scope: stack hardening (above); maintenance page owned by the Composer image (agent publishes to the runtime volume, proxy serves it, optional project `.proxy/maintenance/` override, stack schema 3, `check --fix` migrates); operations-centre operations (restart/recreate allow-listed services, `logs tail`, "update everything"); **`VERSION` replaced by a `release-manifest.json`** like DjangoLux and projects (version, summary, highlights, release URL), exposed as an image label so DjangoLux's Updates card can show what a Composer update brings (used by DjangoLux 1.14).
- **v1.6.1b4 (beta, published 2026-10-09; `:v1.6.1b4` + `:beta`, amd64+arm64; accepted on `testbed-dlux` with DjangoLux 1.11.0b6)**: `dlux update`/`dlux rollback` take a DjangoLux backup before swapping (`composer/dlux_snapshot.py`, `before_swap`, `--backup data|full|skip`, `COMPOSER_DLUX_BACKUP`); `manage.py dlux_backup` (DjangoLux 1.10.2+, 1.11.0b6) with a `manage.py shell` fallback that marks failures final; executor runs (`COMPOSER_OPERATION_ID`) leave it to DjangoLux. 814 tests OK; verified live on `testbed-dlux` (fallback, refused rollback, primary full backup) — django-lux `testbed_break_scenarios.md` S4.
- 2026-10-24 **`1.9/ocr` locally accepted**, paired DjangoLux `1.15/ocr` 686a5ca: OCR allow-list/lifecycle, unscoped project-image updates and ASCII wrappers v4. OCR now runs from the project image, not a separate registry image. Dev testbed `depy/testbed-dlux-ocr` :8097 uses Composer `--dev`; real settings Stop/Start and queued-scan recovery PASS. No push/tag; Archive unchanged.
- **v1.6.0 (stable, released 2026-09-30)**: identical in scope to 1.6.0b1 (the egress relay). `:v1.6.0`, `:latest` and `:beta` all on one multi-arch digest. Accepted before the tag on `testbed-dlux` and on the sales CRM dev stack (celery taken off `egress`, still refreshed its rates through the declared operations). Not exercised with a valid OpenWeather key. The CRM dev stack and the testbed follow it; other stacks get it with `self update` / `agent update`.
- **v1.6.0b1 (beta, published 2026-09-30, re-cut once)**: the egress relay (`composer/relay.py`, `composer relay list|approve`, built-in `weather.geocode`/`weather.current`, sealed secrets). The first tag failed in CI before publishing anything (the test job installed no `cryptography`, so 23 relay tests errored); the tag was deleted and re-cut on the fix, which also names `cryptography` in the Dockerfile and CI and adds relay checks to the smoke test. `:v1.6.0b1` + `:beta` (amd64+arm64), `:latest` still 1.5.3. Accepted on `testbed-dlux`: deployer and resident pair both 1.6.0b1, `check` all pass, the agent answers relay requests from a celery with no route out.
- **v1.5.x (2026-09)**: 1.5.1 `check-fix-apply` via the executor's `check_fix` op; 1.5.3 `--skip-config` + channel-aware unpinned `dlux update`; 1.5.4b1 adds `docker-buildx-plugin` to the image.
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
- **Beta first, every release, no exceptions** (2026-09-24): `requires_beta_first()` now covers patches too, in this repo and in django-lux. Tag `vX.Y.ZbN`, test it on `:beta`, then tag the stable.
- Secrets are plaintext-only: `.env` -> `secrets/.env` -> `.secrets/.env`.
- Destructive flags require typed confirmation unless `-y` or `COMPOSER_ASSUME_YES=1`; non-TTY fails closed.
- `update` deploys, `pull` only downloads, `self update` updates Composer, and `-u` is the sole compact update argument.
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
  - [ ] A pinned `dlux update --version X` right after X is published fails "not published" while PyPI's CDN serves a stale simple index (seen 2026-10-09 with 1.11.0b6); the stale-index re-read only covers unpinned updates. Retry pinned resolution the same way.
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
  - [x] 2026-10-08: retired the clean, fully merged `feat/egress-relay` branch/worktree; checkout and Git worktree metadata preserved under sibling `../.xclude/`; main unchanged except this tracker note.
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
- 2026-10-25: candidate1.9.0b1 full865 OK/4skips, beta classifier, Docker build+complete runtime smoke PASS. Source+packaged8097 EN/AR password/panel lifecycle and stopped-service recovery PASS; resident versions1.9.0b1, deep22ok/0errors/0fixes. Evidence .xclude/validation/ocr-2026-10-25/ and paired DjangoLux/testbed logs.
- 2026-09-30: 1.5.4b1 — suite 709 OK (6 skips) in a venv with packaging/pyyaml/pypi-attestations; local image has buildx v0.37.1, and a buildx build created no per-step containers.
- 2026-09-25: 1.5.3 on `testbed-dlux` — b2 local build then published: unpinned `dlux update` resolves beta 1.9.4b1 / stable 1.9.3 by policy, the agent->executor handoff applied 1.9.4b1 (ack exit 0); stable 1.5.3 installed stable 1.9.4 over 1.9.3. Suite 705/709 (4 need a Docker CLI in the container).
- 2026-09-24: `--skip-config` live on the decrees rig from a locally built 1.5.3b1 image — `./start.sh --skip-config` put `DLUX_SKIP_CONFIG_IMPORT=True` into web, celery and composer-agent, DjangoLux returned `skipped` with `config.json` byte-identical, and a plain `./start.sh` left the variable unset. 704 tests OK (beta-first now gates patches too).
- 2026-09-24: the card-driven `agent-update` ran against PUBLISHED images for the first time — rig on 1.5.2b1, card offered 1.5.2 from `:beta`, the executor's detached helper pulled and recreated both containers and wrote its own ack (exit 0, 164s). Resident is 1.5.2, `deployer_version` reads 1.5.2b1 (the helper that performed it), and a re-check answers "the beta channel's current version".
- 2026-09-24: 1.5.2b1 published to `:beta`; the decrees rig was put back on published 1.5.1, then host-updated to `:beta` from the registry (resident 1.5.2b1, deployer 1.5.1 published in agent-status). From there DjangoLux's card ran `agent-check` (tick: resident == the beta channel's current version) and the deployment check (15/15 OK). 701 tests OK.
- 2026-09-23: ops responder live on decrees — panel check answered in ~9 s with the CLI's own 15 findings, a reintroduced flat `agent` command came back as a `fail` finding with its fix hint, an unknown operation was refused, and a 1.4.1 pair (no responder) left the run to time out with DjangoLux's version message. 660 tests OK.
### One-line info about last time edited Docs:
- 2026-10-25: candidate1.9.0b1 manifest/changelog and agent protocol document OCR pairing/floor, project-image rebuild and wrapper4; historical stable records retained.
- 2026-09-30: README `--build` row notes buildx/BuildKit; 1.5.4b1 changelog/version opened after verifying the 1.5.3 tag.

## Part 2: Global
### Global Standard Helpers, Shortcuts, Info, etc.:
- `run_docker_compose()` and its streaming variant wrap Compose; `read_composer_version()` reads `VERSION`.

### Global Rulesets:
- Down mode bypasses startup checks; retry discovery after secrets when interpolation blocks the initial pass.
- Preserve unrelated worktree changes; never delete files; keep `tracker.md` below 100 lines.

### Agent Handoff Rules:
- 1.9/OCR polish/stress authorized2026-10-25, paired DjangoLux1.15/OCR; main e8a5f70 integration underway. Testbed depy/testbed-dlux-ocr8097 via ./run.sh --dev and standard dev login. Required main review and beta validation remain before publication; Archive adoption separate.
- Re-run syntax/tests after edits; latest generated caches moved to `.xclude/test-cache-20260905-nested-commands/`.

### References and Links:
- Docker Compose CLI reference: https://docs.docker.com/engine/reference/commandline/
- Executor hardening design: `docs/executor-hardening.md`. Fleet-mgmt plan (dlux repo): `panelPLAN.md` §2.3.
