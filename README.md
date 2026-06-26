# composer
Secrets. Docker. Silence.

Composer manages SOPS secrets and orchestrates Docker Compose. No local Python setup. No local `sops`. Just Docker.

## setup
Put `start.sh` or `start.ps1` in your project root.

## the routine

### 1. secrets
If you need a key:

```bash
./start.sh keygen
```

To encrypt `.env`:

```bash
./start.sh encrypt <public_key>
```

To decrypt `secrets.enc`:

```bash
./start.sh decrypt <private_key>
```

### 2. deployment
Just start it.

```bash
./start.sh
```

Composer resolves secrets automatically. It first looks for a plaintext env
file — `.env`, `secrets/.env`, then `.secrets/.env` — and uses the first one
that supplies every variable the compose file requires. If none qualify, it
falls back to an encrypted file (`secrets.enc`, `secrets/secrets.enc`,
`.secrets/secrets.enc`) and prompts for the AGE private key (unless given with
`-k`).

```bash
./start.sh -k <private_key>   # skip the prompt for the encrypted path
```

## the surface
| flag | result |
| :--- | :--- |
| `-k`, `--key` | AGE private key for the encrypted-secrets path. |
| `-d`, `--dev` | Development mode. Loads `compose.dev.yml` on top of the base compose file (two files) and forces `DEBUG=True` / `DEBUG_STATUS=True` into every service. |
| `-u`, `--update [service]` | Pull the latest image(s) then recreate immediately. Pass a service name to update and recreate only that service (Compose still starts its dependencies; dependents aren't auto-restarted unless their own image changed). |
| `-uo`, `--update-only [service]` | Pull the latest image(s) before the normal full startup, without scoping the recreate. Optionally a single service. |
| `-r`, `--restart [service]` | Restart running containers via `docker compose restart` instead of a `--down` + start. Containers are preserved, so baked-in env vars survive. Pass a service name to restart only that service. |
| `-b`, `--build` | Rebuild images during startup. |
| `--down` | Stop everything. |
| `-v`, `--volumes` | Remove volumes too. |
| `-p`, `--purge` | With `--down`: also remove built untagged images, volumes, networks, orphans, and dangling build cache. |
| `--encrypt` | Encrypt a dotenv file with an AGE public key. |
| `--decrypt` | Decrypt an encrypted dotenv file. |

## mechanics
- **Secrets**: Plaintext env first (must satisfy the compose's required vars), encrypted `secrets.enc` as fallback.
- **Version**: Every service gets `COMPOSER_VERSION`.
- **UI**: Progress stays on one status line.
- **Image**: Wrapper scripts target `debeski/composer:latest`.

## why
Installing `sops`, `age`, and Python everywhere is friction. Composer keeps the toolchain inside the container and leaves the project root alone.
