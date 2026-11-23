"""Does the stack's proxy refuse requests other sites make in the background?

DjangoLux's scaffold carries a Fetch Metadata resource isolation policy
(web.dev/articles/fetch-metadata) in both proxy files, under one name:

- ``.proxy/Caddyfile``: the ``@cross_site_request`` matcher, refused with
  ``respond @cross_site_request 403``;
- ``.proxy/default.conf.template`` (the nginx fallback): a ``map`` that sets
  ``$cross_site_request``, refused with ``if ($cross_site_request) { return 403; }``.

The name is the marker: ``inspect`` sorts each proxy file present into
``present`` (the policy is defined and refused) or ``missing``, so a reworded
comment or a reformatted expression still counts, and a matcher nobody refuses
does not. Composer only reports; the block to copy is in the DjangoLux docs.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict

MARKER = "cross_site_request"
CADDYFILE = Path(".proxy") / "Caddyfile"
NGINX_TEMPLATE = Path(".proxy") / "default.conf.template"
DOCS = "DjangoLux docs/deployment-configuration.md, 'Cross-site requests (Fetch Metadata)'"

PRESENT, MISSING = "present", "missing"

_CADDY_MATCHER = re.compile(r"^[ \t]*@cross_site_request(?:[ \t]|$)", re.M)
_CADDY_REFUSAL = re.compile(r"^[ \t]*(?:respond|error)[ \t]+@cross_site_request[ \t](?:[^#\n]*[ \t])?403(?:[ \t]|$)", re.M)
_NGINX_MAP = re.compile(r"^[ \t]*map[ \t][^#\n{]*\$cross_site_request[ \t]*\{", re.M)
_NGINX_REFUSAL = re.compile(r"^[ \t]*if[ \t]*\([ \t]*\$cross_site_request[ \t]*\)[ \t]*\{[ \t]*return[ \t]+403[ \t]*;", re.M)


def classify(contents: str, kind: str) -> str:
    """``present`` when the file defines the policy and refuses what it matches, else ``missing``."""
    if kind == "caddy":
        found = _CADDY_MATCHER.search(contents) and _CADDY_REFUSAL.search(contents)
    else:
        found = _NGINX_MAP.search(contents) and _NGINX_REFUSAL.search(contents)
    return PRESENT if found else MISSING


def inspect(project_root: Path) -> Dict[str, Any]:
    """``{"files": {relative path: state}, "missing": [relative path, ...]}`` for the proxy files present."""
    files = {}
    for relative, kind in ((CADDYFILE, "caddy"), (NGINX_TEMPLATE, "nginx")):
        path = Path(project_root) / relative
        if path.is_file():
            files[str(relative)] = classify(path.read_text(encoding="utf-8"), kind)
    return {"files": files, "missing": [name for name, state in files.items() if state == MISSING]}
