#!/usr/bin/env bash
# Runtime smoke tests for a built composer image.
#
#   Usage: scripts/smoke-test.sh <image-ref>
#
# Runs the actual image and fails (non-zero) if it is broken, so CI and the
# Release workflow can gate the Docker Hub push on it. Must be run from the repo
# root (reads ./VERSION to assert the image reports the expected version).
set -euo pipefail

IMAGE="${1:?usage: smoke-test.sh <image-ref>}"
run() { docker run --rm "$IMAGE" "$@"; }

echo "==> Smoke-testing image: $IMAGE"

# 1. --version prints and matches the repo VERSION file.
expected="$(tr -d '[:space:]' < VERSION)"
actual="$(run --version | awk '{print $NF}' | tr -d '[:space:]')"
echo "    version: image='$actual' file='$expected'"
[ "$actual" = "$expected" ] || { echo "::error::image version '$actual' != VERSION '$expected'"; exit 1; }

# 2. --help exposes the key CLI flags.
help="$(run --help)"
for flag in --down --purge --volumes --update --update-only --restart --build --encrypt --decrypt; do
  echo "$help" | grep -q -- "$flag" || { echo "::error::--help is missing '$flag'"; exit 1; }
done
echo "$help" | grep -q -- "run " || { echo "::error::--help is missing the 'run' subcommand"; exit 1; }
run run --help >/dev/null || { echo "::error::'run --help' failed"; exit 1; }
echo "    help: all expected flags present"

# 3. Bundled tooling is present and runnable inside the image.
docker run --rm --entrypoint age    "$IMAGE" --version >/dev/null
docker run --rm --entrypoint sops   "$IMAGE" --version >/dev/null
docker run --rm --entrypoint docker "$IMAGE" --version >/dev/null
docker run --rm --entrypoint docker "$IMAGE" compose version >/dev/null
echo "    tooling: age, sops, docker, docker compose all runnable"

# 4. The keygen entrypoint route generates a usable AGE key.
keyout="$(run keygen)"
echo "$keyout" | grep -q "Public key: age1" || { echo "::error::keygen did not emit an AGE public key"; exit 1; }
echo "    keygen: produced an AGE public key"

# 5. End-to-end age+sops encrypt/decrypt round trip inside the image.
docker run --rm --entrypoint bash "$IMAGE" -c '
  set -euo pipefail
  cd "$(mktemp -d)"
  mkdir -p .secrets
  printf "FOO=bar\nBAZ=qux\n" > .secrets/.env
  age-keygen -o .secrets/.key >/dev/null 2>&1
  pub=$(age-keygen -y .secrets/.key | sed "s/^Public key: //")
  sops -e -a "$pub" --input-type dotenv --output secrets.enc .secrets/.env
  export SOPS_AGE_KEY=$(grep "^AGE-SECRET-KEY-" .secrets/.key)
  out=$(sops -d --input-type dotenv --output-type dotenv secrets.enc)
  echo "$out" | grep -q "FOO=bar" || { echo "::error::round-trip decrypted output mismatch: $out"; exit 1; }
'
echo "    crypto: age+sops encrypt/decrypt round trip OK"

echo "==> All runtime smoke tests passed"
