# Releasing Composer

Composer ships as the public Docker image **`debeski/composer`**. Releases are
**tag-driven**: pushing a `v*` git tag runs `.github/workflows/release.yml`,
which builds the multi-arch image, pushes it to Docker Hub, and creates a GitHub
Release. `release-manifest.json` is the single source of truth for the version and
the release notes a deployment sees (`version`, `summary`, `highlights`,
`release_url`). The image derives `/app/VERSION` from it for deployed wrappers
and older Composer images, and carries the whole manifest as the label
`org.dlux.composer.release-manifest` (`base64:` + URL-safe base64 of compact
JSON, the same encoding as a project image's `org.dlux.project.release-manifest`)
so DjangoLux can show what a Composer update brings without pulling it.

## One-time setup

Add two repository secrets (Settings → Secrets and variables → Actions):

| Secret | Value |
| :--- | :--- |
| `DOCKERHUB_USERNAME` | Docker Hub namespace that owns the image (`debeski`). |
| `DOCKERHUB_TOKEN` | Docker Hub **access token** with read/write on `debeski/composer` (Docker Hub → Account Settings → Personal access tokens). |

> The wrapper scripts (`start.sh`, `start.ps1`) pull `debeski/composer:latest`
> (`:beta` on the beta channel), so the image must stay on Docker Hub under that
> name for `update self` to work.

CI and release actions stay pinned to full commit SHAs. Grouped action updates
are validated together with the release candidate before publication; the
changelog records which versions each release moves to.

## Cutting a release

1. Update `CHANGELOG.md`: add a new `## vX.Y.Z` section at the top describing the
   changes. The release notes are extracted from this exact section.
2. Set `version` in `release-manifest.json` to the same `X.Y.Z` (no `v` prefix)
   and update its `summary`, `highlights` and `release_url`. The workflow
   **fails** if the tag and the manifest's version disagree.
3. **If `start.sh` or `start.ps1` changed in this release**, bump the
   `# composer-wrapper: N` marker in *both* (they share one version), record the
   new sha256s in `wrappers-history.json` — the test suite prints them on
   failure — and re-copy both files into `dlux/scaffold_templates/project/` in
   the django-lux repo, updating the expected version in its scaffold test.
   Without the bump, deployments cannot tell a stale wrapper from an edited one
   ([wrapper versioning](deploying.md#wrapper-versioning)).
4. Commit both on `main`.
5. Tag and push:

   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z
   ```

The `Release` workflow then:

- classifies the tag (`composer/release_tag.py`), verifying the tag equals the manifest's `version` and
  that `CHANGELOG.md` has a matching section,
- runs the unit tests, then builds an amd64 image and runs `scripts/smoke-test.sh`
  on it,
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
local versions, post-releases, epochs, a tag disagreeing with the manifest's `version`, and a
version with no `## vX.Y.Z` section in `CHANGELOG.md`.

Check a tag before pushing it:

```bash
python -m composer.release_tag v1.4.0b1
```

### Every stable release is a beta first

The classifier refuses a stable tag unless a beta or release candidate of that
exact version is in the tagged commit's history **and** Docker Hub serves its
image (`requires_beta_first()`, `validate_beta_first()`). `v1.4.0` cannot publish
until `v1.4.0b1` (or `rc1`) has, and a beta tag whose build never pushed does not
count. Patch releases are gated the same way: `v1.4.1` needs a published
`v1.4.1bN`. A registry that cannot be reached refuses the tag.

This proves a beta was *published*, not that it passed acceptance.

### Declare the tested minimum, not the release it precedes

`1.4.0b1` is *not* `1.4.0`: by PEP 440, `>=1.4.0` is false for `1.4.0b1`, and
deliberately so. A DjangoLux beta manifest whose Composer requirement was only
satisfied by a Composer beta must say `">=1.4.0b1"`. Naming the final would
refuse the exact Composer the pair was tested against — which may not exist yet.

## CI

`.github/workflows/ci.yml` runs on every push/PR to `main`: byte-compiles the
`composer` package, runs the unit tests (`python -m unittest discover -s tests`),
smoke-tests the CLI, builds the Docker image (no push) to catch `Dockerfile`
breakage before a release, and runs `scripts/smoke-test.sh` against that image.

## Image vulnerability scan

Before a release, scan the built image with the same gate the Composer image has
been held to since 1.10.3:

```bash
trivy image --scanners vuln --ignore-unfixed --severity HIGH,CRITICAL --exit-code 1 \
  --ignorefile .trivyignore debeski/composer:vX.Y.Z
```

It is a manual step: neither workflow runs Trivy. `.trivyignore` lists the findings
accepted for now, each with its reason and an expiry date (`exp:`). After that date
the gate fails on it again, and the finding is rechecked: drop the entry once Docker
ships a fixed plugin, or renew it with the reason still true.

| CVE | Where | Why it is accepted | Expires |
| --- | --- | --- | --- |
| CVE-2026-17106 | `docker-buildx-plugin` 0.39.1 (`moby/go-archive` v0.2.0, link following in tar extraction) | buildx runs only to build the project's own source (`-b`, `-d`); deploy hosts and the resident pair never build. It is kept because without it `--build` falls back to the deprecated classic builder. | 2027-01-31 |
| CVE-2026-78661 | `docker-compose-plugin` 5.8.0 (`golang.org/x/net` v0.60.0, denial of service on crafted HTML) | Compose parses no untrusted HTML in Composer's use. | 2027-01-31 |

Both plugins were the newest upstream releases when the exceptions were recorded
(2026-11-25), and neither upstream main branch had bumped the library yet.

## Versioning note

Versions `v0.1.0`–`v0.1.13` are the pre-CI history (the old `Decrypter`/manual
`debeski/composer:latest` builds), folded down so the first GitHub-Actions-built
image starts at `v1.0.0`.
