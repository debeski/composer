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
for flag in --down --purge --volumes --build --force --status-file; do
  echo "$help" | grep -q -- "$flag" || { echo "::error::--help is missing '$flag'"; exit 1; }
done
echo "$help" | grep -q -- "run " || { echo "::error::--help is missing the 'run' subcommand"; exit 1; }
echo "$help" | grep -q -- "restart " || { echo "::error::--help is missing the 'restart' subcommand"; exit 1; }
echo "$help" | grep -q -- "watch " || { echo "::error::--help is missing the 'watch' subcommand"; exit 1; }
for subcommand in run restart update pull update-self stop check agent-check agent-update agent-restart agent-off log watch agent enable-agent; do
  run "$subcommand" --help >/dev/null || {
    echo "::error::'$subcommand --help' failed"
    exit 1
  }
done
run -r --help >/dev/null || { echo "::error::'-r --help' restart alias failed"; exit 1; }
echo "    help: all expected flags present"

# 3. Bundled tooling is present and runnable inside the image.
docker run --rm --entrypoint docker "$IMAGE" --version >/dev/null
docker run --rm --entrypoint docker "$IMAGE" compose version >/dev/null
echo "    tooling: docker, docker compose runnable"

# 4. Runtime overrides and Compose config must work with a read-only project,
# read-only image filesystem, no Linux capabilities, and writable /tmp only.
docker run --rm --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --tmpfs /tmp \
  -v "$PWD/tests/fixtures:/workspace:ro" \
  -w /workspace \
  --entrypoint python "$IMAGE" -c '
from pathlib import Path
from composer.launcher import DockerComposeLauncher

launcher = DockerComposeLauncher()
launcher.services = ["web"]
launcher.active_compose_files = ["read-only-compose.yml"]
assert launcher.sync_runtime_compose_override(), launcher.last_runtime_diagnostic
override = launcher.compose_runtime_override
assert override is not None and override.parent == Path("/tmp")
assert override.exists()
ok, out, err = launcher.run_docker_compose(["config"])
assert ok, err or out
assert "COMPOSER_VERSION" in out
assert "/workspace/data" in out
launcher.remove_runtime_compose_override()
assert not override.exists()
'
echo "    runtime override: merged from /tmp with read-only project and zero capabilities"

# 5. A resident updater can consume wrapper-inherited secrets without opening
# the project file again; missing declared keys remain a hard failure.
docker run --rm \
  -e COMPOSER_INHERITED_SECRET_KEYS=POSTGRES_PASSWORD,OPTIONAL_EMPTY \
  -e POSTGRES_PASSWORD=smoke-secret \
  -e OPTIONAL_EMPTY= \
  --entrypoint python "$IMAGE" -c '
from composer.launcher import DockerComposeLauncher

launcher = DockerComposeLauncher()
launcher.active_compose_files = ["compose.yml"]
launcher.required_compose_vars = lambda: set()
launcher.plaintext_env_candidates = lambda: (_ for _ in ()).throw(
    AssertionError("resident updater reopened the project secrets file")
)
ok, error = launcher.resolve_secrets()
assert ok, error
assert launcher.secrets_source == "inherited launcher environment"
'
echo "    secrets: inherited resident environment accepted without project-file access"

# 6. Dedicated lifecycle commands must target only composer-agent.
agent_compose="$PWD/tests/fixtures/agent-lifecycle-compose.yml"
agent_lifecycle() {
  docker run --rm \
    -e COMPOSER_INHERITED_SECRET_KEYS=SMOKE_VALUE \
    -e SMOKE_VALUE=smoke \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$PWD/tests/fixtures:/workspace:ro" \
    -w /workspace \
    "$IMAGE" "$@" -f agent-lifecycle-compose.yml
}
cleanup_agent_lifecycle() {
  docker compose -f "$agent_compose" down >/dev/null 2>&1 || true
}
trap cleanup_agent_lifecycle EXIT
agent_lifecycle agent-update
agent_lifecycle agent-restart
agent_lifecycle agent-off
running="$(docker compose -f "$agent_compose" ps --status running --services)"
[ -z "$running" ] || { echo "::error::agent-off left services running: $running"; exit 1; }
cleanup_agent_lifecycle
trap - EXIT
echo "    agent lifecycle: update, restart, and off target composer-agent only"

echo "==> All runtime smoke tests passed"
