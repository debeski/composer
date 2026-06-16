# Changelog

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
