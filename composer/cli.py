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
            "      Run 'composer run --help' for details."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-k", "--key", help="AGE secret key")
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
        help="Pull latest image(s) before the normal full startup, without scoping the recreate (optionally a single service)",
    )
    parser.add_argument(
        "-r",
        "--restart",
        nargs="?",
        const=True,
        metavar="SERVICE",
        help="Restart running containers (docker compose restart) instead of down + start; pass a service name to restart only that service",
    )
    parser.add_argument(
        "-b",
        "--build",
        action="store_true",
        help="Force build of images before starting containers (--build)",
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
        "--decrypt",
        action="store_true",
        help="Decrypt an encrypted file and print to stdout",
    )
    parser.add_argument(
        "--encrypt",
        action="store_true",
        help="Encrypt a plaintext file (requires -k for public key)",
    )
    parser.add_argument(
        "-i",
        "--input",
        help="Input file path for --encrypt/--decrypt (default: .secrets/.env or secrets.enc)",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output file path for --encrypt/--decrypt (default: stdout for decrypt, secrets.enc for encrypt)",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print Composer version and exit",
    )
    parser.add_argument("key_positional", nargs="?", help="AGE secret key (positional)")

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
