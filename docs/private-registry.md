# Private registry login

A `WEB_IMAGE` in a private registry (a private GitLab project, a private Docker
Hub repository) needs a login to be read, both by `composer-agent`'s update check
and by every pull: `composer-executor`'s image update, `./start.sh update` and an
`up` that has to fetch an image.

## Setting the login

Put a read-only login in the secrets file:

```
COMPOSER_REGISTRY_USERNAME=gitlab+deploy-token-12
COMPOSER_REGISTRY_PASSWORD=<the deploy token>
```

For GitLab, create a deploy token with the `read_registry` scope only (Settings >
Repository > Deploy tokens); for Docker Hub, an access token with read-only scope.

The login belongs to the registry `WEB_IMAGE` names;
`COMPOSER_REGISTRY_HOST=registry.example.com` names another one. Composer never
sends it to any other registry, so public images (caddy, postgres) are pulled as
before:

- the update check follows the registry's `WWW-Authenticate` challenge and sends
  the login as Basic auth to the token service the challenge names (GitLab's
  `/jwt/auth`, Docker Hub's `auth.docker.io`), or to the registry itself when it
  asks for Basic auth. It goes over https only, never across a redirect, and never
  to the storage host a blob read is redirected to;
- every `docker compose` Composer runs gets a private `DOCKER_CONFIG` holding only
  this login (directory `0700`, `config.json` `0600`, removed when the Composer
  process exits). The Docker daemon takes registry credentials from its client,
  and the Composer containers have no Docker login of their own: a `docker login`
  on the host does not reach them.

`COMPOSER_REGISTRY_TOKEN`, a static bearer token sent straight to the registry,
still works for registries that accept one; GitLab's registry tokens expire within
minutes, so use the login there.

## Passing it to the resident pair

The generated resident block passes `COMPOSER_REGISTRY_USERNAME`,
`COMPOSER_REGISTRY_PASSWORD` and `COMPOSER_REGISTRY_HOST` to `composer-agent` and
`composer-executor` as `"${KEY:-}"`, empty unless the secrets file sets them.
`composer check --fix` adds the lines to an older stack's resident block, after
each role's `WEB_IMAGE`; a value the stack sets itself stays. After that fix, and
after any change to the login in the secrets file, run `./start.sh`, which
recreates the pair because their environment changed.

## The `registry-login` finding

`composer check` reads the agent's watched image from the resolved Compose
configuration and asks its registry for the tag, the way the agent asks:

| Situation | `registry-login` finding |
| --- | --- |
| the registry answers, with the login, a static token or without a login | OK |
| the registry needs a login and the stack passes none | WARN, naming the two keys |
| the registry refused the login (wrong token, expired, no access to the repository) | FAIL |
| the secrets file holds a login the stack does not pass to `composer-agent` or `composer-executor` | WARN: `check --fix` adds the lines |
| the registry has no such tag (or hides a private one from an anonymous reader) | WARN |
| the registry is not reachable from where `check` runs | OK, saying it was not checked |
| `WEB_IMAGE` was built on this machine and never pulled (no repository digest) | OK, nothing to read |
| `WEB_IMAGE` is a bare name (no registry, no namespace, such as `myapp:1.0`) that Docker Hub refuses: Docker Hub holds bare names only as official images, which are never private, so it is a local build | OK, nothing to read; the availability document carries no reason, so DjangoLux's Updates card shows no warning |
| Composer does not trust the registry's certificate (a private CA) | WARN |
| no `composer-agent` | no finding |

The Operations card's check runs in `composer-executor`, which has no route to a
registry, so there it reports only whether the pair receives a login.

## When the registry cannot be read

While the registry cannot be read, the availability document stays "unknown" and
never reports an update; its image entry carries `reason` (`login-required`,
`login-refused`, `not-found`, `unreachable`, `certificate`, `failed`), which
`composer agent check` prints.

- **Private CA.** The update check trusts the public certificate authorities only.
  A registry with a certificate from a private CA must also be trusted by the
  Docker daemon for pulls (`/etc/docker/certs.d/<registry host>/ca.crt` on the
  host).
- **Names only `/etc/hosts` knows.** The update check runs in `composer-agent` and
  resolves the registry (and its token service) through Docker's DNS, which does
  not read the host's `/etc/hosts`; the Docker daemon's pulls do. A registry name
  only the host resolves through `/etc/hosts` therefore also needs `extra_hosts` on
  `composer-agent` (for GitLab, both the registry and the GitLab host), or a DNS
  server that both use. Otherwise pulls work while the update check reports the
  registry unreachable.
