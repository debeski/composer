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
Feed it a key and it starts the services.

```bash
./start.sh -k <private_key>
```

## the surface
| flag | result |
| :--- | :--- |
| `-k`, `--key` | AGE private key for decrypt/start flows. |
| `-d`, `--dev` | Development mode. Reads `.secrets/.env` directly. |
| `-u`, `--update` | Pull the latest Composer image. |
| `-b`, `--build` | Rebuild images during startup. |
| `--down` | Stop everything. |
| `-v`, `--volumes` | Remove volumes too. |
| `-p`, `--purge` | With `--down`: also remove built untagged images, volumes, networks, orphans, and dangling build cache. |
| `--encrypt` | Encrypt a dotenv file with an AGE public key. |
| `--decrypt` | Decrypt an encrypted dotenv file. |

## mechanics
- **Version**: Every service gets `COMPOSER_VERSION`.
- **UI**: Progress stays on one status line.
- **Image**: Wrapper scripts target `debeski/composer:latest`.

## why
Installing `sops`, `age`, and Python everywhere is friction. Composer keeps the toolchain inside the container and leaves the project root alone.
