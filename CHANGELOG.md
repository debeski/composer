# Changelog

## v1.1.8
- **Availability Check Publishes The Target Version (`composer watch`)**: The registry availability check now includes the newer image's own **version** in `image-available.json`, so a downstream reader (e.g. the dlux admin panel) can show "update available to v2.4.0" instead of only a digest. New `registry.remote_image_version(ref, token, label)` reads the image's OCI version label (`org.opencontainers.image.version` by default, override with `COMPOSER_VERSION_LABEL` — the **same** env the preflight version gate uses, so the surfaced version matches the gated version) by fetching the tag manifest, descending into a concrete image manifest for multi-arch indexes, and reading `.config.Labels` from the image config blob — reusing the existing Bearer-token challenge flow via a new `_fetch_bytes` helper. `watcher.check_availability` adds `"version"` per image **best-effort and only when an update exists** (avoids an extra registry round-trip on every poll); any failure (older/private/unsupported registry, missing label, network error) is silently omitted so the check never breaks and readers fall back to the digest. Fully backward compatible with the existing `image-available.json` shape.

## v1.1.7
- **Update Console Log (`composer watch --log-file`)**: `watch` now records a clean, ANSI-free console for each update run so a resident proxy can show a live console during the recreate window (when the app itself is down). The `-uo` child appends progress to `COMPOSER_LOG_FILE` (set by the watcher; default `deploy-log.txt` beside `--status-file`, truncated per run) via a new `OutputUtilsMixin.append_console()` — reusing the already-sanitized `emit_progress`/`emit_status` text (no escape codes, no panel redraw noise) plus `— <phase> —` markers and the failure detail from `write_status()`. New `--log-file PATH` flag; the terminal panel and docker logs are unchanged. Opt-in (no-op without a log path).

## v1.1.6
- **Registry Availability Check (`composer watch`)**: `watch` can now poll a registry for a newer image and publish the result, so another process (e.g. a Django admin) can surface "update available" without registry access of its own. New flags `--check-image IMAGE` (repeatable), `--check-interval SECONDS` (default 3600, min 60), and `--availability-file PATH`. On each check it compares the remote tag digest (new `composer/registry.py` — a minimal registry v2 client doing the standard Bearer-token challenge flow; `COMPOSER_REGISTRY_TOKEN` for private repos) against the locally-pulled digest (`docker image inspect … RepoDigests`, via the socket) and writes `{available, checked_at, images:[{image, remote_digest, local_digest, update_available}]}` atomically. Unreadable remote = "unknown" (never a false positive). The check runs immediately on start, on the interval, and is forced right after an applied update so the signal clears. Availability polling is opt-in (needs both `--check-image` and `--availability-file`); the trigger-file watch is unchanged.

## v1.1.5
- **Resident Updater (`composer watch`)**: New `watch` subcommand turns composer into a trigger-driven, in-compose updater. `composer watch --trigger-file PATH [--interval N] [--status-file PATH] [-f FILE] [-d] [--once]` watches a trigger file and, on each new request (a changed `token` field, or the file's `mtime` when there is no token), shells the existing one-shot `composer -uo` pipeline (pull → version gate → recreate → health → post_start) in a child process — so all one-shot behavior/exit codes stay intact. It records the processed token in `<trigger-file>.ack` (atomic write) so a request is applied exactly once and survives a restart of the watcher container. Clean ownership split: the child writes `COMPOSER_STATUS_FILE`; the watcher owns the ack. Implemented as an early `argv[1] == "watch"` intercept → `composer/watcher.py:run_watch()` with `cli.parse_watch_args()`; documented in the main `--help` epilog and `composer watch --help`.
- **Deploy Status File (`--status-file` / `COMPOSER_STATUS_FILE`)**: New opt-in `StatusWriterMixin` writes an atomic JSON `deploy-status.json` (temp-file + `os.replace`) so an external reader (Django admin panel, dashboard, health probe) can observe the run without scraping the terminal UI. `DockerComposeLauncher.run()` writes lifecycle states through the pipeline — `starting` → `pulling` → `recreating` → `migrating` → `ready`, and `failed` (with a truncated `error`) at every abort; the restart branch reports `restarting`/`ready`/`failed`. Payload includes `status`, `updated_at` (UTC ISO-8601), `composer_version`, `compose_files`, and (when the version gate ran) `target_images`/`target_version`/`active_version`. No file is written unless configured; write failures never abort a deploy.
- **Preflight Version Gate (`--force`)**: New opt-in `VersionGateMixin` refuses an update (`-u`/`-uo`) that would recreate onto an image whose version label is **older** than the deployment's currently-active version — the one move a generic pull-and-restart can't safely undo (old code against a forward-migrated schema). Runs after `pull` (target label is local by then) and before recreate. Reads the target version from an image label (`COMPOSER_VERSION_LABEL`, default `org.opencontainers.image.version`) via `docker image inspect`, and the active version from a JSON file+key (`COMPOSER_ACTIVE_VERSION_FILE` + `COMPOSER_ACTIVE_VERSION_KEY`, default `version` — e.g. the dlux runtime `active.json`). Fully generic and disabled unless an active-version source is configured; missing labels/metadata pass with a note; `--force` overrides a block. Ships a dependency-free PEP440/semver-lite `parse_version` (no `packaging` needed).
- **Removed SOPS/AGE Encryption**: Dropped the optional SOPS/AGE encrypted-secrets path entirely; secrets now resolve exclusively from a plaintext env file (`.env` → `secrets/.env` → `.secrets/.env`). `SecretsMixin` lost `decrypt_secrets_raw()`, `encrypt_secrets_raw()`, `encrypted_secrets_path()`, the `ENCRYPTED_CANDIDATES` list, `parse_dotenv_text()`, and the `enc_file` attribute; `resolve_secrets()` no longer takes `args`, drops the encrypted fallback + AGE key prompt, and stores `self.secrets_source` as the plaintext path string. Removed the `-k`/`--key`, `--encrypt`, `--decrypt`, `-i`/`--input`, `-o`/`--output`, and positional `key_positional` CLI arguments (`composer/cli.py`) and the encrypt/decrypt branches in `DockerComposeLauncher.run()`. `RenderingMixin.render()` now always shows the `🔓 PLAINTEXT <path>` source flag (the `🔐 DECRYPTED` variant is gone).
- **Slimmer Image & Entrypoint**: `Dockerfile` no longer installs the `age` apt package or downloads the `sops` binary; `entrypoint.sh` drops the `keygen`/`encrypt`/`decrypt`/`sops` routes and now execs `python -m composer "$@"` directly. `scripts/smoke-test.sh` drops the `--encrypt`/`--decrypt` flag assertions, the `age`/`sops` runnable checks, the keygen route, and the age+sops round-trip; it still gates on version, core flags, the `run` subcommand, and docker/compose availability.
- **Docs**: `README.md` rewritten to remove the secrets encryption/decryption/keygen workflow and the `-k`/`--encrypt`/`--decrypt` flags.

## v1.1.4
- **`run` Subcommand**: Added `composer run [-m] [-s] [-F] [-f FILE] [-d] <service> <command...>` to run a command inside a Compose service without hand-writing `docker exec`/`docker run`. Defaults to `docker compose exec <service> <command...>`; `-m`/`--manage` prepends `python manage.py`, `-s`/`--shell` wraps the command in `sh -c`, `-F`/`--fresh` switches to a one-off `docker compose run --rm`. TTY is auto-managed (`-T` added when stdin/stdout aren't a terminal). Implemented as an early `argv[1] == "run"` intercept in `DockerComposeLauncher.run()` → `handle_run()` → `DockerComposeMixin.exec_in_service()`, with a new `SubprocessRunnerMixin.run_command_interactive()` (inherited stdio) and `DockerComposeMixin.resolve_compose_cli()` (one-shot plugin/legacy probe since interactive runs can't inspect captured output). Compose-file resolution extracted to `resolve_active_compose_files()` and reused; `run` honors `-f`/`-d`. Documented in the main `--help` epilog and `composer run --help`.

## v1.1.3
- **Update-Then-Recreate (`-u`/`--update [service]`)**: `-u` now pulls the latest image(s) **and** recreates immediately in one step. With a service name it scopes both the pull and the recreate — `composer/cli.py` keeps `nargs="?"`/`const=True`, `DockerComposeLauncher` now sets `up_service` alongside `pull_service`, and `DockerComposeMixin.launch_containers()` appends the service to `up -d` so only that service is recreated (Compose still starts its dependencies; recreate is image/config-change driven, no `--force-recreate`). Native Compose semantics: dependents aren't auto-restarted unless their own image changed.
- **Update-Only (`-uo`/`--update-only [service]`)**: New flag preserving the previous `-u` behavior — pull (optionally one service) before the normal full `up -d` startup, without scoping the recreate. Maps to `update_images`/`pull_service` without setting `up_service`.
- **Restart (`-r`/`--restart [service]`)**: New flag that runs `docker compose restart [service]` instead of `--down` + start, preserving containers so baked-in env vars survive. Added `DockerComposeMixin.restart_containers()` and a dedicated launcher branch that resolves secrets, restarts, then monitors health (no post-start/migration hooks). `RenderingMixin.render()` is restart-aware: shows "Restart Services (svc)", hides the Pull/Post-Start rows, and labels a scoped `-u` recreate as "Start Compose (svc)".

## v1.1.2
- **Skip Commented-Out Var Refs**: `required_compose_vars()` now strips YAML comments before scanning, so a `${VAR}` inside a full-line or trailing ` # …` comment is no longer counted as required. A mid-token `#` (e.g. `url#frag`) is preserved, so a real `${VAR}` after it still counts. Adds `ConfigMixin._COMMENT_RE`.

## v1.1.1
- **Smarter Required-Var Detection**: `required_compose_vars()` no longer produces false "missing variable" failures. It strips `$$` escapes before scanning (so shell variables in command/healthcheck scripts like `$$attempts` are not mistaken for compose interpolations), and subtracts variables the compose already supplies itself — `ConfigMixin._compose_env_keys()` collects keys an `environment:` block assigns a concrete **literal** value to (mapping and list syntax), while bare pass-throughs (`- KEY`) and interpolated values (`KEY: ${KEY}`) are still treated as needing a value.

## v1.1.0
- **Plaintext-First Secrets Resolution (default)**: Running with no secrets flags now auto-resolves secrets. `SecretsMixin.resolve_secrets()` searches plaintext env candidates (`.env`, `secrets/.env`, `.secrets/.env`) and uses the first file that satisfies every variable required by the compose (computed via the new `ConfigMixin.required_compose_vars()`, which parses `${VAR}` interpolations and skips those with a `:-`/`-`/`:+`/`+` default). If none qualify, it falls back to an encrypted file (`secrets.enc`, `secrets/secrets.enc`, `.secrets/secrets.enc`), prompting for the AGE private key only when one was not supplied via `-k`/positional/`SOPS_AGE_KEY`. Added env helpers `parse_env_file()`, `parse_dotenv_text()`, `apply_env_values()` and source helpers `plaintext_env_candidates()`/`encrypted_secrets_path()`; `DockerComposeLauncher` now tracks `secrets_source`.
- **Removed `-sd`/`--skip-decrypt`**: The skip-decrypt flag is obsolete and fully removed from `composer/cli.py`, `launcher.py`, `rendering.py`, and `secrets_manager.py` (dropped `load_secrets()`/`load_secrets_from_file()` and the dev-mode coupling that forced it). `-d`/`--dev` is now purely the two-compose-file override mode and no longer dictates the secrets source.
- **Dev Mode Forces Debug**: `-d`/`--dev` now always turns debug on regardless of the project's `DEBUG`/`DEBUG_STATUS` value (or its absence). `DockerComposeMixin.sync_runtime_compose_override()` injects `DEBUG: "True"` and `DEBUG_STATUS: "True"` into every service's `environment` in the last-applied override file (overriding any compose declaration), `build_compose_env()` exports `DEBUG=True`/`DEBUG_STATUS=True` for `${DEBUG}`/`${DEBUG_STATUS}` interpolation, and the launcher forces the `debug_mode` UI flag. Both names are added to the injected set so they never count as missing required secrets.
- **UI Refresh**: Reworked the status panel in `RenderingMixin.render()` — lighter `━` rules replacing the solid block bars, a bold title, the compose-file list on its own `📂` line, and a secrets-source flag (`🔐 DECRYPTED <path>` / `🔓 PLAINTEXT <path>`) replacing the old `⚠️  BYPASS DECRYPTION` indicator. The first step is relabeled `Load Secrets` and shows the resolved source path.

## v1.0.1
- **Runtime-Gated Image Publishing**: Added `scripts/smoke-test.sh`, which runs the built image and asserts `--version` matches the `VERSION` file, `--help` exposes the core flags (`--down`/`--purge`/`--volumes`/`--update`/`--build`/`--encrypt`/`--decrypt`), the bundled `age`/`sops`/`docker`/`docker compose` binaries are runnable, the `keygen` entrypoint route emits an AGE key, and an end-to-end age+sops encrypt/decrypt round trip succeeds. `.github/workflows/release.yml` now builds the amd64 image with `load: true` and runs the smoke tests **before** the multi-arch Docker Hub push, so a runtime-broken image can no longer be published. `.github/workflows/ci.yml` runs the same smoke tests on every push/PR to `main`.

## v1.0.0
- **Composer Rebrand**: Relaunched under the `Composer` name, replaced the old `Decrypter` branding, removed obsolete passphrase-based encryption/decryption support, and improved the modular package structure (`composer/` mixins) and single-status-line terminal UI.
- **Purge Flag (`-p`/`--purge`)**: Added a `--down` child flag in `composer/cli.py` driving a full compose teardown in `DockerComposeMixin.down_containers()` — appends `-v` (implies volume removal even without `-v`), `--rmi local` to drop built untagged images, and `--remove-orphans`. Adds `DockerComposeMixin.prune_build_cache()` running `docker builder prune -f` for dangling BuildKit cache (not compose-scopeable). Wired through `down_volumes`/`purge` on `DockerComposeLauncher`.
- **Tag-Driven Release Pipeline**: Added `.github/workflows/release.yml` triggered by `v*` tags — verifies the tag matches the `VERSION` file, builds the multi-arch (`linux/amd64,linux/arm64`) image with Buildx, pushes `debeski/composer:<version>` and `debeski/composer:latest` to Docker Hub, and publishes a GitHub Release using the matching `CHANGELOG.md` section. Added `.github/workflows/ci.yml` running `compileall` + CLI smoke and a no-push Docker build on pushes/PRs to `main`.
- **Changelog Renormalized**: Folded the pre-release `v1.0.0`–`v2.0.0` history into the `v0.1.x` series so the first GitHub-Actions-published image starts a clean `v1.0.0`.

## v0.1.13
- Improved compose file reporting by listing all active filenames in the UI and debug logs. Standardized compose file resolution (including `docker-compose.yml` fallback) across all orchestration steps.

## v0.1.12
- Added `--update` flag to wrapper scripts (`start.sh`, `start.ps1`) to explicitly update the Docker image. Removed automatic image pull on every run.

## v0.1.11
- Separated progress messages from state circles to prevent terminal output overwrites, and added dynamic waiting/failing status output during the health check loop to clearly identify stuck containers.

## v0.1.10
- Added `--decrypt` and `--encrypt` flags for standalone crypto operations. Added `-i`/`--input` and `-o`/`--output` to customize file paths for encrypt/decrypt.

## v0.1.9
- Updated start templates for bash and powershell.

## v0.1.8
- Fixed a visual bug where the end result erased previous terminal output.

## v0.1.7
- Passed the launcher version into Compose and automatically injected it into all launched services via a generated runtime override, so deployed projects can read the Composer version without per-project compose edits.

## v0.1.6
- Fixed launcher UI redraw issues that could repeat header lines, kept compose/pull progress on a single in-place status line, improved compose startup diagnostics, and accepted quoted `DEBUG_STATUS` values such as `"True"` when parsing compose config.

## v0.1.5
- Streamed Docker Compose build/pull progress during startup, improved failure diagnostics for compose health/post-start errors, and treated running services without healthchecks as ready instead of hanging.

## v0.1.4
- Added `--down` flag to stop containers and `-v` flag to remove volumes when stopping.

## v0.1.3
- Added `-u` / `--update` flag to force pull container images. Support for specific service targeting (e.g., `-u web`).

## v0.1.2
- Shifted core target pattern to Docker Compose (`:compose` tag default). Removed container-internal web reachability checks in favor of native health states.

## v0.1.1
- Added MIT License, detailed project `.gitignore`, and clarified multi-platform Windows (`.ps1`) usage.

## v0.1.0
- Initial release: Core orchestration for SOPS age encryption and Docker deployment setups.
