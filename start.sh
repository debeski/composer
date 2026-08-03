#!/bin/bash
set -euo pipefail

# Closing the terminal must not abort a run in flight. The ignored disposition
# is inherited by `docker run`, so the client is not killed by the hangup and
# the container keeps going; Ctrl+C still reaches composer and cancels.
trap '' HUP

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
composer_self_image="${COMPOSER_SELF_IMAGE:-debeski/composer:latest}"

# `update-self` replaces the legacy one-argument `--update` route.
if [[ $# -eq 1 && ("${1:-}" == "update-self" || "${1:-}" == "--update") ]]; then
    # Show current version from image's VERSION file. `docker image inspect`
    # first, because `docker run` on a missing image pulls it — silently, since
    # the progress goes to the stderr this used to discard.
    echo "=== Current Composer Version ==="
    if docker image inspect "${composer_self_image}" >/dev/null 2>&1; then
        docker run --rm --entrypoint cat "${composer_self_image}" /app/VERSION
    else
        echo "  (not present locally)"
    fi

    echo ""
    echo "Pulling latest composer image..."
    docker pull "${composer_self_image}"
    
    echo ""
    echo "=== Installed Version ==="
    docker run --rm --entrypoint cat "${composer_self_image}" /app/VERSION
    
    exit 0
fi

# Announce the first-run download instead of letting `docker run` pull an
# unnamed image while the command appears to hang.
if ! docker image inspect "${composer_self_image}" >/dev/null 2>&1; then
  echo "Composer image not installed locally — fetching ${composer_self_image}..."
  docker pull "${composer_self_image}"
  echo ""
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
