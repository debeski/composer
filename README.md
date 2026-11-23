# composer

Env. Docker. Silence.

Composer is the deployment and lifecycle layer of the [DjangoLux](https://pypi.org/project/django-lux/) framework. It started as a thin wrapper that resolved secrets and called Docker Compose; it now owns everything that happens to a DLUX stack *around* the application: getting it up, keeping it healthy, updating it, repairing its topology, and letting the application's own admin panel drive all of that safely.

There is no local Python setup. Composer runs from a container, so the only thing a host needs is Docker.

## what it does

- **Deploys** a Compose project with secrets resolved from a plaintext env file, health-checked, with post-start tasks (DjangoLux's migrator) run exactly once. See [deploying](docs/deploying.md).
- **Updates** in two ways: whole application images (`update`, `pull`) behind a version gate that refuses to recreate onto an older image, and **inline DjangoLux releases** (`dlux update`, `dlux rollback`) applied from PyPI onto a runtime volume, with Trusted Publisher attestation, SHA-256 and release-manifest checks, and automatic rollback. See [updates and channels](docs/updates.md).
- **Diagnoses and repairs** the outside of a stack with `check` / `check --fix`: topology, obsolete services and proxy routes, retired wiring, wrapper drift, and deployer/resident version drift, always archived under `.xclude/` first. See [`composer check`](docs/check.md) and [stack migrations](docs/stack-migrations.md).
- **Runs resident inside the stack** as a privilege-split pair, `composer-agent` (network-facing, read-only Docker) and `composer-executor` (sole holder of Docker write authority), so an update or repair can be triggered from the DjangoLux admin UI without giving the web-facing side the Docker socket. See [the resident pair](docs/resident-pair.md) and [executor hardening](docs/executor-hardening.md).
- **Publishes state for the application to show**: deploy status and a console log, image and package availability, the active DjangoLux channel, and results for the Operations card. Composer writes these files; DjangoLux reads them ([Agent Protocol v1](docs/agent-protocol-v1.md)).
- **Manages release channels** for both Composer itself and DjangoLux (stable or beta, independently), and updates itself with `update self` (plain `update` refreshes it first).

## how it fits

```
 host                                   compose project
 ┌───────────────────┐   one-shot       ┌──────────────────────────────────────┐
 │ start.sh/start.ps1├─────────────────►│ web · worker · db · proxy · ...      │
 │  (deployer)       │  debeski/composer└──────────────────────────────────────┘
 └───────────────────┘                    ▲ health, post-start   ▲ recreate/restart
                                          │                      │
                      ┌───────────────────┴──┐  private   ┌──────┴────────────┐
   DjangoLux admin ──►│ composer-agent       │──socket───►│ composer-executor │
   (trigger files,    │ egress, read-only    │  typed ops │ docker.sock owner │
    Operations card)  │ Docker via proxy     │            │ runs the pipeline │
                      └──────────────────────┘            └───────────────────┘
```

Three faces of one image (`debeski/composer`):

| face | runs | job |
| --- | --- | --- |
| **deployer** | on demand, from `start.sh`/`start.ps1` | deploy, update, check, stop, logs, run, migrate |
| **agent** | resident (`composer agent run`) | watches triggers and registries, stages verified packages, publishes availability, delegates every write |
| **executor** | resident (`composer executor run`) | applies updates, restarts, repairs and package activations over a private socket |

The wrappers, the image, and the resident pair are versioned separately, so `check` reports drift between them and `update self` / `agent update` bring them back in line.

## quick start

Put `start.sh` or `start.ps1` in your project root. The DLUX scaffold writes them once; Composer owns them from then on and `check --fix` keeps them current ([wrappers](docs/deploying.md#wrappers)). Then just start it:

```bash
./start.sh
```

Composer resolves secrets automatically: it uses the first of `.env`, `secrets/.env` and `.secrets/.env` that supplies every variable the compose file requires ([secrets](docs/deploying.md#secrets)). To leave DjangoLux's setup wizard available on a first deploy, use `./start.sh --skip-config` ([manual first-deploy setup](docs/first-deploy.md)).

## command map

| I want to… | run |
| --- | --- |
| bring the stack up | `./start.sh` (`-d` for dev, `-b` to build) |
| update Composer, then the application image and the resident pair | `./start.sh update` |
| only download images | `./start.sh pull` |
| update only Composer itself | `./start.sh update self` |
| update DjangoLux inline, or roll it back | `./start.sh dlux update` / `dlux rollback` |
| see or request the DjangoLux channel | `./start.sh dlux channel [stable\|beta]` |
| switch Composer's own channel | `./start.sh check --beta` / `--stable` |
| diagnose, then repair, a deployment | `./start.sh check` / `check --fix` |
| undo a group-access grant on app data | `./start.sh app-permissions revert <manifest>` |
| manage the resident pair | `agent update\|restart\|off\|enable`, `executor enable` |
| run a command in a service | `./start.sh run -m web migrate --noinput` |
| run DjangoLux's migrator | `./start.sh migrate` |
| read logs | `./start.sh log -n 200 web` |
| stop | `./start.sh stop` (`-v`, `-p` are destructive and confirmed) |

Every command, flag and exit code: [commands](docs/commands.md).

## documentation

The [docs index](docs/README.md) lists every page. The most used:

- [Commands](docs/commands.md) · [Deploying](docs/deploying.md) · [Updates and channels](docs/updates.md) · [`composer check`](docs/check.md)
- [Stack migrations](docs/stack-migrations.md) · [The generated proxy](docs/proxy.md) · [App data permissions](docs/app-permissions.md) · [Private registry login](docs/private-registry.md) · [SMTP relay settings](docs/smtp-relay-settings.md) · [Maintenance page](docs/maintenance-page.md)
- [The resident pair](docs/resident-pair.md) · [Executor hardening](docs/executor-hardening.md) · [Agent Protocol v1](docs/agent-protocol-v1.md) · [The egress relay](docs/relay.md)
- [Releasing](docs/RELEASING.md) · [CHANGELOG](CHANGELOG.md)

## why

Installing Python and a compose toolchain on every host is friction, and letting an application container hold the Docker socket to update itself is a risk. Composer keeps the toolchain inside its own image, leaves the project root alone, and splits the resident work across a network-facing agent and a privileged executor, so the application can ask for an update or a repair without ever being able to perform one.
