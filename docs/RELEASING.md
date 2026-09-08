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
> so the image must stay on Docker Hub under that name for `self update` to work.

## Cutting a release

1. Update `CHANGELOG.md`: add a new `## vX.Y.Z` section at the top describing the
   changes. The release notes are extracted from this exact section.
2. Bump `VERSION` to the same `X.Y.Z` (no `v` prefix). The workflow **fails** if
   the tag and `VERSION` disagree.
3. **If `start.sh` or `start.ps1` changed in this release**, bump the
   `# composer-wrapper: N` marker in *both* (they share one version), record the
   new sha256s in `wrappers-history.json` — the test suite prints them on
   failure — and re-copy both files into `dlux/scaffold_templates/project/` in
   the django-lux repo, updating the expected version in its scaffold test.
   Without the bump, deployments cannot tell a stale wrapper from an edited one.
4. Commit both on `main`.
5. Tag and push:

   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z
   ```

The `Release` workflow then:

- classifies the tag (`composer/release_tag.py`), verifying `tag == VERSION` and
  that `CHANGELOG.md` has a matching section,
- builds `linux/amd64` + `linux/arm64` with Buildx,
- pushes `debeski/composer:vX.Y.Z` plus the moving alias the tag earns,
- publishes the GitHub Release with the real `prerelease`/`make_latest` flags,
  using the matching `CHANGELOG.md` section.

## Channels: stable and beta

**The tag decides where a release is published.** No commit-message keyword, no
workflow input, no branch convention.

| Tag | Image tags pushed | GitHub release |
| --- | --- | --- |
| `v1.4.0b1` | `:v1.4.0b1`, `:beta` | Prerelease, **not** "latest" |
| `v1.4.0rc1` | `:v1.4.0rc1`, `:beta` | Prerelease, **not** "latest" |
| `v1.4.0` | `:v1.4.0`, `:latest`, and `:beta` *if newer* | Stable, takes "latest" |

A stable release takes `:beta` as well **only when it is genuinely newer than
whatever `:beta` already points at**. A beta tester should receive finals, or
they would sit on `1.4.0b3` for ever once `1.4.0` shipped — but stable `1.4.1`
must never drag a `1.5.0b1` deployment backwards. The beta channel is *ahead* of
stable, and an alias that can move backwards makes "update" mean "downgrade".

The classifier refuses a tag it cannot publish honestly: non-canonical spellings
(`v1.4.0-beta1`, `v1.4.0.b1`, `V1.4.0` — tag `v1.4.0b1`), development releases,
local versions, post-releases, epochs, a tag disagreeing with `VERSION`, and a
version with no `## vX.Y.Z` section in `CHANGELOG.md`.

Check a tag before pushing it:

```bash
python -m composer.release_tag v1.4.0b1
```

### Declare the tested minimum, not the release it precedes

`1.4.0b1` is *not* `1.4.0`: by PEP 440, `>=1.4.0` is false for `1.4.0b1`, and
deliberately so. A DjangoLux beta manifest whose Composer requirement was only
satisfied by a Composer beta must say `">=1.4.0b1"`. Naming the final would
refuse the exact Composer the pair was tested against — which may not exist yet.

## CI

`.github/workflows/ci.yml` runs on every push/PR to `main`: byte-compiles the
`composer` package, smoke-tests the CLI, and builds the Docker image (no push) to
catch `Dockerfile` breakage before a release.

## Versioning note

Versions `v0.1.0`–`v0.1.13` are the pre-CI history (the old `Decrypter`/manual
`debeski/composer:latest` builds), folded down so the first GitHub-Actions-built
image starts at `v1.0.0`.
