import argparse


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Launch Docker Compose environments with secrets",
        epilog=(
            "subcommands:\n"
            "  run [-m] [-s] [-F] <service> <command...>\n"
            "      Run a command inside a service (docker compose exec).\n"
            "      -m/--manage prepends 'python manage.py'; -s/--shell runs via 'sh -c';\n"
            "      -F/--fresh starts a one-off container (docker compose run --rm).\n"
            "      Run 'composer run --help' for details.\n"
            "  migrate [-s SERVICE] [-f FILE] [-d] [MIGRATOR_ARGS...]\n"
            "      Run the DLUX migrator in web (or --service SERVICE), forwarding\n"
            "      every remaining argument. Run 'composer migrate --help'.\n"
            "  restart [-f FILE] [-d] [--status-file PATH] [service]\n"
            "      Restart running containers (short alias: composer -r).\n"
            "      Run 'composer restart --help' for details.\n"
            "  update [-b] [--force] [-f FILE] [-d] [service...]\n"
            "      Pull the latest image(s), then recreate, health-check, and run\n"
            "      post-start tasks. Name services to scope it.\n"
            "  pull [-f FILE] [-d] [service...]\n"
            "      Pull images without recreating containers.\n"
            "  update-self\n"
            "      Pull the Composer deployer image (legacy alias: --update).\n"
            "  stop [-v] [-p] [-y] [-f FILE] [-d] [service...]\n"
            "      Stop and remove containers, or only the named services.\n"
            "      -v/--volumes and -p/--purge are destructive and ask for\n"
            "      confirmation unless -y/--yes is given (alias: composer down).\n"
            "      Run 'composer stop --help' for details.\n"
            "  check [--fix] [-y] [--deep] [--json] [-f FILE] [-d]\n"
            "      Doctor: verify Docker, compose config, secrets, required env,\n"
            "      topology, and version drift; --fix applies safe migrations,\n"
            "      --deep relays the in-container checks. Run 'composer check --help'.\n"
            "  agent-check [--json] [--availability-file PATH] [IMAGE ...]\n"
            "      Compare remote tag digests with locally pulled images without\n"
            "      pulling or deploying. Defaults to COMPOSER_CHECK_IMAGE,\n"
            "      WEB_IMAGE, or the compose file's agent-watched images.\n"
            "  agent-update | agent-restart | agent-off\n"
            "      Update, restart, or stop this stack's composer-agent service.\n"
            "  log [-n N|all] [--follow] [-f FILE] [-d] [service...]\n"
            "      Read Compose logs for the whole stack or named services\n"
            "      (default: last 50 lines). Run 'composer log --help'.\n"
            "  watch --trigger-file PATH [--interval N]\n"
            "      Resident updater: watch a trigger file and run a full update\n"
            "      (pull + version gate + recreate + health + post_start) on each\n"
            "      new request. Run 'composer watch --help' for details.\n"
            "  agent [--control-url URL] [--state-dir PATH]\n"
            "      Durable resident deployment agent with local DLUX handoff and\n"
            "      outbound control-plane connectivity. Run 'composer agent --help'.\n"
            "  enable-agent [--apply]\n"
            "      Migrate a generated DLUX project from composer-updater to\n"
            "      composer-agent. Dry-run by default."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-f", "--file", help="Specify an alternate compose file")
    parser.add_argument(
        "-d",
        "--dev",
        action="store_true",
        help="Development mode: also load the compose.dev.yml override (two compose files)",
    )
    migrations = parser.add_mutually_exclusive_group()
    migrations.add_argument(
        "-nm",
        "--no-migrate",
        action="store_true",
        help="Skip makemigrations and migrate; static files are still collected",
    )
    migrations.add_argument(
        "-mm",
        "--make-migrations",
        action="store_true",
        help="Force makemigrations for every app, then migrate (mutually exclusive with -nm)",
    )
    parser.add_argument(
        "-a",
        "--app",
        help="Target app for initialization (passed to migrator)",
    )
    parser.add_argument(
        "-u",
        dest="update",
        nargs="?",
        const=True,
        metavar="SERVICE",
        help="Pull latest image(s) then recreate immediately; pass a service name to update and recreate only that service",
    )
    parser.add_argument(
        "-b",
        "--build",
        action="store_true",
        help="Force build of images before starting containers (--build)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the preflight version gate (allow updating onto an older image version)",
    )
    parser.add_argument(
        "--status-file",
        metavar="PATH",
        help="Write a JSON deploy-status file to PATH (overrides COMPOSER_STATUS_FILE)",
    )
    parser.add_argument(
        "--down",
        action="store_true",
        help="Run docker compose down instead of up",
    )
    parser.add_argument(
        "-v",
        "--volumes",
        action="store_true",
        help="Remove volumes when using --down",
    )
    parser.add_argument(
        "-p",
        "--purge",
        action="store_true",
        help="Purge with --down: remove built untagged images, volumes, networks, orphans, and dangling build cache",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Answer 'yes' to confirmation prompts for destructive actions (-v, -p)",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print Composer version and exit",
    )

    return parser.parse_args()


def parse_update_args(argv):
    """Parse arguments for the `update` subcommand (composer update ...)."""
    parser = argparse.ArgumentParser(
        prog="composer update",
        description=(
            "Pull the latest image(s), then recreate the affected containers, "
            "wait for health checks, and run post-start tasks. Name services to "
            "scope both the pull and the recreate."
        ),
    )
    parser.add_argument("-f", "--file", help="Specify an alternate compose file")
    parser.add_argument(
        "-d",
        "--dev",
        action="store_true",
        help="Target the dev compose files (adds compose.dev.yml override)",
    )
    parser.add_argument(
        "-b",
        "--build",
        action="store_true",
        help="Force build of images before recreating containers (--build)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the preflight version gate (allow updating onto an older image version)",
    )
    migrations = parser.add_mutually_exclusive_group()
    migrations.add_argument(
        "-nm",
        "--no-migrate",
        action="store_true",
        help="Skip makemigrations and migrate; static files are still collected",
    )
    migrations.add_argument(
        "-mm",
        "--make-migrations",
        action="store_true",
        help="Force makemigrations for every app, then migrate (mutually exclusive with -nm)",
    )
    parser.add_argument(
        "-a",
        "--app",
        help="Target app for initialization (passed to migrator)",
    )
    parser.add_argument(
        "--status-file",
        metavar="PATH",
        help="Write a JSON deploy-status file to PATH (overrides COMPOSER_STATUS_FILE)",
    )
    parser.add_argument(
        "service",
        nargs="*",
        help="Update only these Compose services (default: every service)",
    )
    return parser.parse_args(argv)


def parse_pull_args(argv):
    parser = argparse.ArgumentParser(
        prog="composer pull",
        description=(
            "Pull the latest image(s) without recreating containers, running "
            "health checks, or executing post-start tasks."
        ),
    )
    parser.add_argument("-f", "--file", help="Specify an alternate compose file")
    parser.add_argument(
        "-d",
        "--dev",
        action="store_true",
        help="Target the dev compose files (adds compose.dev.yml override)",
    )
    parser.add_argument(
        "--status-file",
        metavar="PATH",
        help="Write a JSON pull-status file to PATH (overrides COMPOSER_STATUS_FILE)",
    )
    parser.add_argument(
        "service",
        nargs="*",
        help="Pull only these Compose services (default: every service)",
    )
    return parser.parse_args(argv)


def parse_update_self_args(argv):
    parser = argparse.ArgumentParser(
        prog="composer update-self",
        description="Pull the latest Composer deployer image.",
    )
    return parser.parse_args(argv)


def parse_check_args(argv):
    """Parse arguments for the `check` subcommand (composer check ...)."""
    parser = argparse.ArgumentParser(
        prog="composer check",
        description=(
            "Diagnose the outside of a DLUX stack: Docker, Compose config, "
            "secrets, required environment variables, topology, and version "
            "drift. Deep, app-level checks are relayed to the container."
        ),
    )
    parser.add_argument("-f", "--file", help="Specify an alternate compose file")
    parser.add_argument(
        "-d",
        "--dev",
        action="store_true",
        help="Target the dev compose files (adds compose.dev.yml override)",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help=(
            "Apply guarded safe fixes (remove obsolete pgadmin/db-backup services "
            "and their legacy proxy routes, and migrate a legacy "
            "composer-updater topology)"
        ),
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Confirm --fix actions up front instead of being prompted",
    )
    parser.add_argument(
        "--deep",
        action="store_true",
        help="Also relay the in-container doctor (app-level checks)",
    )
    parser.add_argument(
        "--deep-service",
        default="web",
        metavar="SERVICE",
        help="Service to run the in-container doctor in (default: web)",
    )
    parser.add_argument(
        "--deep-command",
        default="python manage.py dlux_doctor",
        metavar="CMD",
        help="In-container doctor command (default: 'python manage.py dlux_doctor')",
    )
    parser.add_argument("--json", action="store_true", help="Emit results as JSON")
    return parser.parse_args(argv)


def parse_agent_check_args(argv):
    parser = argparse.ArgumentParser(
        prog="composer agent-check",
        description=(
            "Check whether newer image tag digests are available without pulling "
            "or changing the deployment. The JSON form uses the same availability "
            "contract as composer-agent."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the complete machine-readable availability document",
    )
    parser.add_argument(
        "--availability-file",
        metavar="PATH",
        help="Also atomically write the availability document to PATH",
    )
    parser.add_argument("-f", "--file", help="Specify an alternate compose file")
    parser.add_argument(
        "image",
        nargs="*",
        metavar="IMAGE",
        help=(
            "Tagged image reference to check; repeat for multiple images "
            "(default: COMPOSER_CHECK_IMAGE, WEB_IMAGE, or the images the "
            "compose file's composer agent watches)"
        ),
    )
    return parser.parse_args(argv)


def _parse_agent_service_args(argv, prog, description, *, status_file=False):
    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument("-f", "--file", help="Specify an alternate compose file")
    parser.add_argument(
        "-d",
        "--dev",
        action="store_true",
        help="Target the dev compose files (adds compose.dev.yml override)",
    )
    if status_file:
        parser.add_argument(
            "--status-file",
            metavar="PATH",
            help="Write operation status JSON to PATH",
        )
    return parser.parse_args(argv)


def parse_agent_update_args(argv):
    return _parse_agent_service_args(
        argv,
        "composer agent-update",
        "Pull and recreate only this stack's composer-agent service.",
        status_file=True,
    )


def parse_agent_restart_args(argv):
    return _parse_agent_service_args(
        argv,
        "composer agent-restart",
        "Restart and health-check only this stack's composer-agent service.",
        status_file=True,
    )


def parse_agent_off_args(argv):
    return _parse_agent_service_args(
        argv,
        "composer agent-off",
        "Stop this stack's composer-agent service without changing the application.",
    )


def parse_stop_args(argv, prog="composer stop"):
    """Parse arguments for the `stop` subcommand (composer stop ...)."""
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Stop and remove this project's containers, or only the named "
            "services. Secrets, health checks, and post-start tasks are skipped. "
            "Destructive options (-v, -p) require an explicit confirmation."
        ),
    )
    parser.add_argument("-f", "--file", help="Specify an alternate compose file")
    parser.add_argument(
        "-d",
        "--dev",
        action="store_true",
        help="Target the dev compose files (adds compose.dev.yml override)",
    )
    parser.add_argument(
        "-v",
        "--volumes",
        action="store_true",
        help="Also remove named volumes (destructive: stored data is lost)",
    )
    parser.add_argument(
        "-p",
        "--purge",
        action="store_true",
        help="Also remove volumes, built untagged images, orphans, and dangling build cache",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Confirm the destructive options up front instead of being prompted",
    )
    parser.add_argument(
        "service",
        nargs="*",
        help="Stop only these Compose services (default: the whole project)",
    )
    return parser.parse_args(argv)


def parse_down_args(argv):
    """Deprecated alias kept for `composer down ...`."""
    return parse_stop_args(argv, prog="composer down")


def parse_log_args(argv):
    """Parse arguments for the `log` subcommand (composer log ...)."""
    parser = argparse.ArgumentParser(
        prog="composer log",
        description=(
            "Read Compose logs for the whole stack or for named services. "
            "Shows the last 50 lines per service by default."
        ),
    )
    parser.add_argument("-f", "--file", help="Specify an alternate compose file")
    parser.add_argument(
        "-d",
        "--dev",
        action="store_true",
        help="Target the dev compose files (adds compose.dev.yml override)",
    )
    parser.add_argument(
        "-n",
        "--tail",
        default="50",
        metavar="N",
        help="Lines to show per service; 'all' or 0 shows the full log (default: 50)",
    )
    parser.add_argument(
        "-F",
        "--follow",
        action="store_true",
        help="Stream new log output until interrupted",
    )
    parser.add_argument(
        "-t",
        "--timestamps",
        action="store_true",
        help="Prefix each line with its timestamp",
    )
    parser.add_argument(
        "--since",
        metavar="TIME",
        help="Only show logs after this point (e.g. 10m, 2h, 2026-07-24T10:00:00)",
    )
    parser.add_argument(
        "--until",
        metavar="TIME",
        help="Only show logs before this point",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable the per-service color prefix",
    )
    parser.add_argument(
        "service",
        nargs="*",
        help="Read only these Compose services (default: every service)",
    )
    return parser.parse_args(argv)


def parse_run_args(argv):
    """Parse arguments for the `run` subcommand (composer run ...)."""
    parser = argparse.ArgumentParser(
        prog="composer run",
        description="Run a command inside a Compose service (docker compose exec/run).",
    )
    parser.add_argument(
        "-m",
        "--manage",
        action="store_true",
        help="Run as a Django management command (prepends 'python manage.py')",
    )
    parser.add_argument(
        "-s",
        "--shell",
        action="store_true",
        help="Run the command through a shell (sh -c) so pipes/&&/redirection work",
    )
    parser.add_argument(
        "-F",
        "--fresh",
        action="store_true",
        help="Start a one-off container (docker compose run --rm) instead of exec into the running one",
    )
    parser.add_argument("-f", "--file", help="Specify an alternate compose file")
    parser.add_argument(
        "-d",
        "--dev",
        action="store_true",
        help="Target the dev compose files (adds compose.dev.yml override)",
    )
    parser.add_argument("service", help="Compose service name")
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command (and its arguments) to run inside the service",
    )
    return parser.parse_args(argv)


def parse_migrate_args(argv):
    """Parse Composer's options and leave all other options for migrator."""
    parser = argparse.ArgumentParser(
        prog="composer migrate",
        description=(
            "Run the DLUX migrator in a running Compose service. Composer uses "
            "the service's org.dlux.post-start command when declared, otherwise "
            "the existing dlux-updater supervisor (with the packaged supervisor "
            "as the default). Unrecognized arguments are "
            "forwarded unchanged to 'python manage.py migrator'."
        ),
    )
    parser.add_argument(
        "-s",
        "--service",
        default="web",
        help="Compose service in which to run migrator (default: web)",
    )
    parser.add_argument("-f", "--file", help="Specify an alternate compose file")
    parser.add_argument(
        "-d",
        "--dev",
        action="store_true",
        help="Target the dev compose files (adds compose.dev.yml override)",
    )
    args, migrator_args = parser.parse_known_args(argv)
    if migrator_args[:1] == ["--"]:
        migrator_args = migrator_args[1:]
    args.migrator_args = migrator_args
    return args


def parse_restart_args(argv):
    """Parse arguments for the `restart` subcommand (composer restart ...)."""
    parser = argparse.ArgumentParser(
        prog="composer restart",
        description=(
            "Restart running Compose containers, then wait for their health checks. "
            "Containers are preserved and post-start tasks are not run."
        ),
    )
    parser.add_argument("-f", "--file", help="Specify an alternate compose file")
    parser.add_argument(
        "-d",
        "--dev",
        action="store_true",
        help="Target the dev compose files (adds compose.dev.yml override)",
    )
    parser.add_argument(
        "--status-file",
        metavar="PATH",
        help="Write a JSON restart-status file to PATH (overrides COMPOSER_STATUS_FILE)",
    )
    parser.add_argument(
        "service",
        nargs="?",
        help="Restart only this Compose service (default: all services)",
    )
    return parser.parse_args(argv)


def parse_watch_args(argv):
    """Parse arguments for the `watch` subcommand (composer watch ...)."""
    parser = argparse.ArgumentParser(
        prog="composer watch",
        description=(
            "Resident updater. Watches a trigger file and, on each new request "
            "(a changed token / mtime), runs a full update via 'composer update' "
            "(pull + version gate + recreate + health + post_start). Records the "
            "processed token in <trigger-file>.ack so a request survives restarts "
            "and is not re-run."
        ),
    )
    parser.add_argument(
        "--trigger-file",
        required=True,
        metavar="PATH",
        help="File watched for update requests (JSON with a 'token', or any file — mtime is the token)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=15.0,
        metavar="SECONDS",
        help="Seconds between trigger checks (default: 15, min 2)",
    )
    parser.add_argument(
        "--status-file",
        metavar="PATH",
        help="Deploy-status file for each update run (exported as COMPOSER_STATUS_FILE to the child)",
    )
    parser.add_argument(
        "--log-file",
        metavar="PATH",
        help="Console log file for each update run (default: 'deploy-log.txt' beside --status-file); exported as COMPOSER_LOG_FILE",
    )
    parser.add_argument("-f", "--file", help="Alternate compose file (passed through to each update)")
    parser.add_argument(
        "-d",
        "--dev",
        action="store_true",
        help="Use the dev compose files for each update (adds compose.dev.yml)",
    )
    parser.add_argument(
        "--check-image",
        action="append",
        metavar="IMAGE",
        help="Image ref to poll the registry for a newer digest (repeatable); enables the availability check",
    )
    parser.add_argument(
        "--check-interval",
        type=float,
        default=3600.0,
        metavar="SECONDS",
        help="Seconds between registry availability checks (default: 3600, min 60)",
    )
    parser.add_argument(
        "--availability-file",
        metavar="PATH",
        help="Write image-update availability JSON to PATH (requires --check-image)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process at most one pending request, then exit (for testing)",
    )
    return parser.parse_args(argv)


def parse_agent_args(argv):
    parser = argparse.ArgumentParser(
        prog="composer agent",
        description=(
            "Resident deployment agent. Preserves the local DLUX trigger/status "
            "contract and optionally connects outbound to a DLUX control plane."
        ),
    )
    parser.add_argument(
        "--control-url",
        default=None,
        help="Control-plane base URL (default: COMPOSER_CONTROL_URL)",
    )
    parser.add_argument(
        "--enrollment-token",
        default=None,
        help="One-use enrollment token (default: COMPOSER_ENROLLMENT_TOKEN)",
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        help="Private durable agent state directory (default: COMPOSER_AGENT_STATE_DIR or /var/lib/composer-agent)",
    )
    parser.add_argument(
        "--bridge-dir",
        default=None,
        help="DLUX typed bridge directory (default: sibling 'agent' directory beside the trigger file)",
    )
    parser.add_argument(
        "--trigger-file",
        default="/opt/dlux-runtime/state/image-update-request.json",
        help="Local DLUX image-update trigger file",
    )
    parser.add_argument("--status-file", help="Local deploy-status JSON file")
    parser.add_argument("--log-file", help="Local sanitized deploy log file")
    parser.add_argument("--interval", type=float, default=2.0, help="Local work-loop interval")
    parser.add_argument("-f", "--file", help="Alternate compose file")
    parser.add_argument("-d", "--dev", action="store_true", help="Use compose.dev.yml")
    parser.add_argument("--check-image", action="append", metavar="IMAGE")
    parser.add_argument("--check-interval", type=float, default=3600.0)
    parser.add_argument("--availability-file")
    parser.add_argument(
        "--allow-http-localhost",
        action="store_true",
        help="Allow an http://localhost control URL for development only",
    )
    parser.add_argument("--once", action="store_true", help="Run one local agent iteration")
    return parser.parse_args(argv)


def parse_enable_agent_args(argv):
    parser = argparse.ArgumentParser(
        prog="composer enable-agent",
        description=(
            "Replace a recognized generated DLUX composer-updater block with the "
            "hardened composer-agent topology. The default is a read-only dry run."
        ),
    )
    parser.add_argument("--apply", action="store_true", help="Apply after Compose validation")
    parser.add_argument(
        "--project-dir",
        default=".",
        help="Generated DLUX project directory (default: current directory)",
    )
    parser.add_argument("-f", "--file", help="Compose file relative to the project directory")
    parser.add_argument(
        "--allow-unverified-dlux",
        action="store_true",
        help="Allow apply when a DjangoLux 1.5+ dependency cannot be verified",
    )
    parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def parse_enable_executor_args(argv):
    parser = argparse.ArgumentParser(
        prog="composer enable-executor",
        description=(
            "Harden a generated composer-agent stack: move Docker write authority "
            "into a composer-executor, demote docker-socket-proxy to read-only, and "
            "keep the agent read-only. The default is a read-only dry run."
        ),
    )
    parser.add_argument("--apply", action="store_true", help="Apply after Compose validation")
    parser.add_argument(
        "--project-dir",
        default=".",
        help="Generated DLUX project directory (default: current directory)",
    )
    parser.add_argument("-f", "--file", help="Compose file relative to the project directory")
    parser.add_argument(
        "--allow-unverified-dlux",
        action="store_true",
        help="Allow apply when a DjangoLux 1.5+ dependency cannot be verified",
    )
    parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def parse_executor_args(argv):
    parser = argparse.ArgumentParser(
        prog="composer executor",
        description=(
            "Run the privileged composer-executor: the sole holder of Docker "
            "authority. Serves a private Unix socket for the agent's typed "
            "restart/recovery operations. Not for interactive use."
        ),
    )
    parser.add_argument(
        "--socket",
        metavar="PATH",
        help="Unix socket path (default: COMPOSER_EXECUTOR_SOCKET or the runtime default)",
    )
    parser.add_argument(
        "--trigger-file",
        metavar="PATH",
        help="Image-update trigger file to watch and perform (enables the watch loop)",
    )
    parser.add_argument("--status-file", metavar="PATH", help="Deploy status JSON path")
    parser.add_argument("--interval", type=float, default=2.0, help="Watch poll interval seconds")
    parser.add_argument("-f", "--file", help="Alternate compose file")
    parser.add_argument("-d", "--dev", action="store_true", help="Target the dev compose files")
    return parser.parse_args(argv)
