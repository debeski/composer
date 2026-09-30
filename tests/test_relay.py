import base64
import copy
import hashlib
import json
import os
import socket
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from composer import relay
from composer.relay import (
    Catalog, KeyStore, OperationError, RelayFailure, RelayResponder,
    build_catalog, operation_digest, parse_operation, perform, project_fields, public_address,
)

FIXTURES = Path(__file__).parent / "fixtures"

PAGE = {
    "name": "finance.rates_page",
    "url": "https://rates.example.com/exchange/",
    "headers": {"User-Agent": "Mozilla/5.0 (test)"},
    "response": {"type": "text", "max_bytes": 4096},
}
WEATHER = {
    "name": "weather.current",
    "url": "https://api.example.com/data/2.5/weather",
    "params": {
        "lat": {"type": "number", "min": -90, "max": 90},
        "lon": {"type": "number", "min": -180, "max": 180},
        "lang": {"type": "string", "max": 5, "pattern": "^[a-z]{2}$", "required": False},
    },
    "auth": {"placement": "query", "name": "appid"},
    "response": {"type": "json", "fields": ["main.temp", "weather.0.description", "list.[].name"]},
    "rate": {"per_minute": 3},
}


def seal(public_key_doc, secret, operation_id, op):
    """The sender's half of the sealed-secret format, as DjangoLux implements it."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    recipient = base64.b64decode(public_key_doc["public_key"])
    ephemeral = X25519PrivateKey.generate()
    epk = ephemeral.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(recipient))
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=epk + recipient, info=relay.KDF_INFO).derive(shared)
    nonce = os.urandom(12)
    ciphertext = ChaCha20Poly1305(key).encrypt(nonce, secret.encode(), f"{operation_id}\0{op}".encode())
    b64 = lambda raw: base64.b64encode(raw).decode()
    return {"key_id": public_key_doc["key_id"], "epk": b64(epk), "nonce": b64(nonce), "ct": b64(ciphertext)}


class FakeResponse:
    def __init__(self, status=200, body=b"", content_type="text/html; charset=utf-8"):
        self.status, self._body, self._type = status, body, content_type

    def getheader(self, name):
        return self._type if name.lower() == "content-type" else None

    def read(self, amount=None):
        return self._body if amount is None else self._body[:amount]


class FakeConnection:
    instances = []
    response = FakeResponse()

    def __init__(self, host, address, timeout):
        self.host, self.address, self.timeout, self.sent, self.closed = host, address, timeout, None, False
        FakeConnection.instances.append(self)

    def request(self, method, target, headers=None):
        self.sent = (method, target, dict(headers or {}))

    def getresponse(self):
        return FakeConnection.response

    def close(self):
        self.closed = True


def public_resolver(host):
    import ipaddress
    return ipaddress.ip_address("93.184.216.34")


def call(operation, params=None, secret=None, response=None):
    FakeConnection.instances.clear()
    FakeConnection.response = response or FakeResponse(body=b"<html>ok</html>")
    return perform(operation, params or {}, secret, resolver=public_resolver, connection_factory=FakeConnection)


class ParseOperationTests(unittest.TestCase):
    def bad(self, **change):
        raw = copy.deepcopy(PAGE)
        for key, value in change.items():
            if value is None:
                raw.pop(key, None)
            else:
                raw[key] = value
        with self.assertRaises(OperationError):
            parse_operation(raw)

    def test_valid_operations_parse(self):
        page = parse_operation(PAGE)
        self.assertEqual((page.host, page.path, page.response_type), ("rates.example.com", "/exchange/", "text"))
        weather = parse_operation(WEATHER)
        self.assertEqual(weather.fields, ("main.temp", "weather.0.description", "list.[].name"))
        self.assertTrue(weather.auth and weather.per_minute == 3)

    def test_hosts_must_be_pinned_public_style_dns_names_on_443(self):
        for url in (
            "http://rates.example.com/x", "https://127.0.0.1/x", "https://10.0.0.5/x", "https://[::1]/x",
            "https://localhost/x", "https://Rates.Example.com/x", "https://user@rates.example.com/x",
            "https://rates.example.com:8443/x", "https://rates.example.com/x?a=1", "https://rates.example.com/x#f",
            "https://rates.example.com", "https://*.example.com/x", "https://rates.example.com/a b",
        ):
            with self.subTest(url=url):
                self.bad(url=url)
        parse_operation({**PAGE, "url": "https://rates.example.com:443/x"})

    def test_unknown_keys_methods_and_names_are_refused(self):
        self.bad(method="POST")
        self.bad(surprise=True)
        self.bad(name="Rates")
        self.bad(name="rates")
        self.bad(response={"type": "text", "surprise": 1})
        self.bad(response={"type": "json"})
        self.bad(response={"type": "text", "fields": ["a"]})
        self.bad(response={"type": "text", "max_bytes": relay.MAX_RESPONSE_BYTES + 1})
        self.bad(timeout=60)
        self.bad(rate={"per_minute": 500})

    def test_headers_are_an_allowlist(self):
        self.bad(headers={"Authorization": "Bearer x"})
        self.bad(headers={"Cookie": "a=b"})
        self.bad(headers={"User-Agent": "a\r\nInjected: 1"})

    def test_params_and_path_placeholders_must_agree(self):
        self.bad(url="https://rates.example.com/{city}/x")
        self.bad(params={"city": {"type": "string", "in": "path", "pattern": "^[a-z]+$"}})
        self.bad(url="https://rates.example.com/{city}/x", params={"city": {"type": "string", "in": "path"}})
        self.bad(params={"q": {"type": "string", "pattern": "("}})
        self.bad(params={"q": {"type": "object"}})
        parse_operation({**PAGE, "url": "https://rates.example.com/{city}/x",
                         "params": {"city": {"type": "string", "in": "path", "pattern": "^[a-z]+$"}}})

    def test_auth_placement_is_constrained(self):
        self.bad(auth={"placement": "cookie"})
        self.bad(auth={"placement": "header", "name": "Host"})
        self.bad(auth={"placement": "header", "name": "X-Key\r\n"})
        self.bad(auth={"placement": "query", "name": "q"}, params={"q": {"type": "string"}})
        self.bad(auth={"placement": "bearer", "name": "x"})

    def test_digest_changes_with_any_field(self):
        changed = copy.deepcopy(PAGE)
        changed["url"] = "https://other.example.com/exchange/"
        self.assertNotEqual(operation_digest(PAGE), operation_digest(changed))
        self.assertEqual(operation_digest(PAGE), operation_digest(copy.deepcopy(PAGE)))


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def write(self, operations, lock=None):
        (self.dir / "operations.json").write_text(json.dumps({"schema_version": 1, "operations": operations}))
        if lock is not None:
            (self.dir / "operations.lock").write_text(json.dumps({"schema_version": 1, "operations": lock}))

    def test_only_locked_operations_run(self):
        self.write([PAGE, WEATHER], lock={PAGE["name"]: operation_digest(PAGE)})
        catalog = build_catalog(self.dir)
        self.assertEqual(sorted(catalog.operations), ["finance.rates_page"])
        self.assertEqual(catalog.unapproved, ["weather.current"])

    def test_editing_a_locked_operation_unapproves_it(self):
        self.write([PAGE], lock={PAGE["name"]: operation_digest(PAGE)})
        edited = copy.deepcopy(PAGE)
        edited["url"] = "https://evil.example.net/exchange/"
        self.write([edited])
        catalog = build_catalog(self.dir)
        self.assertEqual((catalog.operations, catalog.unapproved), ({}, ["finance.rates_page"]))

    def test_problems_are_reported_and_do_not_disable_the_rest(self):
        broken = {**PAGE, "name": "finance.broken", "url": "http://plain.example.com/x"}
        self.write([PAGE, broken, PAGE], lock={PAGE["name"]: operation_digest(PAGE)})
        catalog = build_catalog(self.dir)
        self.assertIn("finance.rates_page", catalog.operations)
        reasons = " ".join(p["reason"] for p in catalog.problems)
        self.assertIn("https", reasons)
        self.assertIn("declared twice", reasons)

    def test_unreadable_or_oversized_declarations_are_a_problem_not_a_crash(self):
        (self.dir / "operations.json").write_text("{not json")
        self.assertTrue(build_catalog(self.dir).problems)
        (self.dir / "operations.json").write_text(" " * (relay.MAX_DECLARATION_BYTES + 1))
        self.assertTrue(build_catalog(self.dir).problems)

    def test_a_missing_directory_or_file_means_no_declared_operations(self):
        self.assertEqual(build_catalog(None).operations, {})
        self.assertEqual(build_catalog(self.dir).problems, [])

    def test_builtin_operations_need_no_lock_and_cannot_be_shadowed(self):
        builtin = {**PAGE, "name": "weather.page"}
        with patch.object(relay, "BUILTIN_OPERATIONS", [builtin]):
            self.write([{**PAGE, "name": "weather.page", "url": "https://evil.example.net/x/"}],
                       lock={"weather.page": operation_digest({**PAGE, "name": "weather.page", "url": "https://evil.example.net/x/"})})
            catalog = build_catalog(self.dir)
        self.assertEqual(catalog.operations["weather.page"].host, "rates.example.com")
        self.assertIn("built-in", " ".join(p["reason"] for p in catalog.problems))


class AddressTests(unittest.TestCase):
    def resolve(self, *addresses):
        infos = [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 443)) for a in addresses]
        with patch("composer.relay.socket.getaddrinfo", return_value=infos):
            return public_address("rates.example.com")

    def test_public_addresses_are_allowed(self):
        self.assertEqual(str(self.resolve("93.184.216.34")), "93.184.216.34")
        self.assertEqual(str(self.resolve("2606:2800:220:1:248:1893:25c8:1946")), "2606:2800:220:1:248:1893:25c8:1946")

    def test_non_public_addresses_are_blocked(self):
        for blocked in (
            "127.0.0.1", "10.1.2.3", "172.16.0.9", "192.168.1.1", "169.254.169.254", "100.64.0.1",
            "0.0.0.0", "224.0.0.1", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "::ffff:169.254.169.254",
        ):
            with self.subTest(address=blocked):
                with self.assertRaises(RelayFailure) as caught:
                    self.resolve(blocked)
                self.assertEqual(caught.exception.code, "blocked")

    def test_one_bad_address_among_good_ones_blocks_the_host(self):
        with self.assertRaises(RelayFailure):
            self.resolve("93.184.216.34", "10.0.0.1")

    def test_resolution_failure_is_a_network_error(self):
        with patch("composer.relay.socket.getaddrinfo", side_effect=socket.gaierror):
            with self.assertRaises(RelayFailure) as caught:
                public_address("rates.example.com")
        self.assertEqual(caught.exception.code, "network")


class PerformTests(unittest.TestCase):
    def test_text_operation_returns_bounded_text_and_pins_the_address(self):
        out = call(parse_operation(PAGE))
        self.assertEqual(out["data"], "<html>ok</html>")
        connection = FakeConnection.instances[0]
        self.assertEqual(str(connection.address), "93.184.216.34")
        self.assertEqual(connection.host, "rates.example.com")
        method, target, headers = connection.sent
        self.assertEqual((method, target), ("GET", "/exchange/"))
        self.assertEqual(headers["User-Agent"], "Mozilla/5.0 (test)")
        self.assertEqual(headers["Accept-Encoding"], "identity")
        self.assertTrue(connection.closed)

    def test_json_projection_returns_only_declared_scalar_fields(self):
        body = json.dumps({"main": {"temp": 21.5, "secret": "x"}, "weather": [{"description": "clear"}],
                           "list": [{"name": "a"}, {"name": "b"}], "extra": {"deep": 1}}).encode()
        out = call(parse_operation(WEATHER), {"lat": 32.9, "lon": 13.2}, "KEY",
                   FakeResponse(body=body, content_type="application/json"))
        self.assertEqual(out["data"], {"main.temp": 21.5, "weather.0.description": "clear", "list.[].name": ["a", "b"]})

    def test_projection_never_returns_containers(self):
        self.assertEqual(project_fields({"a": {"b": 1}}, ("a",)), {"a": None})
        self.assertEqual(project_fields({"a": [1]}, ("a",)), {"a": None})
        self.assertEqual(project_fields({"a": "x" * 5000}, ("a",))["a"], "x" * 2000)

    def test_redirects_and_errors_are_not_followed(self):
        operation = parse_operation(PAGE)
        for status, code in ((301, "provider"), (302, "provider"), (404, "provider"), (500, "provider"),
                             (401, "credentials"), (403, "credentials")):
            with self.subTest(status=status):
                with self.assertRaises(RelayFailure) as caught:
                    call(operation, response=FakeResponse(status=status))
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(len(FakeConnection.instances), 1)

    def test_content_type_and_size_are_enforced(self):
        with self.assertRaises(RelayFailure) as caught:
            call(parse_operation(PAGE), response=FakeResponse(content_type="application/octet-stream"))
        self.assertEqual(caught.exception.code, "response")
        with self.assertRaises(RelayFailure) as caught:
            call(parse_operation(PAGE), response=FakeResponse(body=b"x" * 4097))
        self.assertEqual(caught.exception.code, "response")
        call(parse_operation(PAGE), response=FakeResponse(body=b"x" * 4096))
        with self.assertRaises(RelayFailure):
            call(parse_operation(WEATHER), {"lat": 1, "lon": 1}, "K", FakeResponse(body=b"not json", content_type="application/json"))

    def test_parameters_are_validated(self):
        operation = parse_operation(WEATHER)
        for params in (
            {"lat": 91, "lon": 1}, {"lat": "1", "lon": 1}, {"lat": True, "lon": 1}, {"lat": 1},
            {"lat": 1, "lon": 1, "lang": "english"}, {"lat": 1, "lon": 1, "lang": "EN"}, {"lat": 1, "lon": 1, "extra": 1},
        ):
            with self.subTest(params=params):
                with self.assertRaises(RelayFailure) as caught:
                    call(operation, params, "K")
                self.assertEqual(caught.exception.code, "invalid")

    def test_path_parameters_are_quoted_and_patterned(self):
        operation = parse_operation({**PAGE, "url": "https://rates.example.com/{city}/x",
                                     "params": {"city": {"type": "string", "in": "path", "pattern": "^[A-Za-z ]+$"}}})
        call(operation, {"city": "New York"})
        self.assertEqual(FakeConnection.instances[0].sent[1], "/New%20York/x")
        for value in ("../etc", "a/b", "x?y=1"):
            with self.assertRaises(RelayFailure):
                call(operation, {"city": value})

    def test_secret_placements(self):
        params = {"lat": 1, "lon": 2}
        json_reply = FakeResponse(body=b"{}", content_type="application/json")
        call(parse_operation(WEATHER), params, "S3CRET", json_reply)
        self.assertIn("appid=S3CRET", FakeConnection.instances[0].sent[1])
        bearer = parse_operation({**WEATHER, "auth": {"placement": "bearer"}})
        call(bearer, params, "S3CRET", json_reply)
        self.assertEqual(FakeConnection.instances[0].sent[2]["Authorization"], "Bearer S3CRET")
        header = parse_operation({**WEATHER, "auth": {"placement": "header", "name": "X-Api-Key"}})
        call(header, params, "S3CRET", json_reply)
        self.assertEqual(FakeConnection.instances[0].sent[2]["X-Api-Key"], "S3CRET")

    def test_secret_rules(self):
        with self.assertRaises(RelayFailure) as caught:
            call(parse_operation(WEATHER), {"lat": 1, "lon": 2})
        self.assertEqual(caught.exception.code, "credentials")
        with self.assertRaises(RelayFailure) as caught:
            call(parse_operation(PAGE), secret="unwanted")
        self.assertEqual(caught.exception.code, "invalid")

    def test_transport_errors_become_network_failures_and_close_the_connection(self):
        class Broken(FakeConnection):
            def getresponse(self):
                raise ConnectionResetError

        with self.assertRaises(RelayFailure) as caught:
            perform(parse_operation(PAGE), {}, None, resolver=public_resolver, connection_factory=Broken)
        self.assertEqual(caught.exception.code, "network")
        self.assertTrue(Broken.instances[-1].closed)


class SealedSecretTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.keys = KeyStore(self.dir / "relay-keys.json")
        self.doc = self.keys.public_document()

    def test_round_trip(self):
        oid = str(uuid.uuid4())
        sealed = seal(self.doc, "sk-live-123", oid, "weather.current")
        self.assertEqual(self.keys.open(sealed, oid, "weather.current"), "sk-live-123")

    def test_a_sealed_secret_cannot_be_replayed_into_another_request_or_operation(self):
        oid = str(uuid.uuid4())
        sealed = seal(self.doc, "sk-live-123", oid, "weather.current")
        with self.assertRaises(ValueError):
            self.keys.open(sealed, str(uuid.uuid4()), "weather.current")
        with self.assertRaises(ValueError):
            self.keys.open(sealed, oid, "weather.geocode")

    def test_tampering_and_foreign_keys_are_refused(self):
        oid = str(uuid.uuid4())
        sealed = seal(self.doc, "sk", oid, "weather.current")
        flipped = dict(sealed, ct=base64.b64encode(b"\x00" * 30).decode())
        with self.assertRaises(ValueError):
            self.keys.open(flipped, oid, "weather.current")
        other = KeyStore(self.dir / "other.json")
        with self.assertRaises(ValueError):
            other.open(sealed, oid, "weather.current")
        for bad in (None, {}, {"key_id": "x"}, dict(sealed, extra=1), dict(sealed, nonce="AAAA")):
            with self.assertRaises(ValueError):
                self.keys.open(bad, oid, "weather.current")

    def test_keys_persist_privately_and_are_reused(self):
        again = KeyStore(self.dir / "relay-keys.json")
        self.assertEqual(again.current_id, self.keys.current_id)
        self.assertEqual(oct((self.dir / "relay-keys.json").stat().st_mode & 0o777), "0o600")

    def test_the_public_document_carries_no_private_material(self):
        private = json.loads((self.dir / "relay-keys.json").read_text())["keys"][0]["private"]
        self.assertNotIn(private, json.dumps(self.doc))

    def test_a_sample_sealed_by_djangolux_opens_here(self):
        """Frozen output of `dlux.relay.seal`: Composer's opener must keep accepting it."""
        fixture = json.loads((FIXTURES / "relay_sealed_dlux.json").read_text())
        key = json.loads((FIXTURES / "relay_sealed.json").read_text())
        store = KeyStore(self.dir / "fixture-dlux.json")
        store._keys = {key["key_id"]: base64.b64decode(key["private_key"])}
        store.current_id = key["key_id"]
        self.assertEqual(store.open(fixture["sealed"], fixture["operation_id"], fixture["op"]), fixture["secret"])

    def test_shared_fixture_opens_with_the_fixture_key(self):
        """The same bytes DjangoLux's tests seal and open: the two sides agree on the format."""
        fixture = json.loads((FIXTURES / "relay_sealed.json").read_text())
        store = KeyStore(self.dir / "fixture.json")
        store._keys = {fixture["key_id"]: base64.b64decode(fixture["private_key"])}
        store.current_id = fixture["key_id"]
        self.assertEqual(store.open(fixture["sealed"], fixture["operation_id"], fixture["op"]), fixture["secret"])


class ResponderTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.agent = Path(tempfile.mkdtemp())
        self.declared = Path(tempfile.mkdtemp())
        (self.declared / "operations.json").write_text(json.dumps({"schema_version": 1, "operations": [PAGE, WEATHER]}))
        (self.declared / "operations.lock").write_text(json.dumps({"schema_version": 1, "operations": {
            PAGE["name"]: operation_digest(PAGE), WEATHER["name"]: operation_digest(WEATHER),
        }}))
        self.calls = []
        self.now = datetime.now(timezone.utc).timestamp()
        self.mono = 1000.0
        self.responder = self.make()

    def make(self, **kw):
        def fake(operation, params, secret=None):
            self.calls.append((operation.name, params, secret))
            return {"http_status": 200, "content_type": "text/html", "bytes": 5, "data": "hello"}

        args = dict(perform_call=fake, clock=lambda: self.now, monotonic=lambda: self.mono, composer_version="1.6.0b1")
        args.update(kw)
        return RelayResponder(self.state, self.agent, self.declared, **args)

    def submit(self, op="finance.rates_page", params=None, sealed=None, oid=None, lifetime=60, **override):
        oid = oid or str(uuid.uuid4())
        created = datetime.fromtimestamp(self.now, timezone.utc)
        body = {"schema_version": 1, "operation_id": oid, "op": op, "params": params or {}, "sealed": sealed,
                "created_at": created.isoformat(), "expires_at": (created + timedelta(seconds=lifetime)).isoformat()}
        body.update(override)
        path = self.responder.requests / f"{oid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body))
        return oid, path

    def result(self, oid):
        return json.loads((self.responder.results / f"{oid}.json").read_text())

    def test_publishes_capabilities_and_a_public_key(self):
        self.responder.answer()
        caps = json.loads((self.responder.root / "capabilities.json").read_text())
        self.assertEqual(sorted(caps["operations"]), ["finance.rates_page", "weather.current"])
        self.assertEqual(caps["composer"], "1.6.0b1")
        self.assertTrue(caps["operations"]["weather.current"]["auth"])
        key = json.loads((self.responder.root / "public-key.json").read_text())
        self.assertEqual(key["key_id"], caps["key_id"])
        self.assertNotIn("private", json.dumps(key))

    def test_answers_a_request_and_archives_it(self):
        oid, path = self.submit()
        self.assertEqual(self.responder.answer(), 1)
        result = self.result(oid)
        self.assertEqual((result["status"], result["data"]), ("ok", "hello"))
        self.assertEqual(result["request_digest"], hashlib.sha256((self.responder.processed / f"{oid}.json").read_bytes()).hexdigest())
        self.assertFalse(path.exists())

    def test_sealed_secret_reaches_the_call_and_never_touches_the_volume_in_the_clear(self):
        self.responder.answer()
        doc = json.loads((self.responder.root / "public-key.json").read_text())
        oid = str(uuid.uuid4())
        sealed = seal(doc, "PLAINTEXT-SECRET-42", oid, "weather.current")
        self.submit("weather.current", {"lat": 1, "lon": 2}, sealed, oid)
        self.responder.answer()
        self.assertEqual(self.result(oid)["status"], "ok")
        self.assertEqual(self.calls[-1][2], "PLAINTEXT-SECRET-42")
        for path in list(self.state.rglob("*")) + list(self.agent.rglob("*")):
            if path.is_file() and path.name != "relay-keys.json":
                self.assertNotIn(b"PLAINTEXT-SECRET-42", path.read_bytes(), str(path))

    def test_a_sealed_secret_for_another_request_is_refused(self):
        self.responder.answer()
        doc = json.loads((self.responder.root / "public-key.json").read_text())
        sealed = seal(doc, "S", str(uuid.uuid4()), "weather.current")
        oid, _ = self.submit("weather.current", {"lat": 1, "lon": 2}, sealed)
        self.responder.answer()
        self.assertEqual((self.result(oid)["status"], self.result(oid)["error"]), ("error", "credentials"))
        self.assertEqual(self.calls, [])

    def test_unknown_unapproved_and_malformed_requests_are_rejected(self):
        (self.declared / "operations.lock").write_text(json.dumps({"schema_version": 1, "operations": {PAGE["name"]: operation_digest(PAGE)}}))
        unknown, _ = self.submit("finance.nothing")
        unapproved, _ = self.submit("weather.current", {"lat": 1, "lon": 1})
        wrong_id, path = self.submit()
        path.write_text(json.dumps({**json.loads(path.read_text()), "operation_id": str(uuid.uuid4())}))
        schema, _ = self.submit(schema_version=9)
        self.responder.answer()
        self.assertEqual(self.result(unknown)["error"], "unsupported")
        self.assertEqual(self.result(unapproved)["error"], "unsupported")
        self.assertEqual(self.result(wrong_id)["error"], "invalid")
        self.assertEqual(self.result(schema)["error"], "invalid")
        self.assertEqual(self.calls, [])

    def test_expired_and_over_long_requests_are_rejected(self):
        expired, _ = self.submit(lifetime=-5)
        long_lived, _ = self.submit(lifetime=3600)
        naive, _ = self.submit(created_at="2026-01-01T00:00:00", expires_at="2099-01-01T00:00:00")
        self.responder.answer()
        self.assertEqual(self.result(expired)["error"], "expired")
        self.assertEqual(self.result(long_lived)["error"], "invalid")
        self.assertEqual(self.result(naive)["error"], "invalid")

    def test_oversized_or_badly_named_request_files_never_run(self):
        oid = str(uuid.uuid4())
        big = self.responder.requests / f"{oid}.json"
        big.parent.mkdir(parents=True, exist_ok=True)
        big.write_text(" " * (relay.MAX_REQUEST_BYTES + 1))
        odd = self.responder.requests / "not-a-uuid.json"
        odd.write_text("{}")
        self.responder.answer()
        self.assertEqual(self.result(oid)["error"], "invalid")
        self.assertFalse(odd.exists())
        self.assertEqual(self.calls, [])

    def test_rate_limit_applies_per_operation(self):
        ids = [self.submit("weather.current", {"lat": 1, "lon": 1}, oid=str(uuid.uuid4()))[0] for _ in range(4)]
        # weather.current requires a secret; the fake perform does not, so this only counts calls.
        self.responder.answer()
        statuses = [self.result(i).get("error") or self.result(i)["status"] for i in ids]
        self.assertEqual(statuses.count("limit"), 1)
        self.mono += 61
        again, _ = self.submit("weather.current", {"lat": 1, "lon": 1})
        self.responder.answer()
        self.assertEqual(self.result(again)["status"], "ok")

    def test_backlog_is_bounded(self):
        ids = [self.submit()[0] for _ in range(relay.MAX_PENDING + 5)]
        self.responder.answer()
        answered = [i for i in ids if (self.responder.results / f"{i}.json").exists()]
        rejected = [i for i in answered if self.result(i).get("error") == "limit"]
        self.assertEqual(len(rejected), 5)
        self.assertEqual(len(self.calls), relay.PER_TICK)
        self.assertEqual(len(list(self.responder.requests.glob("*.json"))), relay.MAX_PENDING - relay.PER_TICK)

    def test_failures_are_reported_with_stable_codes_and_no_raw_errors(self):
        def failing(operation, params, secret=None):
            raise RelayFailure("network", "connect to https://x/?appid=SECRETKEY failed")

        responder = self.make(perform_call=failing)
        oid, _ = self.submit()
        responder.answer()
        result = self.result(oid)
        self.assertEqual((result["status"], result["error"]), ("error", "network"))

        def exploding(operation, params, secret=None):
            raise RuntimeError("boom https://x/?appid=SECRETKEY")

        import contextlib
        import io

        responder = self.make(perform_call=exploding)
        oid, _ = self.submit()
        logged = io.StringIO()
        with contextlib.redirect_stdout(logged):
            responder.answer()
        result = self.result(oid)
        self.assertEqual(result["error"], "provider")
        self.assertNotIn("SECRETKEY", json.dumps(result))
        self.assertIn("RuntimeError", logged.getvalue())
        self.assertNotIn("SECRETKEY", logged.getvalue())

    def test_old_results_and_archives_are_swept(self):
        oid, _ = self.submit()
        self.responder.answer()
        self.now += relay.PROCESSED_RETENTION + 1
        for path in list(self.responder.results.glob("*.json")) + list(self.responder.processed.glob("*.json")):
            os.utime(path, (self.now - relay.PROCESSED_RETENTION - 5,) * 2)
        self.responder.answer()
        self.assertEqual(list(self.responder.results.glob("*.json")), [])
        self.assertEqual(list(self.responder.processed.glob("*.json")), [])

    def test_stats_count_calls_without_recording_parameters(self):
        self.submit(params={})
        self.responder.answer()
        self.mono += relay.PUBLISH_INTERVAL + 1
        self.responder.answer()
        stats = json.loads((self.responder.root / "stats.json").read_text())["operations"]["finance.rates_page"]
        self.assertEqual((stats["calls"], stats["ok"], stats["bytes"]), (1, 1, 5))

    def test_answer_never_raises(self):
        import contextlib
        import io

        logged = io.StringIO()
        with patch.object(RelayResponder, "_answer", side_effect=RuntimeError("disk gone")), \
                contextlib.redirect_stdout(logged):
            self.assertEqual(self.responder.answer(), 0)
        self.assertNotIn("disk gone", logged.getvalue())


if __name__ == "__main__":
    unittest.main()
