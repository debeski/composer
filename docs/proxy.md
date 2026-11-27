# The generated proxy

DjangoLux generates the stack's proxy: `.proxy/Caddyfile` for Caddy, or
`.proxy/default.conf.template` for Nginx. `composer check` checks three security
rules in it, and `check --fix` rewrites an unmodified generated file where a rule
needs a new form. The maintenance page the proxy serves has its own page:
[maintenance page](maintenance-page.md).

A changed proxy file takes effect only when the proxy reloads it: `./start.sh`
alone does not reload a changed file, so run `./start.sh restart caddy` (or
`nginx`). `check --fix` reloads or recreates the proxy itself after its own
rewrites.

## Stored files

**Security, DjangoLux stack schema 4.** Stacks generated before DjangoLux 1.15.3
have a proxy that serves every `/media/` file straight from disk, so anyone who can
reach the site can download stored files by URL without signing in. `composer
check` reports this as a `stored-files` **FAIL** (a hand-edited `/media/` section is
a WARN instead).

Once the running DjangoLux is 1.15.3 or later (the active inline release, or the
image's), `composer check --fix` rewrites an unmodified generated `.proxy/Caddyfile`
/ `.proxy/default.conf.template`:

- only `media/public/`, branding and managed images and fonts stay public;
- every other `/media/` file is first checked by DjangoLux (`forward_auth` /
  `auth_request` to `/sys/files/media-access/`);
- the new config is validated before anything is written, the files are restamped
  3 → 4 (see [stamps](stack-migrations.md#stamps)) and the originals archived under
  `.xclude/composer-check/`, and the proxy is recreated.

With an older DjangoLux the fix refuses, because the proxy would then refuse every
private file; the finding and the refusal both name the way to 1.15.3 on this stack
([the way out of an old stack](stack-migrations.md#the-way-out-of-an-old-stack)). Before fixing, list folders your site shows to signed-out visitors
(product photos, for example) in DjangoLux's `DLUX_PUBLIC_MEDIA`, or move them
under `media/public/`.

### How stored files are served

**Security.** Who may fetch a file is half of it; the other half is how it is
served. A proxy that serves an uploaded HTML or SVG file inline runs its script as
the site, with the rights of whoever opens it. DjangoLux's scaffold proxy sends, on
every `/media/` route (public and private):

- `X-Content-Type-Options: nosniff`;
- `Content-Security-Policy: default-src 'none'; sandbox` on every file but PDF (a
  sandbox stops the browser's PDF viewer);
- `Content-Disposition: attachment` on every file except raster images (AVIF, BMP,
  GIF, ICO, JPEG, PNG, WebP) and PDF.

It also sends `Content-Security-Policy: default-src 'none'; script-src 'self';
style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self';
base-uri 'none'; form-action 'none'; frame-ancestors 'self'` with the maintenance
page, both under
`/_dlux/maintenance/` and on the 502/503/504 page, so a page there runs only its
own files.

Caddy carries the `/media/` headers in a top-level `(dlux_media_headers)` snippet
that both `/media/` routes `import`. nginx carries them in the `$dlux_media_csp` and
`$dlux_media_disposition` maps and three `add_header` lines in every `/media/`
location. A private proxy file that lacks any of them is a `stored-files`
**FAIL** that names each missing header and file. Only the maintenance page's CSP
missing is a **WARN**. `composer check --fix` adds what is missing to the generated
shape in place, with no particular DjangoLux needed: it inserts the snippet and the
imports (Caddy), or the maps and `add_header` lines (nginx), and the maintenance
CSP lines. It keeps the schema stamp, validates the new config, archives the
originals under `.xclude/composer-check/` and recreates the proxy. A route that sets
some of these headers itself is left for a human; the finding says what to copy
from a fresh `python -m dlux startproject`.

## Cross-site requests (Fetch Metadata)

DjangoLux's generated proxy refuses with 403 what another site's page makes a
visitor's browser send in the background (images, scripts, `fetch()`, form posts,
`<object>`/`<embed>`), using the browser's `Sec-Fetch-*` headers
([web.dev resource isolation policy](https://web.dev/articles/fetch-metadata)).
Links from other sites, same-site requests and clients without the headers pass,
and static files and public media stay open to other sites.

Composer finds the policy by its name, `cross_site_request`, in each proxy file
present:

| File | Present when it has | `fetch-metadata` finding |
| --- | --- | --- |
| `.proxy/Caddyfile` | a `@cross_site_request` matcher and `respond @cross_site_request 403` (or `error … 403`) | OK, else WARN |
| `.proxy/default.conf.template` | a `map … $cross_site_request` and `if ($cross_site_request) { return 403; }` | OK, else WARN |
| neither file | | OK, nothing to check |

Comments and formatting don't count, a matcher nothing refuses is missing, and a
custom expression under the same name is accepted. The WARN names each file without
it.

The fix is to copy the block from DjangoLux's docs
(`docs/deployment-configuration.md`, *Cross-site requests (Fetch Metadata)*) or a
fresh `python -m dlux startproject`, then restart the proxy. CSRF tokens and
`SameSite` cookies already stop forged changes, so this is a WARN and `check --fix`
leaves the file alone.

## Trusted front proxies

**Caddy.** The generated `.proxy/Caddyfile` keeps an incoming `X-Forwarded-For`
from any peer on a private network and appends the peer, and DjangoLux records the
client address from that header. A deployment behind a front proxy on a private
address (pfSense, HAProxy, another nginx, a cloud load balancer) needs this to see
real client addresses; without a front proxy, a client on a private network that
reaches Caddy directly can choose the address DjangoLux records, which sidesteps
the per-address login lockout and puts a false address in the audit log.

The trusted range is the optional `DLUX_FRONT_PROXY` line in the secrets file
(`.secrets/.env`), which the Caddyfile reads as `trusted_proxies static
{$DLUX_FRONT_PROXY:private_ranges}` and compose passes to `caddy` as
`DLUX_FRONT_PROXY: "${DLUX_FRONT_PROXY-private_ranges}"`:

| `DLUX_FRONT_PROXY` | Caddy trusts | `front-proxy` finding |
| --- | --- | --- |
| not set | every private network | WARN |
| `private_ranges` | every private network, as a deliberate choice | OK |
| empty (`DLUX_FRONT_PROXY=`) | no proxy: it records the address that connects to it | OK |
| `10.0.0.5` or `10.0.0.5 192.168.10.0/24` | only those addresses and ranges (space-separated, no quotes) | OK |
| anything else (`none`, comma-separated lists) | Caddy does not start | FAIL, naming the entries |
| a quoted value (`"10.0.0.5 10.0.0.6"`) | `./start.sh` passes the quotes to Caddy, which does not start with a quoted list | FAIL |

The WARN names three ways out (at a terminal, `check --fix` asks which one applies
and writes the answer: [questions](check.md#questions-at-a-terminal)):

- set `DLUX_FRONT_PROXY` to the front proxy's address(es);
- set it empty when users reach the server directly;
- or leave it unset and firewall the server so only the front proxy reaches Caddy's
  published port (`CADDY_PORT`, else `NGINX_PORT`, else 80).

Caddy reads the value when its container is created, so apply a changed secrets
file with `./start.sh`, which recreates `caddy` because its environment changed.

### What `check --fix` changes

`composer check --fix` brings an unmodified generated stack to this form:

- it rewrites the `trusted_proxies static private_ranges` line of `.proxy/Caddyfile`
  (indentation and the other `servers` options kept);
- it adds the variable to the `caddy` service's `environment` in the Compose file
  that defines it, in that mapping's or list's own style, or as a new mapping after
  `ports`.

Until then the finding is a WARN that the stack cannot be told which proxies to
trust, also when `DLUX_FRONT_PROXY` is already set. Left unset, the rewritten stack
trusts exactly what it trusted before.

The fix validates the Compose configuration and then the Caddyfile with `caddy
validate` under the new environment, archives the originals under
`.xclude/composer-check/`, and recreates a running `caddy`; it refuses while
`DLUX_FRONT_PROXY` holds an invalid entry or is in quotes. A `trusted_proxies` line
of the project's own, a Caddyfile without one, or a `caddy` service that sets its
own `DLUX_FRONT_PROXY` value is reported as custom and left alone; an `environment`
written as an alias or flow mapping is reported with the line to add by hand. The
Operations card previews the same change.
