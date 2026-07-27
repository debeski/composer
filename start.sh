#!/bin/bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
composer_self_image="${COMPOSER_SELF_IMAGE:-debeski/composer:latest}"

# `update-self` replaces the legacy one-argument `--update` route.
if [[ $# -eq 1 && ("${1:-}" == "update-self" || "${1:-}" == "--update") ]]; then
    # Show current version from image's VERSION file
    echo "=== Current Composer Version ==="
    docker run --rm --entrypoint cat "${composer_self_image}" /app/VERSION 2>/dev/null || echo "  (not present locally)"
    
    echo ""
    echo "Pulling latest composer image..."
    docker pull "${composer_self_image}"
    
    echo ""
    echo "=== Installed Version ==="
    docker run --rm --entrypoint cat "${composer_self_image}" /app/VERSION
    
    exit 0
fi

docker_flags=(--rm)
if [[ -t 0 && -t 1 ]]; then
  docker_flags=(-it "${docker_flags[@]}")
fi

secret_flags=()
for candidate in .env secrets/.env .secrets/.env; do
  secret_path="${script_dir}/${candidate}"
  if [[ ! -f "${secret_path}" ]]; then
    continue
  fi
  if [[ ! -r "${secret_path}" ]]; then
    echo "Secrets file exists but is not readable by the current host user: ${secret_path}" >&2
    exit 1
  fi
  secret_keys="$(
    awk -F= '/^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*[[:space:]]*=/{key=$1; gsub(/^[[:space:]]+|[[:space:]]+$/, "", key); print key}' "${secret_path}" |
      paste -sd, -
  )"
  if [[ -z "${secret_keys}" ]]; then
    echo "Secrets file contains no environment values: ${secret_path}" >&2
    exit 1
  fi
  secret_flags=(
    --env-file "${secret_path}"
    -e "COMPOSER_INHERITED_SECRET_KEYS=${secret_keys}"
  )
  break
done

docker run "${docker_flags[@]}" \
  "${secret_flags[@]}" \
  -v "${script_dir}:${script_dir}" \
  -w "${script_dir}" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  "${composer_self_image}" "$@"
