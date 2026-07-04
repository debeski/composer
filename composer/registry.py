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
