"""The egress relay: the agent makes the outbound calls, nothing else does.

In a generated stack only ``composer-agent`` can reach the internet. ``web`` and
``celery`` ask it, over the runtime volume, to perform a *named operation*:

    celery -> state/relay/requests/<uuid>.json
    agent  -> state/relay/results/<uuid>.json      (web and celery read it)

The request names an operation and its parameters. It never carries a URL, a
header or a command, so a compromised application can only ask for what an
operation already allows. Operations come from two places: the ones Composer
ships (``BUILTIN_OPERATIONS``) and the ones a project declares in
``relay/operations.json`` in its own directory. The agent already mounts the
project directory read-only and the application services do not, so application
code cannot widen its own network access; and a declared operation runs only
while ``relay/operations.lock`` pins its exact digest (``composer relay approve``).

Secrets (an API key, a token) are sealed by DjangoLux to a public key only this
agent holds. The volume never carries one in the clear.

Every call is bounded: https to one pinned host on 443, verified TLS, no
redirects, an address check after resolving (no loopback, private, link-local or
metadata address), a response size cap, a content-type allowlist, a timeout, a
rate limit, and a request expiry. See ``docs/relay.md``.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

from .agent_protocol import redact_text

SCHEMA_VERSION = 1
ALGORITHM = "x25519-hkdf-sha256-chacha20poly1305"
KDF_INFO = b"composer-relay-v1"

MAX_REQUEST_BYTES = 64 * 1024
MAX_DECLARATION_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_RESULT_BYTES = MAX_RESPONSE_BYTES + 64 * 1024
MAX_REQUEST_LIFETIME = timedelta(seconds=120)
RESULT_RETENTION = 300
PROCESSED_RETENTION = 3600
MAX_PENDING = 64
PER_TICK = 8
PUBLISH_INTERVAL = 60.0

OPERATIONS_FILE = "operations.json"
LOCK_FILE = "operations.lock"

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}(\.[a-z][a-z0-9_]{0,31}){1,3}$")
PARAM_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
HOST_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
PATH_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/{}-]*$")
HEADER_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
QUERY_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")
FIELD_PATH_RE = re.compile(r"^([A-Za-z0-9_]+|\[\])(\.([A-Za-z0-9_]+|\[\]))*$")
SECRET_RE = re.compile(r"^[\x21-\x7e]{1,512}$")
SAFE_HEADERS = {"user-agent", "accept", "accept-language"}
FORBIDDEN_AUTH_HEADERS = {
    "host", "content-length", "content-type", "transfer-encoding", "connection",
    "user-agent", "accept", "accept-language", "accept-encoding", "cookie", "upgrade",
}
DEFAULT_CONTENT_TYPES = {
    "json": ["application/json"],
    "text": ["text/html", "text/plain", "text/xml", "application/xml", "application/xhtml+xml"],
}

OPERATION_KEYS = {"name", "method", "url", "params", "headers", "auth", "response", "timeout", "rate"}
PARAM_KEYS = {"type", "in", "required", "max", "min", "pattern"}
RESPONSE_KEYS = {"type", "fields", "max_bytes", "content_types"}

#: Operations Composer itself ships. Same shape as a declared operation and
#: parsed by the same code; they are trusted by being part of this release, so
#: no lock is needed. DjangoLux features (weather) are added here.
BUILTIN_OPERATIONS: List[Dict[str, Any]] = [
    {
        "name": "weather.geocode",
        "url": "https://api.openweathermap.org/geo/1.0/direct",
        "params": {
            "q": {
                "type": "string",
                "max": 120,
                "pattern": "^.{2,120}$"
            },
            "limit": {
                "type": "integer",
                "min": 1,
                "max": 10,
                "required": False
            }
        },
        "auth": {
            "placement": "query",
            "name": "appid"
        },
        "response": {
            "type": "json",
            "fields": [
                "[].name",
                "[].state",
                "[].country",
                "[].lat",
                "[].lon"
            ],
            "max_bytes": 131072
        },
        "timeout": 4,
        "rate": {
            "per_minute": 30
        }
    },
    {
        "name": "weather.current",
        "url": "https://api.openweathermap.org/data/2.5/weather",
        "params": {
            "lat": {
                "type": "number",
                "min": -90,
                "max": 90
            },
            "lon": {
                "type": "number",
                "min": -180,
                "max": 180
            },
            "units": {
                "type": "string",
                "max": 8,
                "pattern": "^(metric|imperial|standard)$",
                "required": False
            },
            "lang": {
                "type": "string",
                "max": 8,
                "pattern": "^[a-z]{2}([_-][a-zA-Z]{2,4})?$",
                "required": False
            }
        },
        "auth": {
            "placement": "query",
            "name": "appid"
        },
        "response": {
            "type": "json",
            "fields": [
                "main.temp",
                "main.feels_like",
                "dt",
                "weather.0.id",
                "weather.0.icon",
                "weather.0.description"
            ],
            "max_bytes": 131072
        },
        "timeout": 4,
        "rate": {
            "per_minute": 60
        }
    }
]


class OperationError(ValueError):
    """A declaration is not acceptable; the message says which rule it broke."""


class RelayFailure(Exception):
    """A request could not be completed. ``code`` is the stable reason."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Param:
    name: str
    kind: str
    where: str
    required: bool
    maximum: Optional[float]
    minimum: Optional[float]
    pattern: Optional[re.Pattern]


@dataclass(frozen=True)
class Operation:
    name: str
    host: str
    path: str
    params: Dict[str, Param]
    headers: Dict[str, str]
    auth: Optional[Dict[str, str]]
    response_type: str
    fields: Tuple[str, ...]
    max_bytes: int
    content_types: Tuple[str, ...]
    timeout: float
    per_minute: int
    digest: str
    builtin: bool = False
    raw: Dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def describe(self) -> Dict[str, Any]:
        return {
            "host": self.host,
            "response": self.response_type,
            "auth": bool(self.auth),
            "params": sorted(self.params),
            "max_bytes": self.max_bytes,
            "per_minute": self.per_minute,
            "builtin": self.builtin,
        }


def operation_digest(raw: Dict[str, Any]) -> str:
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise OperationError(message)


def _int_in(value: Any, low: int, high: int, default: int, label: str) -> int:
    if value is None:
        return default
    _require(isinstance(value, int) and not isinstance(value, bool) and low <= value <= high,
             f"{label} must be an integer from {low} to {high}")
    return value


def parse_operation(raw: Any, *, builtin: bool = False) -> Operation:
    """Validate one declaration strictly and return it, or raise ``OperationError``."""
    _require(isinstance(raw, dict), "an operation must be an object")
    unknown = sorted(set(raw) - OPERATION_KEYS)
    _require(not unknown, f"unknown key(s): {', '.join(unknown)}")
    name = raw.get("name")
    _require(isinstance(name, str) and NAME_RE.match(name),
             "name must be namespaced like 'app.operation' (lowercase letters, digits, underscore)")
    _require(raw.get("method", "GET") == "GET", "only GET is supported")

    url = raw.get("url")
    _require(isinstance(url, str) and url.startswith("https://"), "url must start with https://")
    rest = url[len("https://"):]
    host_part, slash, tail = rest.partition("/")
    path = "/" + tail if slash else ""
    _require(path != "" and PATH_RE.match(path), "url needs a plain path and no query string or fragment")
    _require("?" not in url and "#" not in url and "@" not in host_part, "url must not carry a query, fragment or credentials")
    host = host_part[:-4] if host_part.endswith(":443") else host_part
    _require(":" not in host, "only port 443 is allowed")
    _require(bool(HOST_RE.match(host)) and host == host.lower(), "host must be a lowercase DNS name")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise OperationError("host must be a DNS name, not an IP address")

    params: Dict[str, Param] = {}
    declared = raw.get("params", {})
    _require(isinstance(declared, dict) and len(declared) <= 16, "params must be an object of at most 16 entries")
    for pname, spec in declared.items():
        _require(isinstance(pname, str) and PARAM_RE.match(pname), f"parameter name {pname!r} is not allowed")
        _require(isinstance(spec, dict), f"parameter {pname} must be an object")
        extra = sorted(set(spec) - PARAM_KEYS)
        _require(not extra, f"parameter {pname}: unknown key(s): {', '.join(extra)}")
        kind = spec.get("type")
        _require(kind in {"string", "integer", "number"}, f"parameter {pname}: type must be string, integer or number")
        where = spec.get("in", "query")
        _require(where in {"query", "path"}, f"parameter {pname}: 'in' must be query or path")
        pattern = None
        if spec.get("pattern") is not None:
            _require(kind == "string" and isinstance(spec["pattern"], str) and len(spec["pattern"]) <= 200,
                     f"parameter {pname}: pattern applies to strings and is at most 200 characters")
            try:
                pattern = re.compile(spec["pattern"])
            except re.error as exc:
                raise OperationError(f"parameter {pname}: bad pattern ({exc})") from None
        if kind == "string":
            maximum = _int_in(spec.get("max"), 1, 256, 128, f"parameter {pname}: max")
            minimum = None
        else:
            maximum = spec.get("max")
            minimum = spec.get("min")
            for label, bound in (("max", maximum), ("min", minimum)):
                _require(bound is None or (isinstance(bound, (int, float)) and not isinstance(bound, bool)),
                         f"parameter {pname}: {label} must be a number")
        required = spec.get("required", True)
        _require(isinstance(required, bool), f"parameter {pname}: required must be true or false")
        params[pname] = Param(pname, kind, where, required, maximum, minimum, pattern)

    placeholders = set(re.findall(r"\{([a-z][a-z0-9_]*)\}", path))
    path_params = {n for n, p in params.items() if p.where == "path"}
    _require(placeholders == path_params, "path placeholders and parameters declared 'in': 'path' must match exactly")
    _require(re.sub(r"\{[a-z][a-z0-9_]*\}", "", path).count("{") == 0
             and re.sub(r"\{[a-z][a-z0-9_]*\}", "", path).count("}") == 0, "malformed path placeholder")
    for pname in path_params:
        _require(params[pname].kind == "string" and params[pname].pattern is not None,
                 f"path parameter {pname} must be a string with a pattern")

    headers: Dict[str, str] = {}
    raw_headers = raw.get("headers", {})
    _require(isinstance(raw_headers, dict) and len(raw_headers) <= 4, "headers must be an object of at most 4 entries")
    for hname, hvalue in raw_headers.items():
        _require(isinstance(hname, str) and hname.lower() in SAFE_HEADERS,
                 f"header {hname!r} is not allowed (allowed: User-Agent, Accept, Accept-Language)")
        _require(isinstance(hvalue, str) and 0 < len(hvalue) <= 200 and all(32 <= ord(c) < 127 for c in hvalue),
                 f"header {hname}: value must be printable ASCII, at most 200 characters")
        headers[hname] = hvalue

    auth = raw.get("auth")
    if auth is not None:
        _require(isinstance(auth, dict) and set(auth) <= {"placement", "name"}, "auth takes 'placement' and 'name'")
        placement = auth.get("placement")
        _require(placement in {"bearer", "header", "query"}, "auth placement must be bearer, header or query")
        if placement == "bearer":
            _require("name" not in auth, "bearer auth takes no name")
        elif placement == "header":
            _require(isinstance(auth.get("name"), str) and HEADER_NAME_RE.match(auth["name"])
                     and auth["name"].lower() not in FORBIDDEN_AUTH_HEADERS, "auth header name is not allowed")
        else:
            _require(isinstance(auth.get("name"), str) and QUERY_NAME_RE.match(auth["name"])
                     and auth["name"] not in params, "auth query name is not allowed or collides with a parameter")

    response = raw.get("response")
    _require(isinstance(response, dict), "response is required")
    extra = sorted(set(response) - RESPONSE_KEYS)
    _require(not extra, f"response: unknown key(s): {', '.join(extra)}")
    rtype = response.get("type")
    _require(rtype in {"text", "json"}, "response type must be text or json")
    fields: Tuple[str, ...] = ()
    if rtype == "json":
        listed = response.get("fields")
        _require(isinstance(listed, list) and 1 <= len(listed) <= 32
                 and all(isinstance(f, str) and len(f) <= 120 and FIELD_PATH_RE.match(f) for f in listed),
                 "a json response needs 1 to 32 field paths such as 'main.temp' or '[].name'")
        fields = tuple(listed)
    else:
        _require("fields" not in response, "a text response takes no fields")
    max_bytes = _int_in(response.get("max_bytes"), 1, MAX_RESPONSE_BYTES, 262144, "response max_bytes")
    types = response.get("content_types", DEFAULT_CONTENT_TYPES[rtype])
    _require(isinstance(types, list) and 1 <= len(types) <= 8
             and all(isinstance(t, str) and re.match(r"^[a-z]+/[a-z0-9.+-]+$", t) for t in types),
             "content_types must list media types such as 'application/json'")

    timeout = raw.get("timeout", 10)
    _require(isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and 1 <= timeout <= 15,
             "timeout must be 1 to 15 seconds")
    rate = raw.get("rate", {})
    _require(isinstance(rate, dict) and set(rate) <= {"per_minute"}, "rate takes 'per_minute'")
    per_minute = _int_in(rate.get("per_minute"), 1, 120, 30, "rate per_minute")

    return Operation(
        name=name, host=host, path=path, params=params, headers=headers, auth=auth,
        response_type=rtype, fields=fields, max_bytes=max_bytes, content_types=tuple(types),
        timeout=float(timeout), per_minute=per_minute, digest=operation_digest(raw),
        builtin=builtin, raw=raw,
    )


def _read_bounded_json(path: Path, limit: int) -> Any:
    try:
        if path.stat().st_size > limit:
            raise OperationError(f"{path.name} is larger than {limit // 1024} KiB")
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise OperationError(f"{path.name} is not readable JSON ({exc.__class__.__name__})") from None


@dataclass
class Catalog:
    """The operations in force, and why any declared one is not."""

    operations: Dict[str, Operation] = field(default_factory=dict)
    problems: List[Dict[str, str]] = field(default_factory=list)
    unapproved: List[str] = field(default_factory=list)
    signature: str = ""


def read_lock(relay_dir: Path) -> Dict[str, str]:
    try:
        data = _read_bounded_json(relay_dir / LOCK_FILE, MAX_DECLARATION_BYTES)
    except OperationError:
        return {}
    ops = data.get("operations") if isinstance(data, dict) else None
    return {str(k): str(v) for k, v in ops.items()} if isinstance(ops, dict) else {}


def load_declared(relay_dir: Path) -> Tuple[Dict[str, Operation], List[Dict[str, str]]]:
    """Parse ``relay/operations.json``: valid operations, and the problems found."""
    operations: Dict[str, Operation] = {}
    problems: List[Dict[str, str]] = []
    try:
        data = _read_bounded_json(relay_dir / OPERATIONS_FILE, MAX_DECLARATION_BYTES)
    except OperationError as exc:
        return {}, [{"name": "", "reason": str(exc)}]
    if data is None:
        return {}, []
    listed = data.get("operations") if isinstance(data, dict) and data.get("schema_version") == SCHEMA_VERSION else None
    if not isinstance(listed, list):
        return {}, [{"name": "", "reason": "operations.json needs schema_version 1 and an 'operations' list"}]
    for raw in listed[:64]:
        try:
            operation = parse_operation(raw)
        except OperationError as exc:
            problems.append({"name": str(raw.get("name", "")) if isinstance(raw, dict) else "", "reason": str(exc)})
            continue
        if operation.name in operations:
            problems.append({"name": operation.name, "reason": "declared twice"})
            continue
        operations[operation.name] = operation
    return operations, problems


def build_catalog(relay_dir: Optional[Path]) -> Catalog:
    catalog = Catalog()
    for raw in BUILTIN_OPERATIONS:
        operation = parse_operation(raw, builtin=True)
        catalog.operations[operation.name] = operation
    if relay_dir is None:
        return catalog
    declared, catalog.problems = load_declared(relay_dir)
    lock = read_lock(relay_dir)
    for name, operation in declared.items():
        if name in catalog.operations:
            catalog.problems.append({"name": name, "reason": "clashes with a built-in operation"})
        elif lock.get(name) != operation.digest:
            catalog.unapproved.append(name)
        else:
            catalog.operations[name] = operation
    catalog.signature = hashlib.sha256(json.dumps(
        [sorted(catalog.operations), sorted(catalog.unapproved), catalog.problems], sort_keys=True,
    ).encode()).hexdigest()
    return catalog


# --- sealed secrets ---------------------------------------------------------

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(value: Any) -> bytes:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("not base64")
    return base64.b64decode(value.encode("ascii"), validate=True)


def _key_id(public_raw: bytes) -> str:
    return hashlib.sha256(public_raw).hexdigest()[:16]


class KeyStore:
    """The agent's private relay keys. Lives in the agent-only state volume."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._keys: Dict[str, bytes] = {}
        self.current_id = ""
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for item in data.get("keys", []):
                self._keys[str(item["key_id"])] = _unb64(item["private"])
            self.current_id = str(data.get("current") or "")
        except (OSError, ValueError, KeyError, TypeError):
            self._keys, self.current_id = {}, ""
        if self.current_id not in self._keys:
            self._generate()

    def _generate(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

        private = X25519PrivateKey.generate()
        raw = private.private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption(),
        )
        public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.current_id = _key_id(public)
        self._keys[self.current_id] = raw
        self._save()

    def _save(self) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "current": self.current_id,
            "keys": [{"key_id": k, "private": _b64(v)} for k, v in self._keys.items()],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def public_document(self) -> Dict[str, Any]:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

        private = X25519PrivateKey.from_private_bytes(self._keys[self.current_id])
        public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return {
            "schema_version": SCHEMA_VERSION,
            "algorithm": ALGORITHM,
            "key_id": self.current_id,
            "public_key": _b64(public),
            "published_at": _now(),
        }

    def open(self, envelope: Any, operation_id: str, operation: str) -> str:
        """Decrypt a sealed secret bound to this request. Raises ``ValueError``."""
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        if not isinstance(envelope, dict) or set(envelope) != {"key_id", "epk", "nonce", "ct"}:
            raise ValueError("malformed sealed secret")
        raw_private = self._keys.get(str(envelope["key_id"]))
        if raw_private is None:
            raise ValueError("sealed to an unknown key")
        epk, nonce, ciphertext = _unb64(envelope["epk"]), _unb64(envelope["nonce"]), _unb64(envelope["ct"])
        if len(epk) != 32 or len(nonce) != 12 or not 16 < len(ciphertext) <= 1024:
            raise ValueError("malformed sealed secret")
        private = X25519PrivateKey.from_private_bytes(raw_private)
        public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        shared = private.exchange(X25519PublicKey.from_public_bytes(epk))
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=epk + public, info=KDF_INFO).derive(shared)
        try:
            plain = ChaCha20Poly1305(key).decrypt(nonce, ciphertext, f"{operation_id}\0{operation}".encode())
        except InvalidTag:
            raise ValueError("sealed secret does not belong to this request") from None
        secret = plain.decode("utf-8")
        if not SECRET_RE.match(secret):
            raise ValueError("secret has unusable characters")
        return secret


# --- the call ---------------------------------------------------------------

def public_address(host: str) -> ipaddress._BaseAddress:
    """Resolve once and refuse anything that is not a public address."""
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise RelayFailure("network", "name resolution failed") from None
    addresses = []
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%")[0])
        if address.version == 6 and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        addresses.append(address)
    if not addresses:
        raise RelayFailure("network", "name resolution failed")
    for address in addresses:
        if not address.is_global or address.is_multicast:
            raise RelayFailure("blocked", "the host resolves to a non-public address")
    return addresses[0]


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connects to the address already vetted, verifying TLS for the real host name."""

    def __init__(self, host: str, address: ipaddress._BaseAddress, timeout: float):
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        sock = socket.create_connection((str(self._address), self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _project(node: Any, tokens: List[str]) -> Any:
    if not tokens:
        if isinstance(node, str):
            return node[:2000]
        return node if node is None or isinstance(node, (bool, int, float)) else None
    head, rest = tokens[0], tokens[1:]
    if head == "[]":
        return [_project(item, rest) for item in node[:100]] if isinstance(node, list) else None
    if isinstance(node, dict):
        return _project(node.get(head), rest)
    if isinstance(node, list) and head.isdigit() and int(head) < len(node):
        return _project(node[int(head)], rest)
    return None


def project_fields(data: Any, fields: Tuple[str, ...]) -> Dict[str, Any]:
    return {path: _project(data, path.split(".")) for path in fields}


def check_param(param: Param, value: Any) -> str:
    """Validate one parameter value and return its text form."""
    if param.kind == "string":
        if not isinstance(value, str) or not value or len(value) > int(param.maximum or 128):
            raise RelayFailure("invalid", f"parameter {param.name} is not an acceptable string")
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise RelayFailure("invalid", f"parameter {param.name} has control characters")
        if param.pattern is not None and not param.pattern.fullmatch(value):
            raise RelayFailure("invalid", f"parameter {param.name} does not match its pattern")
        return value
    kind = int if param.kind == "integer" else (int, float)
    if isinstance(value, bool) or not isinstance(value, kind):
        raise RelayFailure("invalid", f"parameter {param.name} must be a {param.kind}")
    if (param.maximum is not None and value > param.maximum) or (param.minimum is not None and value < param.minimum):
        raise RelayFailure("invalid", f"parameter {param.name} is out of range")
    return repr(value) if isinstance(value, float) else str(value)


def perform(
    operation: Operation,
    params: Dict[str, Any],
    secret: Optional[str] = None,
    *,
    resolver: Callable[[str], Any] = public_address,
    connection_factory: Callable[..., Any] = PinnedHTTPSConnection,
) -> Dict[str, Any]:
    """Make the call and return the result body fields. Raises ``RelayFailure``."""
    unknown = sorted(set(params) - set(operation.params))
    if unknown:
        raise RelayFailure("invalid", f"unknown parameter(s): {', '.join(unknown)}")
    values: Dict[str, str] = {}
    for name, param in operation.params.items():
        if name not in params:
            if param.required:
                raise RelayFailure("invalid", f"parameter {name} is required")
            continue
        values[name] = check_param(param, params[name])
    if operation.auth and not secret:
        raise RelayFailure("credentials", "this operation needs a secret")
    if secret and not operation.auth:
        raise RelayFailure("invalid", "this operation takes no secret")

    path = operation.path
    for name in [n for n, p in operation.params.items() if p.where == "path"]:
        path = path.replace("{" + name + "}", quote(values[name], safe=""))
    query = [(n, v) for n, v in values.items() if operation.params[n].where == "query"]
    headers = {"Accept-Encoding": "identity", "Connection": "close", "User-Agent": "composer-relay/1"}
    headers.update(operation.headers)
    if operation.auth:
        placement = operation.auth["placement"]
        if placement == "bearer":
            headers["Authorization"] = f"Bearer {secret}"
        elif placement == "header":
            headers[operation.auth["name"]] = str(secret)
        else:
            query.append((operation.auth["name"], str(secret)))
    target = path + ("?" + urlencode(query) if query else "")

    address = resolver(operation.host)
    connection = connection_factory(operation.host, address, operation.timeout)
    try:
        try:
            connection.request("GET", target, headers=headers)
            response = connection.getresponse()
            status = response.status
            content_type = (response.getheader("Content-Type") or "").split(";")[0].strip().lower()
            body = response.read(operation.max_bytes + 1)
        except (OSError, http.client.HTTPException):
            raise RelayFailure("network", "the request failed") from None
    finally:
        connection.close()
    if status in (401, 403):
        raise RelayFailure("credentials", f"the server answered {status}")
    if not 200 <= status < 300:
        raise RelayFailure("provider", f"the server answered {status}")
    allowed = set(operation.content_types) | (
        {content_type} if operation.response_type == "json" and content_type.endswith("+json") else set()
    )
    if content_type not in allowed:
        raise RelayFailure("response", "the server sent an unexpected content type")
    if len(body) > operation.max_bytes:
        raise RelayFailure("response", "the response is larger than the operation allows")
    if operation.response_type == "json":
        try:
            data = project_fields(json.loads(body), operation.fields)
        except ValueError:
            raise RelayFailure("response", "the response is not valid JSON") from None
        return {"http_status": status, "content_type": content_type, "bytes": len(body), "data": data}
    return {
        "http_status": status, "content_type": content_type, "bytes": len(body),
        "data": body.decode("utf-8", "replace"),
    }


# --- the spool --------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _aware(value: Any) -> datetime:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        raise RelayFailure("invalid", "timestamps must be ISO 8601") from None
    if moment.tzinfo is None:
        raise RelayFailure("invalid", "timestamps must carry a time zone")
    return moment


def _atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class RelayResponder:
    """Answers relay requests from the runtime volume. ``answer()`` never raises."""

    def __init__(
        self,
        state_dir: Path,
        agent_state_dir: Path,
        relay_dir: Optional[Path],
        *,
        composer_version: str = "",
        perform_call: Callable[..., Dict[str, Any]] = perform,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.root = Path(state_dir) / "relay"
        self.relay_dir = Path(relay_dir) if relay_dir else None
        self.keys = KeyStore(Path(agent_state_dir) / "relay-keys.json")
        self.composer_version = composer_version
        self._perform = perform_call
        self._clock = clock
        self._monotonic = monotonic
        self._calls: Dict[str, deque] = {}
        self.stats: Dict[str, Dict[str, Any]] = {}
        self.catalog = Catalog()
        self._published_signature = None
        self._next_publish = 0.0

    # paths
    @property
    def requests(self) -> Path:
        return self.root / "requests"

    @property
    def results(self) -> Path:
        return self.root / "results"

    @property
    def processed(self) -> Path:
        return self.root / "processed"

    def answer(self) -> int:
        try:
            return self._answer()
        except Exception as exc:  # noqa: BLE001 - the watch loop must survive a relay fault
            # The class only: an exception's text can carry a request URL, and a
            # query-string key is not something the redactor can recognise.
            print(f"⚠ relay failed: {type(exc).__name__}", flush=True)
            return 0

    def _answer(self) -> int:
        for directory in (self.requests, self.results, self.processed):
            directory.mkdir(parents=True, exist_ok=True)
        self.catalog = build_catalog(self.relay_dir)
        self._publish()
        pending = sorted(self.requests.glob("*.json"), key=lambda p: (p.stat().st_mtime, p.name))
        handled = 0
        for index, path in enumerate(pending):
            if index < PER_TICK:
                self._process(path)
            elif index >= MAX_PENDING:
                self._reject(path, "limit", "too many requests are waiting")
            else:
                continue
            handled += 1
        self._sweep()
        return handled

    def _publish(self) -> None:
        due = self._monotonic() >= self._next_publish
        if not due and self.catalog.signature == self._published_signature:
            return
        _atomic_json(self.root / "public-key.json", self.keys.public_document())
        _atomic_json(self.root / "capabilities.json", {
            "schema_version": SCHEMA_VERSION,
            "composer": self.composer_version,
            "algorithm": ALGORITHM,
            "key_id": self.keys.current_id,
            "operations": {name: op.describe() for name, op in sorted(self.catalog.operations.items())},
            "unapproved": sorted(self.catalog.unapproved),
            "problems": self.catalog.problems,
            "updated_at": _now(),
        })
        _atomic_json(self.root / "stats.json", {"schema_version": SCHEMA_VERSION, "operations": self.stats, "updated_at": _now()})
        self._published_signature = self.catalog.signature
        self._next_publish = self._monotonic() + PUBLISH_INTERVAL

    def _write_result(self, operation_id: str, digest: str, status: str, **fields: Any) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION, "operation_id": operation_id, "request_digest": digest,
            "status": status, "completed_at": _now(), **fields,
        }
        if len(json.dumps(payload)) > MAX_RESULT_BYTES:
            payload = {**{k: payload[k] for k in ("schema_version", "operation_id", "request_digest", "completed_at")},
                       "status": "error", "error": "response", "detail": "the answer is too large to return"}
        _atomic_json(self.results / f"{operation_id}.json", payload)

    def _archive(self, path: Path) -> None:
        try:
            os.replace(path, self.processed / path.name)
        except OSError:
            path.unlink(missing_ok=True)

    def _reject(self, path: Path, code: str, detail: str) -> None:
        stem = path.stem
        try:
            uuid.UUID(stem)
            self._write_result(str(uuid.UUID(stem)), "", "rejected", error=code, detail=detail)
        except ValueError:
            pass
        self._archive(path)

    def _count(self, name: str, ok: bool, size: int = 0) -> None:
        entry = self.stats.setdefault(name, {"calls": 0, "ok": 0, "errors": 0, "bytes": 0, "last_at": ""})
        entry["calls"] += 1
        entry["ok" if ok else "errors"] += 1
        entry["bytes"] += size
        entry["last_at"] = _now()

    def _rate_ok(self, operation: Operation) -> bool:
        window = self._calls.setdefault(operation.name, deque())
        now = self._monotonic()
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= operation.per_minute:
            return False
        window.append(now)
        return True

    def _process(self, path: Path) -> None:
        try:
            raw = path.read_bytes() if path.stat().st_size <= MAX_REQUEST_BYTES else b""
        except OSError:
            return
        digest = hashlib.sha256(raw).hexdigest()
        try:
            operation_id = str(uuid.UUID(path.stem))
        except ValueError:
            self._archive(path)
            return
        name = ""
        try:
            request = json.loads(raw.decode("utf-8")) if raw else None
            if (not isinstance(request, dict) or request.get("schema_version") != SCHEMA_VERSION
                    or request.get("operation_id") != operation_id):
                raise RelayFailure("invalid", "the request is malformed")
            name = str(request.get("op") or "")
            operation = self.catalog.operations.get(name)
            if operation is None:
                raise RelayFailure("unsupported", "no such operation is available")
            expires = _aware(request.get("expires_at"))
            created = _aware(request.get("created_at"))
            now = datetime.fromtimestamp(self._clock(), timezone.utc)
            if expires <= now:
                raise RelayFailure("expired", "the request expired before it was answered")
            if expires - created > MAX_REQUEST_LIFETIME:
                raise RelayFailure("invalid", "the request lifetime is too long")
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise RelayFailure("invalid", "params must be an object")
            secret = None
            if request.get("sealed") is not None:
                if not operation.auth:
                    raise RelayFailure("invalid", "this operation takes no secret")
                try:
                    secret = self.keys.open(request["sealed"], operation_id, name)
                except (ValueError, KeyError, TypeError):
                    raise RelayFailure("credentials", "the sealed secret could not be opened") from None
            if not self._rate_ok(operation):
                raise RelayFailure("limit", "this operation is being called too often")
            outcome = self._perform(operation, params, secret)
        except RelayFailure as failure:
            if name in self.catalog.operations:
                self._count(name, False)
            status = "rejected" if failure.code in {"invalid", "unsupported", "expired", "limit"} else "error"
            self._write_result(operation_id, digest, status, error=failure.code, detail=redact_text(failure.detail)[:300])
        except Exception as exc:  # noqa: BLE001 - one bad request must not stop the rest
            print(f"⚠ relay request {operation_id} failed: {type(exc).__name__}", flush=True)
            self._write_result(operation_id, digest, "error", error="provider", detail="the call could not be completed")
        else:
            self._count(operation.name, True, outcome.get("bytes", 0))
            self._write_result(operation_id, digest, "ok", **outcome)
        self._archive(path)

    def _sweep(self) -> None:
        now = self._clock()
        for directory, keep in ((self.results, RESULT_RETENTION), (self.processed, PROCESSED_RETENTION)):
            for path in directory.glob("*.json"):
                try:
                    if now - path.stat().st_mtime > keep:
                        path.unlink()
                except OSError:
                    continue
