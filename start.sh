#!/bin/bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# `--update` as the *only* argument self-updates the Composer tool image.
# `--update <service>` (and -u/-uo/-r) pass through to the app instead.
if [[ $# -eq 1 && "${1:-}" == "--update" ]]; then
    # Show current version from image's VERSION file
    echo "=== Current Composer Version ==="
    docker run --rm --entrypoint cat debeski/composer:latest /app/VERSION 2>/dev/null || echo "  (not present locally)"
    
    echo ""
    echo "Pulling latest composer image..."
    docker pull debeski/composer:latest
    
    echo ""
    echo "=== Installed Version ==="
    docker run --rm --entrypoint cat debeski/composer:latest /app/VERSION
    
    exit 0
fi

docker_flags=(--rm)
if [[ -t 0 && -t 1 ]]; then
  docker_flags=(-it "${docker_flags[@]}")
fi

docker run "${docker_flags[@]}" \
  -v "${script_dir}:${script_dir}" \
  -w "${script_dir}" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  debeski/composer:latest "$@"
