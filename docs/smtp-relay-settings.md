# SMTP relay settings

DjangoLux's System Settings Email step (DjangoLux 1.16.2 or later), with delivery
path *Internal SMTP relay* and provider settings *Environment*, shows the relay's
host, port, encryption and user, and whether a password is set, from web's own
environment. Composer checks that the stack passes those settings, and that the
relay it runs can check its provider's certificate.

## The shared settings

The generated compose shares them in the `x-environment` block, which `web`,
`worker` and `smtp-relay` merge:

- `SMTP_RELAY_HOST`, `SMTP_RELAY_PORT`, `SMTP_RELAY_USE_TLS`, `SMTP_RELAY_USE_SSL`
  (empty: SSL on port 465, STARTTLS otherwise) and `SMTP_RELAY_USER`, each as
  `"${SMTP_RELAY_<KEY>:-<default>}"`;
- `SMTP_RELAY_PASSWORD_SET: "${SMTP_RELAY_PASSWORD:+yes}"`. The password itself
  stays in `smtp-relay`'s environment, so web only learns whether one is set;
- the relay's certificate check: `SMTP_RELAY_TLS_VERIFY:
  "${SMTP_RELAY_TLS_VERIFY:-True}"` (`False` accepts any provider certificate) and
  `SMTP_RELAY_CA_FILE: "${SMTP_RELAY_CA_FILE:-}"` (a private CA bundle's path inside
  `smtp-relay`). The relay reads both from its own environment, so on a compose
  that does not pass them it always verifies against the system CAs and setting
  them in the secrets file has no effect.

## The `relay-env` finding

| Compose | `relay-env` finding |
| --- | --- |
| the settings reach `smtp-relay` only, in the generated `"${SMTP_RELAY_<KEY>:-<default>}"` form | WARN: the Email step cannot show them (it still sends) |
| the mail settings are shared but not `SMTP_RELAY_TLS_VERIFY` and `SMTP_RELAY_CA_FILE` | WARN: the relay's certificate check cannot be configured |
| shared through `x-environment` as above | OK |
| no `smtp-relay` service running DjangoLux's relay | OK |
| custom: a value written into `smtp-relay` instead of the secrets file, a missing generated line, an `x-environment` block that sets some of them its own way, or an environment Composer cannot edit | OK, naming what it left alone |
| `x-environment` passes `SMTP_RELAY_PASSWORD` itself, to every service that merges it | WARN |

### What `check --fix` changes

`composer check --fix` brings an unmodified generated stack to the shared form:

1. it moves the `smtp-relay` lines into the `x-environment` block after
   `DEFAULT_FROM_EMAIL` (or at the block's end), keeping each default the stack
   used;
2. adds `SMTP_RELAY_USE_SSL` and `SMTP_RELAY_PASSWORD_SET`, then the two
   certificate lines (after `SMTP_RELAY_PASSWORD_SET` on a stack that already
   shares the mail settings, moving them out of `smtp-relay` with their defaults if
   they are there);
3. leaves `SMTP_RELAY_PASSWORD` in `smtp-relay`.

It validates the result with `docker compose config` and refuses when another
Compose file would give `web` or `worker` different values from `smtp-relay`, or
hand the password to any other service. It archives the original under
`.xclude/composer-check/` and recreates a running `web`, `worker` and `smtp-relay`,
which read their environment only when created. The Operations card previews the
same change.

It never reads the secrets file or shows the password, in the preview or anywhere
else, and the secrets file needs no change: an unset `SMTP_RELAY_USE_SSL`,
`SMTP_RELAY_TLS_VERIFY` or `SMTP_RELAY_CA_FILE` reads as its default.

A custom stack is left alone. To have Composer share its settings, keep the values
in the secrets file (`SMTP_RELAY_HOST=smtp.office365.com`) and pass them to
`smtp-relay` in the generated form.

## Trusting a private CA

The fix never adds a CA mount: a bind mount of a missing file makes Docker create a
directory there. To trust a private CA:

1. save the bundle as `.secrets/smtp-relay-ca.pem`;
2. add `volumes: [./.secrets/smtp-relay-ca.pem:/etc/dlux/smtp-relay-ca.pem:ro]` to
   `smtp-relay`;
3. set `SMTP_RELAY_CA_FILE=/etc/dlux/smtp-relay-ca.pem` in the secrets file.

A mount of the project's own is left alone.

## The `relay-version` finding

`smtp-relay` runs the DjangoLux baked into the project image: it mounts no runtime
volume, so an inline DjangoLux update (`composer dlux update`) reaches `web` and
`worker` but not the relay. A relay older than DjangoLux 1.16.2 signs in to the
provider without checking its certificate and ignores `SMTP_RELAY_TLS_VERIFY` and
`SMTP_RELAY_CA_FILE`, while a web on 1.16.2 or later shows the check in the Email
step.

So when web runs 1.16.2 or later, the `relay-version` finding reads the relay's
version from its image in a fresh container (`docker compose run --rm --no-deps`,
`python -m dlux --version`): OK at 1.16.2 or later, WARN when older. There is no
finding on an older web, without a relay, or when the version cannot be read.

`check --fix` cannot change an image: move the stack to a project image built on
DjangoLux 1.16.2 or later (a project release, or a rebuild from source), then run
`./start.sh`.
