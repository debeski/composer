"""Handing a verified DjangoLux wheel to a process that has no network.

Two halves of one contract. `composer-agent` is the half with egress, so it
fetches — index, attestation, digest, every trust decision DjangoLux itself
makes — and leaves the wheel in ``downloads/`` on the shared runtime volume.
`composer-executor` is the half with Docker authority, and it is deliberately
kept off every routed network (it sits alone on the ``internal: true``
docker_proxy network); it is handed the version, filename and digest over the
private agent→executor socket and swaps the release in without a single outbound
connection.

The digest travels over the socket rather than in a file beside the wheel on
purpose: `celery` mounts that volume read-write too, so a record kept on the
volume is a record the planted wheel's author could write. Bytes may come from
the volume; what they must hash to may not.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from . import dlux_channel
from . import dlux_release_source as release_source
from .dlux_runtime import DluxRuntime

# Wheels kept in downloads/ besides the one just staged. One spare is enough to
# make a repeat request cheap without the volume growing for ever.
DEFAULT_KEEP_DOWNLOADS = 1


class StagingError(RuntimeError):
    """The release could not be staged, or a staged release is not trustworthy."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def prune_downloads(runtime: DluxRuntime, keep_filename: str, keep=DEFAULT_KEEP_DOWNLOADS) -> list:
    """Drop older staged wheels, never the one just staged."""
    if not runtime.downloads.is_dir():
        return []
    wheels = sorted(
        (item for item in runtime.downloads.glob("*.whl") if item.name != keep_filename),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    removed = []
    for wheel in wheels[max(0, int(keep)):]:
        try:
            wheel.unlink()
            removed.append(wheel.name)
        except OSError:
            pass
    return removed


def stage_release(runtime_root, target_version="", *, source=release_source) -> dict:
    """Fetch and verify a release onto the runtime volume. Returns its identity.

    The returned dict is exactly what the executor needs and nothing more:
    ``{version, filename, sha256}``. A release that may not be applied inline is
    refused here rather than on the far side of the socket, where the operator
    reading the error has less context.
    """
    runtime = DluxRuntime(runtime_root)
    if not runtime.exists():
        raise StagingError(f"No DjangoLux runtime volume at {runtime.root}.")
    runtime.downloads.mkdir(parents=True, exist_ok=True)
    # The agent stages what this deployment is entitled to install. Resolving on
    # the deployment's own channel here — rather than trusting the caller — is
    # what stops a beta reaching a stable deployment through the staging path.
    channel, _policy_error = dlux_channel.read_policy(runtime.state_dir)
    try:
        described = source.describe(target_version, channel=channel, workdir=runtime.downloads)
    except Exception as exc:
        raise StagingError(str(exc)) from exc
    if not described.get("inline_safe"):
        raise StagingError(
            described.get("reason")
            or f"DjangoLux {described.get('version')} cannot be applied inline."
        )
    filename = str(described["filename"])
    wheel = runtime.downloads / filename
    if not wheel.is_file():
        raise StagingError("The verified wheel was not written to the runtime volume.")
    prune_downloads(runtime, filename)
    # Deliberately just the identity: version, filename, digest. The channel
    # decided WHICH release was resolved; it is not part of what the release IS,
    # and the executor verifies the wheel against this digest rather than
    # re-deciding eligibility. test_dlux_package_stage pins that contract.
    return {
        "version": str(described["version"]),
        "filename": filename,
        "sha256": str(described["sha256"]),
    }


class StagedRelease:
    """A release already on the volume, standing in for `dlux_release_source`.

    `apply_package_update` takes its source as a dependency, so the executor's
    half needs nothing but an ``obtain()`` that reads locally. Every check the
    network path makes still happens: the bytes must hash to the digest the
    socket delivered, and the manifest inside the wheel must claim the version
    that was asked for and declare itself inline-safe.
    """

    def __init__(self, runtime: DluxRuntime, *, filename: str, sha256: str, version: str):
        self.runtime = runtime
        self.filename = str(filename)
        self.sha256 = str(sha256).lower()
        self.version = str(version)

    @property
    def wheel(self) -> Path:
        # The protocol rejects a filename with a path separator; joining only the
        # basename here means a bad name can never escape downloads/ even if a
        # future caller skips that validation.
        return self.runtime.downloads / Path(self.filename).name

    def obtain(self, target_version="", *, workdir=None):
        version = str(target_version or self.version)
        if version != self.version:
            raise release_source.ReleaseSourceError(
                f"The staged release is DjangoLux {self.version}, not {version}."
            )
        if not self.wheel.is_file():
            raise release_source.ReleaseSourceError(
                f"DjangoLux {self.version} is not staged on the runtime volume."
            )
        actual = _sha256(self.wheel)
        if actual != self.sha256:
            raise release_source.ReleaseSourceError(
                "The staged wheel does not match the digest the agent verified."
            )
        candidate = release_source.ReleaseCandidate(
            version=self.version, filename=self.wheel.name, url="", sha256=self.sha256,
        )
        # assess() re-reads the manifest *inside* the wheel: it fails unless the
        # release names this version and declares itself inline-safe.
        release_source.assess(candidate, self.wheel)
        workdir = Path(workdir or self.runtime.staging)
        workdir.mkdir(parents=True, exist_ok=True)
        return candidate, release_source.unpack(self.wheel, workdir / "unpacked")
