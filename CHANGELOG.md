# Changelog

- **v2.0.0** - Relaunched under the `Composer` name, replaced the old `Decrypter` branding, removed obsolete passphrase-based encryption/decryption support, improved the modular structure and terminal UI, and released the image as `debeski/composer:latest`.

- **v1.0.14** - Improved compose file reporting by listing all active filenames in the UI and debug logs. Standardized compose file resolution (including `docker-compose.yml` fallback) across all orchestration steps.

- **v1.0.13** - Added `--update` flag to wrapper scripts (`start.sh`, `start.ps1`) to explicitly update the Docker image. Removed automatic image pull on every run.

- **v1.0.11** - Separated progress messages from state circles to prevent terminal output overwrites, and added dynamic waiting/failing status output during the health check loop to clearly identify stuck containers.

- **v1.0.10** - Added `--decrypt` and `--encrypt` flags for standalone crypto operations. Added `-i`/`--input` and `-o`/`--output` to customize file paths for encrypt/decrypt.

- **v1.0.9** - Updated start templates for bash and powershell.

- **v1.0.8** - Fixed a visual bug where the end result erased previous terminal output.

- **v1.0.7** - Passed the launcher version into Compose and automatically injected it into all launched services via a generated runtime override, so deployed projects can read the Composer version without per-project compose edits.

- **v1.0.6** - Fixed launcher UI redraw issues that could repeat header lines, kept compose/pull progress on a single in-place status line, improved compose startup diagnostics, and accepted quoted `DEBUG_STATUS` values such as `"True"` when parsing compose config.

- **v1.0.5** - Streamed Docker Compose build/pull progress during startup, improved failure diagnostics for compose health/post-start errors, and treated running services without healthchecks as ready instead of hanging.

- **v1.0.4** - Added `--down` flag to stop containers and `-v` flag to remove volumes when stopping.

- **v1.0.3** - Added `-u` / `--update` flag to force pull container images. Support for specific service targeting (e.g., `-u web`).

- **v1.0.2** - Shifted core target pattern to Docker Compose (`:compose` tag default). Removed container-internal web reachability checks in favor of native health states.

- **v1.0.1** - Added MIT License, detailed project `.gitignore`, and clarified multi-platform Windows (`.ps1`) usage.

- **v1.0.0** - Initial release: Core orchestration for SOPS age encryption and Docker deployment setups.
