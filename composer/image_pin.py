"""Pin Composer's own image to a digest that its release job signed.

`debeski/composer:latest` and `:beta` are moving tags, so whoever can push to
Docker Hub decides what the deployer and the resident pair run next. This module
moves that decision to the release job instead:

- Each release publishes ``composer-image.json`` on its GitHub Release: the
  version, its tag and the multi-arch index digest it pushed. Next to it is a
  Sigstore bundle, signed keylessly by ``release.yml`` running on that tag.
- `resolve()` reads the channel tag's version label from the registry. That
  label is only a hint. It then downloads that version's statement, verifies
  the bundle against the release workflow's identity for that exact tag, and
  returns ``debeski/composer:vX.Y.Z@sha256:…``.
- The pin lives in ``.composer-image`` beside ``.composer-channel``. The wrapper
  runs the deployer from it, and the generated block runs the resident pair
  from it. Pulling by digest makes the content itself tamper-proof.

A registry that lies can therefore only point a tag at a genuine older release.
`resolve()` refuses to move backwards unless the caller allows it.

Two kinds of failure are kept apart:

- **Unavailable**: GitHub cannot be reached, the assets are missing, or Sigstore
  is not installed. The explicit `--allow-unverified-composer` escape may then
  pin the registry's digest for that version's own tag.
- **Invalid**: the signature, identity or statement does not match. No flag
  overrides that.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import channel_config, registry, versions
from .safe_files import atomic_write_text

LOCK_FILENAME = ".composer-image"
REPOSITORY = channel_config.IMAGE_REPOSITORY
GITHUB_REPOSITORY = "debeski/composer"
STATEMENT_NAME = "composer-image.json"
BUNDLE_NAME = "composer-image.json.sigstore.json"
WORKFLOW_PATH = ".github/workflows/release.yml"
OIDC_ISSUER = "https://token.actions.githubusercontent.com"
STATEMENT_SCHEMA = 1

#: The environment form of `--allow-unverified-composer`, which the wrapper forwards.
ALLOW_UNVERIFIED_ENV = "COMPOSER_ALLOW_UNVERIFIED_COMPOSER"

#: Where a GitHub release download may redirect to: the release-asset CDN.
ASSET_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}
MAX_STATEMENT_BYTES = 16 * 1024
MAX_BUNDLE_BYTES = 256 * 1024
FETCH_TIMEOUT = 20
VERIFY_TIMEOUT = 120

_TAG = r"v\d+(?:\.\d+){1,2}(?:(?:a|b|rc)\d+)?"
_DIGEST = r"sha256:[0-9a-f]{64}"
PINNED_REF = re.compile(rf"^{re.escape(REPOSITORY)}:(?P<tag>{_TAG})@(?P<digest>{_DIGEST})$")
DIGEST_RE = re.compile(rf"^{_DIGEST}$")


class PinError(RuntimeError):
    """A pin could not be resolved. ``unavailable`` marks a reachability problem.

    ``way_out`` says what to do instead, when the refusal is not one a retry or
    `--allow-unverified-composer` can change.
    """

    def __init__(self, message: str, *, unavailable: bool = False, way_out: str = ""):
        super().__init__(message)
        self.unavailable = unavailable
        self.way_out = way_out


@dataclass(frozen=True)
class Pin:
    version: str
    digest: str
    verified: bool = True

    @property
    def tag(self) -> str:
        return f"v{self.version}"

    @property
    def ref(self) -> str:
        return f"{REPOSITORY}:{self.tag}@{self.digest}"

    @property
    def prerelease(self) -> bool:
        return versions.is_prerelease(self.version)

    @property
    def short(self) -> str:
        return f"{self.version} ({self.digest[:19]}…)"


def parse_ref(text) -> Optional[Pin]:
    """The pin a ``debeski/composer:vX@sha256:…`` reference names, or ``None``."""
    match = PINNED_REF.match(str(text or "").strip())
    if not match:
        return None
    return Pin(version=match.group("tag")[1:], digest=match.group("digest"))


def lock_file(project_dir=".") -> Path:
    return Path(project_dir) / LOCK_FILENAME


#: What the wrappers strip from the ends of the lock's line (bash and PowerShell alike).
_LOCK_BLANKS = " \t\r\v\f"


def _lock_line(raw: str) -> str:
    """The first non-empty line, stripped at its ends only, as both wrappers read it.

    A leading UTF-8 byte-order mark is dropped, as PowerShell's ``Get-Content`` drops it.
    """
    raw = raw[1:] if raw.startswith("\ufeff") else raw
    for line in raw.split("\n"):
        line = line.strip(_LOCK_BLANKS)
        if line:
            return line
    return ""


def read_lock(project_dir=".") -> Optional[Pin]:
    """The pin in ``.composer-image``, or ``None``. Never raises.

    The first non-empty line, stripped at its ends, must be a pinned reference
    whole; nothing inside it is removed. A malformed lock reads as no lock. The
    wrappers apply the same rule, so they never disagree about which image runs.
    """
    try:
        raw = lock_file(project_dir).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    match = PINNED_REF.match(_lock_line(raw))
    return Pin(version=match.group("tag")[1:], digest=match.group("digest")) if match else None


def lock_state(project_dir=".") -> "tuple[str, Optional[Pin]]":
    """``("absent" | "unreadable" | "valid", pin)``: a lock present but unusable is not "no lock"."""
    path = lock_file(project_dir)
    if not path.exists():
        return "absent", None
    pin = read_lock(project_dir)
    return ("valid", pin) if pin is not None else ("unreadable", None)


def write_lock(pin: Pin, project_dir=".") -> Path:
    path = lock_file(project_dir)
    atomic_write_text(path, pin.ref + "\n", mode=0o644)
    return path


def allow_unverified_from_env(env=None) -> bool:
    env = os.environ if env is None else env
    return str(env.get(ALLOW_UNVERIFIED_ENV) or "").strip().lower() in {"1", "true", "yes"}


def signer_identity(version: str) -> str:
    return f"https://github.com/{GITHUB_REPOSITORY}/{WORKFLOW_PATH}@refs/tags/v{version}"


def asset_url(version: str, name: str) -> str:
    return f"https://github.com/{GITHUB_REPOSITORY}/releases/download/v{version}/{name}"


# --- fetching -----------------------------------------------------------------


class _AssetRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a release download only to GitHub's own asset hosts, over https."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme != "https" or parsed.hostname not in ASSET_HOSTS:
            raise urllib.error.HTTPError(newurl, code, "redirect to an unexpected host", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_AssetRedirects)


def fetch_asset(url: str, limit: int, *, opener=None, timeout: float = FETCH_TIMEOUT) -> bytes:
    """Download one release asset, bounded. Any failure is ``unavailable``."""
    opener = opener or _OPENER
    request = urllib.request.Request(url, headers={"User-Agent": "composer-image-pin"})
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise PinError(f"{url} does not exist (release not published, or published unsigned).",
                           unavailable=True) from exc
        raise PinError(f"GitHub answered {exc.code} for {url}.", unavailable=True) from exc
    except (OSError, ValueError) as exc:
        raise PinError(f"GitHub could not be reached for {url}: {exc}", unavailable=True) from exc
    if len(body) > limit:
        raise PinError(f"{url} is larger than {limit} bytes; refusing it.")
    return body


# --- verifying ----------------------------------------------------------------


def _sigstore_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("sigstore") is not None


def verify_bundle(statement_path: str, bundle_path: str, version: str, *, runner=subprocess.run,
                  available: Callable[[], bool] = _sigstore_available) -> None:
    """Refuse unless the bundle is release.yml's signature on that tag over the statement.

    Online first, so Sigstore key rotations reach us. Then ``--offline`` against
    the trust root that ships with sigstore, so a Sigstore outage alone does not
    block updates. The bundle carries its own transparency-log proof either way.
    """
    if not available():
        raise PinError("Sigstore verification is unavailable in this Composer image.", unavailable=True)
    base = [
        sys.executable, "-m", "sigstore", "verify", "identity",
        "--bundle", bundle_path,
        "--cert-identity", signer_identity(version),
        "--cert-oidc-issuer", OIDC_ISSUER,
    ]
    details = []
    for extra in ([], ["--offline"]):
        try:
            proc = runner(base + extra + [statement_path], capture_output=True, text=True,
                          timeout=VERIFY_TIMEOUT, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            details.append(str(exc))
            continue
        if proc.returncode == 0:
            return
        lines = (proc.stderr or proc.stdout or "").strip().splitlines()
        details.append(lines[-1] if lines else f"exit {proc.returncode}")
    flat = "; ".join(detail for detail in details if detail)
    raise PinError(
        f"The signature on Composer {version}'s digest statement does not verify against "
        f"{signer_identity(version)}" + (f" ({flat[:300]})" if flat else "") + "."
    )


def check_statement(raw: bytes, version: str) -> str:
    """The digest a verified statement names, after checking it describes ``version``."""
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PinError(f"Composer {version}'s digest statement is not JSON.") from exc
    if not isinstance(document, dict) or document.get("schema") != STATEMENT_SCHEMA:
        raise PinError(f"Composer {version}'s digest statement has an unknown schema.")
    expected = {"image": REPOSITORY, "version": version, "tag": f"v{version}"}
    for key, value in expected.items():
        if document.get(key) != value:
            raise PinError(
                f"Composer {version}'s digest statement names {key} {document.get(key)!r}, not {value!r}."
            )
    if bool(document.get("prerelease")) != versions.is_prerelease(version):
        raise PinError(f"Composer {version}'s digest statement disagrees about being a prerelease.")
    digest = str(document.get("digest") or "")
    if not DIGEST_RE.match(digest):
        raise PinError(f"Composer {version}'s digest statement carries no sha256 digest.")
    return digest


def verified_pin(version: str, *, fetch=fetch_asset, runner=subprocess.run,
                 available: Callable[[], bool] = _sigstore_available) -> Pin:
    """Download, verify and read ``version``'s signed statement."""
    statement = fetch(asset_url(version, STATEMENT_NAME), MAX_STATEMENT_BYTES)
    bundle = fetch(asset_url(version, BUNDLE_NAME), MAX_BUNDLE_BYTES)
    with tempfile.TemporaryDirectory(prefix="composer-pin-") as holder:
        statement_path = os.path.join(holder, STATEMENT_NAME)
        bundle_path = os.path.join(holder, BUNDLE_NAME)
        Path(statement_path).write_bytes(statement)
        Path(bundle_path).write_bytes(bundle)
        verify_bundle(statement_path, bundle_path, version, runner=runner, available=available)
    return Pin(version=version, digest=check_statement(statement, version))


# --- resolving ----------------------------------------------------------------


@dataclass(frozen=True)
class Resolution:
    """What the channel asks for. ``pin`` is None when there is none yet and none to take."""

    pin: Optional[Pin]
    current: Optional[Pin]
    changed: bool
    note: str = ""


#: The first release whose GitHub Release carries a signed digest statement.
#: It is also the first that can run a pinned stack: an older one has no
#: ``org.dlux.composer.image-pin`` label, cannot read a digest-pinned block and
#: never moves a pin, so the wrapper would run it from the lock for good.
SIGNED_SINCE = "1.11.0b1"


def predates_signing(version: str) -> bool:
    return versions.parse(version) < versions.parse(SIGNED_SINCE)


#: What to do instead of pinning a release from before `SIGNED_SINCE`.
PREDATES_PINNING_WAY_OUT = (
    "Stay on the beta channel until the stable channel publishes Composer 1.11.0 or later, then run "
    "'./start.sh check --stable'."
)


def predates_pinning_refusal(version: str, channel: str = "") -> str:
    """Why ``version`` is never pinned, and what to do instead."""
    where = f"the {channel} channel's newest release, " if channel else ""
    return (
        f"Composer {version} ({where}from before {SIGNED_SINCE}) cannot run a pinned stack: it predates "
        "signed releases and digest pinning, so it can neither read the pinned Composer block nor move "
        "the pin again, and the wrapper would keep running it from .composer-image. No flag pins it, "
        f"--allow-unverified-composer included. {PREDATES_PINNING_WAY_OUT}"
    )


def channel_version(channel: str, *, label_reader=None, strict: bool = True) -> str:
    """The version the channel's tag(s) advertise. Untrusted: it only picks what to verify.

    ``strict`` refuses a prerelease on the stable channel, which pinning needs;
    a status display passes ``False`` and shows what the tag says.
    """
    label_reader = label_reader or registry.remote_image_version
    channel = channel_config.normalize_channel(channel)
    tags = ["beta", "latest"] if channel == channel_config.BETA else ["latest"]
    seen = []
    for tag in tags:
        value = str(label_reader(f"{REPOSITORY}:{tag}") or "").strip()
        if versions.try_parse(value) is not None:
            seen.append(value)
    if not seen:
        raise PinError(
            f"The published version of the {channel} Composer channel could not be read from the registry.",
            unavailable=True,
        )
    newest = max(seen, key=versions.parse)
    if strict and channel == channel_config.STABLE and versions.is_prerelease(newest):
        raise PinError(f"{REPOSITORY}:latest advertises the prerelease {newest}; the stable channel refuses it.")
    return newest


def _unverified_pin(version: str, *, digest_reader=None) -> Pin:
    digest, reason = (digest_reader or registry.tag_digest)(f"{REPOSITORY}:v{version}")
    if not digest or not DIGEST_RE.match(str(digest)):
        raise PinError(
            f"The registry digest of {REPOSITORY}:v{version} could not be read ({reason or 'unknown'}).",
            unavailable=True,
        )
    return Pin(version=version, digest=str(digest), verified=False)


def _keep(current: Optional[Pin], note: str) -> Resolution:
    return Resolution(pin=current, current=current, changed=False, note=note)


def _order(version: str, baseline: Optional[str]) -> Optional[int]:
    """-1, 0 or 1 for ``version`` against ``baseline``; None when there is no usable baseline."""
    other = versions.try_parse(baseline)
    if other is None:
        return None
    mine = versions.parse(version)
    return (mine > other) - (mine < other)


def resolve(
    channel: str,
    *,
    current: Optional[Pin] = None,
    running: Optional[str] = None,
    allow_downgrade: bool = False,
    allow_unverified: bool = False,
    label_reader=None,
    pin_reader=None,
    digest_reader=None,
) -> Resolution:
    """The pin the channel should run now, or `PinError`.

    A pin moves only when there is something to move to. The baseline is the
    current pin, or, before the first pin, the Composer doing the update
    (``running``):

    - **Newer than the baseline:** a real update. It must verify, and it fails
      closed. Only ``allow_unverified`` turns an unreachable GitHub or Sigstore
      into the release tag's registry digest. A failed signature is never
      accepted.
    - **The baseline itself:** the current pin stays. Before the first pin, the
      release is pinned when it verifies. If it cannot be verified yet, nothing
      changes and the note says why; a forged statement still fails.
    - **Older than the baseline:** nothing to update; a channel tag never moves
      a pin backwards. ``allow_downgrade`` (an explicit switch or
      ``--allow-downgrade``) asks for that move, and it must then verify.
    - **An unreadable channel:** nothing changes, except for an explicit move,
      which fails.

    A release before `SIGNED_SINCE` is never pinned, verified or not: it
    cannot run a pinned stack (`predates_pinning_refusal`). Moving away from a
    lock that names one is an ordinary update.
    """
    try:
        version = channel_version(channel, label_reader=label_reader)
    except PinError as exc:
        if exc.unavailable and not allow_downgrade:
            return _keep(current, f"{exc} The Composer pin is unchanged.")
        raise
    if current is not None and current.version == version:
        return _keep(current, f"Composer {current.short} is already the {channel} channel's release.")
    order = _order(version, current.version if current is not None else running)
    if order is not None and order < 0 and not allow_downgrade:
        if current is not None:
            note = (f"The {channel} channel advertises Composer {version}, older than the pinned "
                    f"{current.version}; a channel tag never moves a pin backwards. Nothing to update.")
        else:
            note = (f"This Composer ({running}) is newer than the {channel} channel's newest release "
                    f"({version}); nothing to pin.")
        return _keep(current, note)
    required = allow_downgrade or order is None or order > 0
    if predates_signing(version):
        refusal = predates_pinning_refusal(version, channel)
        if required:
            raise PinError(refusal, way_out=PREDATES_PINNING_WAY_OUT)
        return _keep(current, f"{refusal} Nothing changed.")
    try:
        pin = (pin_reader or verified_pin)(version)
    except PinError as exc:
        failure = exc
    else:
        return Resolution(pin=pin, current=current, changed=_moved(pin, current))
    if failure.unavailable and allow_unverified:
        pin = _unverified_pin(version, digest_reader=digest_reader)
        return Resolution(pin=pin, current=current, changed=_moved(pin, current), note=(
            f"UNVERIFIED: {failure} Pinned the registry's digest for {pin.tag} because "
            "--allow-unverified-composer was given."
        ))
    if required or not failure.unavailable:
        raise failure
    return _keep(current, f"Composer {version} could not be pinned yet: {failure} Nothing changed.")


def _moved(pin: Pin, current: Optional[Pin]) -> bool:
    return current is None or pin.ref != current.ref


def update_pin(
    project_dir=".",
    *,
    compose_file: str = "",
    allow_downgrade: bool = False,
    allow_unverified: bool = False,
    env=None,
    resolver=None,
    running: Optional[str] = None,
):
    """Resolve the channel's signed release and apply it to the lock and the block.

    Returns ``(resolution, outcome)``; ``outcome`` is ``pin_composer_image``'s
    result, or empty when there is no pin to apply. Raises `PinError`, or `AgentInstallError` when the Compose edit is
    refused. An explicit ``COMPOSER_SELF_IMAGE`` is the operator's own pin, so
    nothing is resolved over it.
    """
    from .agent_installer import pin_composer_image

    env = os.environ if env is None else env
    if str(env.get("COMPOSER_SELF_IMAGE") or "").strip():
        raise PinError("COMPOSER_SELF_IMAGE pins the Composer image explicitly; unset it to follow the channel.")
    if running is None:
        from .version import read_composer_version

        running = read_composer_version()
    channel = channel_config.read_channel(project_dir)
    resolution = (resolver or resolve)(
        channel,
        current=read_lock(project_dir),
        running=running,
        allow_downgrade=allow_downgrade,
        allow_unverified=allow_unverified or allow_unverified_from_env(env),
    )
    if resolution.pin is None:
        return resolution, {"applied": False, "files": [], "command": "", "lock_changed": False}
    outcome = pin_composer_image(project_dir, pin=resolution.pin, compose_file=compose_file, apply=True)
    return resolution, outcome
