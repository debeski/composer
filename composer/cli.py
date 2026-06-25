import argparse


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Launch Docker Compose environments with secrets")
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
        help="Force docker compose pull before starting",
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
