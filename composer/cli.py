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
            "  restart [-f FILE] [-d] [--status-file PATH] [service]\n"
            "      Restart running containers (short alias: composer -r).\n"
            "      Run 'composer restart --help' for details.\n"
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
    parser.add_argument(
        "-nm",
        "--no-migrate",
        action="store_true",
        help="Bypass post-start migration tasks",
    )
    parser.add_argument(
        "-mm",
        "--make-migrations",
        action="store_true",
        help="Force making migrations during post-start tasks",
    )
    parser.add_argument(
        "-a",
        "--app",
        help="Target app for initialization (passed to migrator)",
    )
    parser.add_argument(
        "-u",
        "--update",
        nargs="?",
        const=True,
        metavar="SERVICE",
        help="Pull latest image(s) then recreate immediately; pass a service name to update and recreate only that service",
    )
    parser.add_argument(
        "-uo",
        "--update-only",
        nargs="?",
        const=True,
        metavar="SERVICE",
        help="Pull latest image(s) only, then exit; pass a service name to pull only that service",
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
        "--version",
        action="store_true",
        help="Print Composer version and exit",
    )

    return parser.parse_args()


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
            "(a changed token / mtime), runs a full update via 'composer -u' "
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
