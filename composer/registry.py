"""Minimal OCI/Docker registry v2 client — just enough to read the current
digest of a tag without pulling the image.

Used by `composer watch` to detect that a new application image has been
published (remote tag digest != the locally-pulled digest) and publish that
availability for another process (e.g. dlux) to surface as "update available".

Supports the standard Bearer token challenge flow, so it works with Docker Hub
and most registries. A static bearer token may be supplied via
``COMPOSER_REGISTRY_TOKEN`` for private repositories. Any failure returns
``None`` (treated as "unknown", never a false positive).
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional, Tuple

DOCKER_HUB_REGISTRY = "registry-1.docker.io"

# Accept both OCI and Docker manifest (and index/list) media types so the
# registry returns the tag's canonical digest in Docker-Content-Digest.
_MANIFEST_ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])

# Single-arch image manifest (used when descending from a multi-arch index) and
# the image config blob (which carries the labels we read the version from).
_IMAGE_MANIFEST_ACCEPT = ", ".join([
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])
_CONFIG_ACCEPT = ", ".join([
    "application/vnd.oci.image.config.v1+json",
    "application/vnd.docker.container.image.v1+json",
    "application/json",
])

# Default OCI label GitHub Actions' docker/metadata-action stamps with the
# release version. Override with COMPOSER_VERSION_LABEL for a custom label.
_DEFAULT_VERSION_LABEL = "org.opencontainers.image.version"


def parse_image_ref(ref: str) -> Tuple[str, str, str]:
    """Split an image reference into (registry, repository, tag).

    Defaults to Docker Hub, prefixing single-name repos with ``library/``.
    Any ``@digest`` suffix is stripped.
    """
    ref = str(ref or "").split("@", 1)[0]
    registry = DOCKER_HUB_REGISTRY
    remainder = ref
    if "/" in ref:
        first, rest = ref.split("/", 1)
        if "." in first or ":" in first or first == "localhost":
            registry, remainder = first, rest
    last = remainder.rsplit("/", 1)[-1]
    if ":" in last:
        repo, tag = remainder.rsplit(":", 1)
    else:
        repo, tag = remainder, "latest"
    if registry == DOCKER_HUB_REGISTRY and "/" not in repo:
        repo = f"library/{repo}"
    return registry, repo, tag


def _open(url: str, headers: dict, *, method: str = "GET", timeout: float = 15):
    request = urllib.request.Request(url, headers=headers, method=method)
    return urllib.request.urlopen(request, timeout=timeout)


def _bearer_token(challenge: str, timeout: float) -> Optional[str]:
    params = dict(re.findall(r'(\w+)="([^"]*)"', challenge or ""))
    realm = params.get("realm")
    if not realm:
        return None
    query = []
    for key in ("service", "scope"):
        if params.get(key):
            query.append(f"{key}={urllib.parse.quote(params[key])}")
    url = realm + (f"?{'&'.join(query)}" if query else "")
    try:
        with _open(url, {"Accept": "application/json"}, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    return data.get("token") or data.get("access_token")


def _fetch_bytes(url: str, accept: str, token: Optional[str], timeout: float) -> Optional[bytes]:
    """GET ``url`` (with the same 401 Bearer-challenge retry as
    ``remote_tag_digest``) and return the response body, or None. Never raises."""
    headers = {"Accept": accept}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with _open(url, headers, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        if exc.code != 401 or token:
            return None
        bearer = _bearer_token(exc.headers.get("Www-Authenticate", ""), timeout)
        if not bearer:
            return None
        headers["Authorization"] = f"Bearer {bearer}"
        try:
            with _open(url, headers, timeout=timeout) as response:
                return response.read()
        except Exception:
            return None
    except Exception:
        return None


def remote_image_version(
    ref: str, *, token: Optional[str] = None, timeout: float = 15, label: Optional[str] = None
) -> Optional[str]:
    """Best-effort: the remote image's own version from its OCI image label
    (``org.opencontainers.image.version`` by default). Reads the tag manifest,
    descends into a concrete manifest for multi-arch indexes, fetches the image
    config blob, and returns the label value — or None if it can't be determined
    (private/unsupported registry, missing label, network error). Never raises,
    so callers degrade gracefully to the digest when this is unavailable."""
    label = label or _DEFAULT_VERSION_LABEL
    try:
        registry, repo, tag = parse_image_ref(ref)
        base = f"https://{registry}/v2/{repo}"
        raw = _fetch_bytes(f"{base}/manifests/{tag}", _MANIFEST_ACCEPT, token, timeout)
        if not raw:
            return None
        manifest = json.loads(raw.decode("utf-8"))
        # Multi-arch index/list: descend into the first concrete image manifest.
        media = str(manifest.get("mediaType") or "")
        if "index" in media or "manifest.list" in media or (
            "manifests" in manifest and "config" not in manifest
        ):
            child = next(
                (m for m in (manifest.get("manifests") or [])
                 if isinstance(m, dict) and m.get("digest")),
                None,
            )
            if not child:
                return None
            raw = _fetch_bytes(f"{base}/manifests/{child['digest']}", _IMAGE_MANIFEST_ACCEPT, token, timeout)
            if not raw:
                return None
            manifest = json.loads(raw.decode("utf-8"))
        config_digest = (manifest.get("config") or {}).get("digest")
        if not config_digest:
            return None
        raw = _fetch_bytes(f"{base}/blobs/{config_digest}", _CONFIG_ACCEPT, token, timeout)
        if not raw:
            return None
        blob = json.loads(raw.decode("utf-8"))
        labels = {}
        for key in ("config", "container_config"):
            section = blob.get(key)
            if isinstance(section, dict) and isinstance(section.get("Labels"), dict):
                labels.update(section["Labels"])
        version = str(labels.get(label) or "").strip()
        return version or None
    except Exception:
        return None


def remote_tag_digest(ref: str, *, token: Optional[str] = None, timeout: float = 15) -> Optional[str]:
    """Return the registry's current digest (``sha256:...``) for the tag, or
    None if it can't be determined. Never raises."""
    registry, repo, tag = parse_image_ref(ref)
    url = f"https://{registry}/v2/{repo}/manifests/{tag}"
    headers = {"Accept": _MANIFEST_ACCEPT}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def _digest_from(resp) -> Optional[str]:
        return resp.headers.get("Docker-Content-Digest")

    try:
        with _open(url, headers, timeout=timeout) as response:
            return _digest_from(response)
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        if exc.code != 401 or token:
            return None
        bearer = _bearer_token(exc.headers.get("Www-Authenticate", ""), timeout)
        if not bearer:
            return None
        headers["Authorization"] = f"Bearer {bearer}"
        try:
            with _open(url, headers, timeout=timeout) as response:
                return _digest_from(response)
        except Exception:
            return None
    except Exception:
        return None
