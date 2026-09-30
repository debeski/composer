# The egress relay

In a generated DjangoLux stack only `composer-agent` can reach the internet.
`web` and `celery` ask it to make outbound calls, over the runtime volume, as
*named operations*. This page is the agent's side of the contract; the project
developer's side is `docs/outbound-requests.md` in the django-lux repository.

```
celery  --requests/<uuid>.json-->  composer-agent  ---> internet
web,celery  <--results/<uuid>.json--  composer-agent
```

Files live under `<runtime>/state/relay/`. `celery` mounts the volume read-write
and `web` read-only, so only `celery` can write a request. The agent answers from
the same loop that answers the Operations card (default poll: 2 s).

## Operations

A request names an operation and its parameters; it never carries a URL, header
or command. Operations come from:

- **Built-in** (`BUILTIN_OPERATIONS` in `composer/relay.py`): shipped with this
  release, trusted by being part of it.
- **Declared by the project** in `relay/operations.json` in the project directory.
  The agent already mounts the project directory read-only (`${PWD}:${PWD}:ro`)
  and the application services do not, so application code cannot edit it. A
  declared operation runs only while `relay/operations.lock` pins its exact
  digest. `composer relay approve` writes the lock and prints every new or changed
  host; commit the lock with the declarations.

```json
{"schema_version": 1, "operations": [{
  "name": "finance.cbl_page",
  "url": "https://cbl.gov.ly/currency-exchange-rates/",
  "headers": {"User-Agent": "Mozilla/5.0 (my-app)"},
  "response": {"type": "text", "max_bytes": 524288},
  "rate": {"per_minute": 6}
}]}
```

| Field | Rule |
| --- | --- |
| `name` | `app.operation`, lowercase, at least two dotted parts |
| `url` | `https://`, an exact lowercase DNS name (no IP, wildcard, port other than 443, credentials, query or fragment), a plain path; `{param}` placeholders for path parameters |
| `params` | each `string` (with `max`, `pattern`), `integer` or `number` (`min`, `max`); `in`: `query` or `path`; path parameters need a pattern; at most 16 |
| `headers` | only `User-Agent`, `Accept`, `Accept-Language`; printable ASCII |
| `auth` | one secret: `bearer`, `header` (name) or `query` (name); never a forbidden header |
| `response` | `text` (bounded, decoded) or `json` (only the listed `fields` come back: dotted paths, `[]` maps a list, scalars only); `max_bytes` up to 1 MiB; `content_types` |
| `timeout` / `rate` | 1-15 s (default 10); `per_minute` 1-120 (default 30) |

Unknown keys are refused. Invalid or unapproved operations are listed in
`state/relay/capabilities.json` (`problems`, `unapproved`) and never run.

## What the agent enforces on every call

https on 443 to the declared host only; TLS verified for that name; the name is
resolved once and the address refused unless it is public (no loopback, private,
link-local including the cloud metadata address, shared, multicast, or their IPv6 and
IPv4-mapped forms) and the connection goes to that address, so DNS cannot swap it
afterwards; no redirects; `Accept-Encoding: identity`; a content-type allowlist; a
response size cap; a timeout; a per-operation rate limit; request expiry (at most
120 s ahead, time-zone aware); at most 64 pending requests; 64 KiB per request.
Results carry stable error codes (`credentials`, `network`, `provider`,
`response`, `blocked`, `limit`, `unsupported`, `expired`, `invalid`) and never raw
exception text. Logs name the exception class only.

## Sealed secrets

At first start the agent creates an X25519 key pair in its own state volume
(`relay-keys.json`, mode 0600, never on the shared volume) and publishes the public
key in `state/relay/public-key.json`. A sender seals a secret to it: an ephemeral
X25519 key, HKDF-SHA256 (salt = ephemeral public key + recipient public key, info
`composer-relay-v1`) and ChaCha20-Poly1305, with `"<operation_id>\0<op>"` as
associated data, so a captured blob cannot be replayed into another request or
operation. The plaintext exists only in the sender's and the agent's memory.
`tests/fixtures/relay_sealed.json` is shared with the django-lux tests so the two
sides cannot drift apart.

## Files

| File | Written by | Purpose |
| --- | --- | --- |
| `requests/<uuid>.json` | celery | one request; `operation_id` equals the filename |
| `results/<uuid>.json` | agent | the answer, with `request_digest`; swept after 5 min |
| `processed/` | agent | answered requests; swept after 1 h |
| `public-key.json`, `capabilities.json`, `stats.json` | agent | key, what is available and why not, per-operation counters (never parameters) |

`composer relay list` shows declared and built-in operations and their state.
