# Composer — project rules

Composer (`debeski/composer`) deploys and maintains DjangoLux stacks: the deployer
wrapper, the resident `composer-agent`/`composer-executor` pair, inline DjangoLux
updates, `check`/`check --fix`, the egress relay and deployment operations. It is
the home for anything a stack needs that is not DjangoLux itself (the maintenance
page moves here in DjangoLux 1.13.0).

Project state, tasks and known bugs: see `tracker.md`. These rules are
self-contained so a fresh clone, a cloud session or Codex can read them.

## Branches

- **One feature per branch, from current `main`.** Never develop on `main` or reuse
  another active feature's branch. Merge only after implementation, docs and tests
  pass.
- **Branch names are `<release line>/<work>`** in snake_case, using this repo's
  own release line: `1.7/maintenance_page`, `1.7/operations`. Its checkout
  directory is the last path segment (`../maintenance_page`) under
  `~/Desktop/depy/app-composer/`.
- **Retire merged branches without asking.** Once a branch is merged into `main`
  and safe to discard, move its ignored leftovers (`.xclude/`, venv links, scratch
  output) into `main/.xclude/retired-worktrees/<date>/<checkout>/`,
  `git worktree remove` the checkout, and delete the branch locally and on GitHub.
  Stop and ask only when something blocks it (unmerged commits, a conflict,
  uncommitted work). This overrides any personal rule asking for approval before
  each worktree removal.

## Keep in step with DjangoLux

Composer mirrors DjangoLux contracts and must follow them in the same release
cycle, never a release later:

- the stack contract and stamps (`composer/stack_schema.py` mirrors
  `dlux.contracts.stack`); the release manifest `requires` keys (unknown keys fail
  closed, so a new DjangoLux key needs Composer support first);
- the updater/ops request protocol (`state/` files, `docs/agent-protocol-v1.md`,
  DjangoLux `dlux.updater.ops.OPERATIONS`);
- relay operations shared with `dlux.relay` (`relay_weather_ops.json`,
  `relay_projection_cases.json`);
- the maintenance page (until 1.13.0 moves it here for good).

When a DjangoLux change needs Composer, cut the paired Composer beta alongside it,
accept both together on `~/Desktop/depy/testbed-dlux`, and record the pairing
(versions and floors such as `services.composer`) in both trackers.

## Releases

- Every stable release is a beta first (`vX.Y.ZbN`, image `:beta`). Never tag or
  publish without the owner's OK.
- A cross-repo floor names the other release's first prerelease (`>=1.7.0b1`), or
  the two betas cannot be tested against each other.

## Checks

- `python -m unittest discover -s tests -v`
- `./scripts/smoke-test.sh <image>` for image changes (CI runs it on every build).
