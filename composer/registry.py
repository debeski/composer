"""Minimal OCI/Docker registry v2 client — just enough to read the current
digest of a tag without pulling the image.

Used by `composer agent watch` to detect that a new application image has been
published (remote tag digest != the locally-pulled digest) and publish that
availability for another process (e.g. dlux) to surface as "update available".

Supports the standard Bearer token challenge flow, so it works with Docker Hub
and most registries, and a registry that asks for Basic auth itself. A private
repository is read with a login (``registry_login``: sent to the token realm the
registry names, or to a Basic-auth registry, over https only) or a static bearer
token (``COMPOSER_REGISTRY_TOKEN``). Any failure returns ``None`` (treated as
"unknown", never a false positive) with a reason the caller may report.
"""

import base64
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

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
DEFAULT_VERSION_LABEL = "org.opencontainers.image.version"


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(RejectRedirects)


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
    return _OPENER.open(request, timeout=timeout)


#: Why a lookup came back empty. ``check`` and the availability document name it;
#: an empty lookup is still "unknown", never a false positive.
LOGIN_REQUIRED = "login-required"
LOGIN_REFUSED = "login-refused"
NOT_FOUND = "not-found"
UNREACHABLE = "unreachable"
CERTIFICATE = "certificate"
FAILED = "failed"

#: How ``agent check`` names each reason.
REASON_TEXT = {
    LOGIN_REQUIRED: "the registry needs a login (COMPOSER_REGISTRY_USERNAME, COMPOSER_REGISTRY_PASSWORD)",
    LOGIN_REFUSED: "the registry refused the login",
    NOT_FOUND: "the registry has no such tag",
    UNREACHABLE: "the registry is unreachable",
    CERTIFICATE: "the registry's certificate is not trusted",
    FAILED: "the registry's answer could not be read",
}

#: A registry login: (username, password or deploy token).
Credentials = Tuple[str, str]


def _basic(credentials: Credentials) -> str:
    user, password = credentials
    return "Basic " + base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")


def _challenge(header: str) -> Tuple[str, Dict[str, str]]:
    scheme, _, rest = (header or "").strip().partition(" ")
    return scheme.lower(), dict(re.findall(r'(\w+)="([^"]*)"', rest))


def _unreachable(exc: OSError) -> str:
    """A certificate this client does not trust, or no answer at all."""
    cause = getattr(exc, "reason", exc)
    return CERTIFICATE if isinstance(cause, ssl.SSLCertVerificationError) else UNREACHABLE


def _refusal(code: int, authenticated: bool) -> str:
    if code in (401, 403):
        return LOGIN_REFUSED if authenticated else LOGIN_REQUIRED
    if code == 404:
        return NOT_FOUND
    return FAILED


def _token(params: Dict[str, str], timeout: float, credentials: Optional[Credentials]) -> Tuple[Optional[str], str]:
    """A pull token from the realm the registry's Bearer challenge names.

    The login goes to that realm only, over https, as Basic auth: the token
    flow of the distribution spec, which GitLab, Docker Hub and GHCR follow.
    ``_open`` refuses redirects, so the realm cannot pass it on.
    """
    realm = params.get("realm")
    if not realm:
        return None, FAILED
    parsed_realm = urllib.parse.urlsplit(realm)
    if parsed_realm.scheme != "https" or not parsed_realm.netloc:
        return None, FAILED
    query = []
    for key in ("service", "scope"):
        if params.get(key):
            query.append(f"{key}={urllib.parse.quote(params[key])}")
    url = realm + (f"?{'&'.join(query)}" if query else "")
    headers = {"Accept": "application/json"}
    if credentials:
        headers["Authorization"] = _basic(credentials)
    try:
        with _open(url, headers, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        return None, _refusal(exc.code, bool(credentials))
    except OSError as exc:
        return None, _unreachable(exc)
    except Exception:
        return None, FAILED
    token = (data.get("token") or data.get("access_token")) if isinstance(data, dict) else None
    if not token:
        return None, LOGIN_REFUSED if credentials else LOGIN_REQUIRED
    return token, ""


def _bearer_token(challenge: str, timeout: float, credentials: Optional[Credentials] = None) -> Optional[str]:
    scheme, params = _challenge(challenge)
    if scheme != "bearer":
        return None
    return _token(params, timeout, credentials)[0]


def _authorization(exc, timeout: float, credentials: Optional[Credentials]) -> Tuple[Optional[str], str]:
    """The Authorization header that answers a 401, or ``(None, reason)``."""
    scheme, params = _challenge(exc.headers.get("Www-Authenticate", ""))
    if scheme == "bearer":
        token, reason = _token(params, timeout, credentials)
        return (f"Bearer {token}", "") if token else (None, reason)
    if scheme == "basic" and credentials:
        return _basic(credentials), ""
    return None, LOGIN_REQUIRED


#: Redirect codes a registry answers a blob GET with.
_REDIRECTS = (301, 302, 303, 307, 308)


def _follow_to_storage(exc, accept: str, timeout: float, hops: int = 3) -> Optional[bytes]:
    """Read the storage URL a registry redirects a blob GET to, or None.

    Docker Hub (and GHCR, and ECR) do not serve blobs themselves: they answer
    with a 307 to a pre-signed CDN URL, so the image config — where the version
    label lives — is unreadable unless that one hop is followed. It is followed
    only to https, and never with the registry's Authorization header attached:
    the pre-signed URL carries its own credentials, and handing a registry the
    ability to forward ours to a host of its choosing is exactly what
    ``RejectRedirects`` exists to prevent. It still does.
    """
    while hops > 0:
        if exc.code not in _REDIRECTS:
            return None
        location = exc.headers.get("Location") or ""
        if not location.lower().startswith("https://"):
            return None
        hops -= 1
        try:
            with _open(location, {"Accept": accept}, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as nested:  # type: ignore[attr-defined]
            exc = nested
        except Exception:
            return None
    return None


def _body(response) -> bytes:
    return response.read()


def _digest(response) -> Optional[str]:
    return response.headers.get("Docker-Content-Digest")


def _get(
    url: str,
    accept: str,
    token: Optional[str],
    timeout: float,
    *,
    credentials: Optional[Credentials] = None,
    follow: bool = False,
    read: Callable[[Any], Any] = _body,
) -> Tuple[Optional[Any], str]:
    """GET ``url`` and ``read`` the response: ``(value, "")`` or ``(None, reason)``. Never raises.

    A 401 is answered once, through the challenge (``_authorization``), unless a
    static ``token`` was already sent. ``follow`` permits the storage redirect a
    blob read needs; a manifest read leaves it off, because there the redirect is
    not part of the protocol.
    """
    headers = {"Accept": accept}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    authenticated = bool(token)
    for attempt in (1, 2):
        try:
            with _open(url, headers, timeout=timeout) as response:
                value = read(response)
            return (value, "") if value is not None else (None, FAILED)
        except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
            if exc.code in _REDIRECTS:
                body = _follow_to_storage(exc, accept, timeout) if follow else None
                return (body, "") if body is not None else (None, FAILED)
            if exc.code != 401 or authenticated or attempt == 2:
                return None, _refusal(exc.code, authenticated)
            authorization, reason = _authorization(exc, timeout, credentials)
            if not authorization:
                return None, reason
            headers["Authorization"] = authorization
            authenticated = bool(credentials)
        except OSError as exc:
            return None, _unreachable(exc)
        except Exception:
            return None, FAILED
    return None, FAILED


def _fetch_bytes(
    url: str,
    accept: str,
    token: Optional[str],
    timeout: float,
    *,
    follow: bool = False,
    credentials: Optional[Credentials] = None,
) -> Optional[bytes]:
    """GET ``url`` and return the response body, or None. Never raises."""
    return _get(url, accept, token, timeout, credentials=credentials, follow=follow)[0]


def remote_image_labels(
    ref: str,
    *,
    token: Optional[str] = None,
    credentials: Optional[Credentials] = None,
    timeout: float = 15,
) -> Optional[Dict[str, object]]:
    """Best-effort remote image labels without pulling the image.

    Reads the tag manifest, descends into a concrete manifest for multi-arch
    indexes, and fetches the image config blob. Returns ``None`` when metadata
    cannot be read and an empty dict when the config contains no labels. Never
    raises, so digest-based availability remains independent of metadata.
    """
    try:
        registry, repo, tag = parse_image_ref(ref)
        base = f"https://{registry}/v2/{repo}"

        def fetch(url: str, accept: str, follow: bool = False) -> Optional[bytes]:
            return _fetch_bytes(url, accept, token, timeout, credentials=credentials, follow=follow)

        raw = fetch(f"{base}/manifests/{tag}", _MANIFEST_ACCEPT)
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
            raw = fetch(f"{base}/manifests/{child['digest']}", _IMAGE_MANIFEST_ACCEPT)
            if not raw:
                return None
            manifest = json.loads(raw.decode("utf-8"))
        config_digest = (manifest.get("config") or {}).get("digest")
        if not config_digest:
            return None
        raw = fetch(f"{base}/blobs/{config_digest}", _CONFIG_ACCEPT, follow=True)
        if not raw:
            return None
        blob = json.loads(raw.decode("utf-8"))
        labels = {}
        for key in ("config", "container_config"):
            section = blob.get(key)
            if isinstance(section, dict) and isinstance(section.get("Labels"), dict):
                labels.update(section["Labels"])
        return labels
    except Exception:
        return None


def remote_image_version(
    ref: str,
    *,
    token: Optional[str] = None,
    credentials: Optional[Credentials] = None,
    timeout: float = 15,
    label: Optional[str] = None,
) -> Optional[str]:
    """Best-effort version from an OCI image label, or ``None``.

    Kept as a small public wrapper for existing callers. Availability checks
    use ``remote_image_labels`` directly so version and release-manifest
    metadata share one registry config lookup.
    """
    labels = remote_image_labels(ref, token=token, credentials=credentials, timeout=timeout)
    version = str((labels or {}).get(label or DEFAULT_VERSION_LABEL) or "").strip()
    return version or None


def tag_digest(
    ref: str,
    *,
    token: Optional[str] = None,
    credentials: Optional[Credentials] = None,
    timeout: float = 15,
) -> Tuple[Optional[str], str]:
    """The registry's current digest (``sha256:...``) for the tag and ``""``, or
    ``None`` and the reason it could not be read. Never raises."""
    try:
        registry, repo, tag = parse_image_ref(ref)
    except Exception:
        return None, FAILED
    return _get(
        f"https://{registry}/v2/{repo}/manifests/{tag}",
        _MANIFEST_ACCEPT,
        token,
        timeout,
        credentials=credentials,
        read=_digest,
    )


def remote_tag_digest(
    ref: str,
    *,
    token: Optional[str] = None,
    credentials: Optional[Credentials] = None,
    timeout: float = 15,
) -> Optional[str]:
    """Return the registry's current digest (``sha256:...``) for the tag, or
    None if it can't be determined. Never raises."""
    return tag_digest(ref, token=token, credentials=credentials, timeout=timeout)[0]
