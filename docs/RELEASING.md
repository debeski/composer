# Releasing Composer

Composer ships as the public Docker image **`debeski/composer`**. Releases are
**tag-driven**: pushing a `v*` git tag runs `.github/workflows/release.yml`,
which builds the multi-arch image, pushes it to Docker Hub, and creates a GitHub
Release. The `VERSION` file is the single source of truth for the version.

## One-time setup

Add two repository secrets (Settings → Secrets and variables → Actions):

| Secret | Value |
| :--- | :--- |
| `DOCKERHUB_USERNAME` | Docker Hub namespace that owns the image (`debeski`). |
| `DOCKERHUB_TOKEN` | Docker Hub **access token** with read/write on `debeski/composer` (Docker Hub → Account Settings → Personal access tokens). |

> The wrapper scripts (`start.sh`, `start.ps1`) pull `debeski/composer:latest`,
> so the image must stay on Docker Hub under that name for `--update` to work.

## Cutting a release

1. Update `CHANGELOG.md`: add a new `## vX.Y.Z` section at the top describing the
   changes. The release notes are extracted from this exact section.
2. Bump `VERSION` to the same `X.Y.Z` (no `v` prefix). The workflow **fails** if
   the tag and `VERSION` disagree.
3. Commit both on `main`.
4. Tag and push:

   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z
   ```

The `Release` workflow then:

- verifies `tag == VERSION`,
- builds `linux/amd64` + `linux/arm64` with Buildx,
- pushes `debeski/composer:X.Y.Z` and `debeski/composer:latest`,
- publishes the GitHub Release using the matching `CHANGELOG.md` section.

## CI

`.github/workflows/ci.yml` runs on every push/PR to `main`: byte-compiles the
`composer` package, smoke-tests the CLI, and builds the Docker image (no push) to
catch `Dockerfile` breakage before a release.

## Versioning note

Versions `v0.1.0`–`v0.1.13` are the pre-CI history (the old `Decrypter`/manual
`debeski/composer:latest` builds), folded down so the first GitHub-Actions-built
image starts at `v1.0.0`.
