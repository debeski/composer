"""The login Composer uses for a private ``WEB_IMAGE`` registry.

``COMPOSER_REGISTRY_USERNAME`` and ``COMPOSER_REGISTRY_PASSWORD`` hold a read-only
login, such as a GitLab deploy token with ``read_registry`` or a Docker Hub
access token with read scope. They belong to one registry:
``COMPOSER_REGISTRY_HOST`` when it is set, otherwise the registry ``WEB_IMAGE``
names. Composer sends them nowhere else, so images from other registries (caddy,
postgres) are pulled as before.

Two readers use the login:

* The update check (``registry``) sends it as Basic auth to the token service
  the registry's challenge names, or to the registry itself when the registry
  asks for Basic auth. It goes over https only and never across a redirect.
* Every ``docker compose`` that Composer runs (pull, up, run) gets a private
  ``DOCKER_CONFIG`` holding this one login. The Docker daemon takes registry
  credentials from its client, and the Composer containers have no Docker login
  of their own. The directory belongs to the process (0700, file 0600) and is
  removed when the process exits.
"""

from __future__ import annotations

import atexit
import base64
import json
import os
import shutil
import tempfile
from typing import Dict, Mapping, Optional, Tuple

from .registry import DOCKER_HUB_REGISTRY, Credentials, parse_image_ref

USERNAME = "COMPOSER_REGISTRY_USERNAME"
PASSWORD = "COMPOSER_REGISTRY_PASSWORD"
HOST = "COMPOSER_REGISTRY_HOST"
KEYS = (USERNAME, PASSWORD, HOST)
#: The services that read the login: the update check and the pulls.
REGISTRY_ROLES = ("composer-agent", "composer-executor")

_DOCKER_HUB_ALIASES = {"docker.io", "index.docker.io", "registry-1.docker.io", "registry.hub.docker.com"}
# The key Docker's own login writes for Docker Hub.
_DOCKER_HUB_CONFIG_KEY = "https://index.docker.io/v1/"

_configs: Dict[Tuple[str, str, str], str] = {}


def normalize_host(value: str) -> str:
    host = str(value or "").strip().lower()
    for prefix in ("https://", "http://"):
        if host.startswith(prefix):
            host = host[len(prefix):]
    host = host.split("/", 1)[0]
    return DOCKER_HUB_REGISTRY if host in _DOCKER_HUB_ALIASES else host


def image_host(image: str) -> str:
    return normalize_host(parse_image_ref(image)[0])


def login_host(environ: Optional[Mapping[str, str]] = None) -> str:
    """The registry the login belongs to, or ``""`` when nothing names one."""
    environ = os.environ if environ is None else environ
    host = normalize_host(environ.get(HOST, ""))
    if host:
        return host
    image = str(environ.get("WEB_IMAGE") or "").strip()
    return image_host(image) if image else ""


def credentials(environ: Optional[Mapping[str, str]] = None) -> Optional[Credentials]:
    """The configured login, or None when either half is missing."""
    environ = os.environ if environ is None else environ
    user = str(environ.get(USERNAME) or "").strip()
    password = str(environ.get(PASSWORD) or "")
    return (user, password) if user and password else None


def credentials_for(image: str, environ: Optional[Mapping[str, str]] = None) -> Optional[Credentials]:
    """The login for ``image``, or None when it lives on another registry."""
    login = credentials(environ)
    if not login:
        return None
    host = login_host(environ)
    return login if host and image_host(image) == host else None


def docker_config(environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """A ``DOCKER_CONFIG`` directory holding the login, or None without one.

    It is created once per process for each login and holds nothing but
    ``auths`` for that registry. The process's own environment already carries
    the same login, so the file adds no new holder of it.
    """
    login = credentials(environ)
    host = login_host(environ)
    if not login or not host:
        return None
    key = (host, login[0], login[1])
    path = _configs.get(key)
    if path and os.path.isfile(os.path.join(path, "config.json")):
        return path
    path = tempfile.mkdtemp(prefix="composer-registry-")
    entry = _DOCKER_HUB_CONFIG_KEY if host == DOCKER_HUB_REGISTRY else host
    auth = base64.b64encode(f"{login[0]}:{login[1]}".encode("utf-8")).decode("ascii")
    descriptor = os.open(os.path.join(path, "config.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({"auths": {entry: {"auth": auth}}}, handle)
    _configs[key] = path
    atexit.register(shutil.rmtree, path, True)
    return path
