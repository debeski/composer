"""Turn a Git tag into a publication decision.

The tag is the only input. No commit-message keyword, no workflow toggle, no
branch convention — a publication that can be steered by prose eventually gets
steered by a typo.

Mirrors ``dlux/updater/release_check.py``'s classifier, because the two projects
release in lockstep and an operator should not have to remember which repository
spells a beta differently.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .versions import InvalidVersion, Version, try_parse

STABLE = "stable"
BETA = "beta"


class ReleaseTagError(RuntimeError):
    """The tag cannot be published as written."""


def classify_tag(tag, *, version_file_value="") -> dict:
    raw = str(tag or "").strip()
    if not raw.startswith("v"):
        raise ReleaseTagError(f"Release tag {raw!r} must start with 'v'.")
    text = raw[1:]
    try:
        version = Version(text)
    except InvalidVersion as exc:
        raise ReleaseTagError(f"Release tag {raw!r} is not a valid PEP 440 version.") from exc
    if str(version) != text:
        raise ReleaseTagError(f"Release tag {raw!r} is not canonical; tag v{version} instead.")
    if version.is_devrelease:
        raise ReleaseTagError(f"Release tag {raw!r} is a development release and is never published.")
    if version.local:
        raise ReleaseTagError(f"Release tag {raw!r} carries a local version segment.")
    if version.is_postrelease:
        raise ReleaseTagError(
            f"Release tag {raw!r} is a post-release. Publish a new patch version instead."
        )
    if version.epoch:
        raise ReleaseTagError(f"Release tag {raw!r} declares an epoch, which this project does not use.")
    if version_file_value:
        declared = str(version_file_value).strip()
        if declared != text:
            raise ReleaseTagError(
                f"Release tag {raw!r} does not match VERSION ({declared}). "
                "Bump VERSION before tagging."
            )
    prerelease = bool(version.is_prerelease)
    return {
        "tag": raw,
        "version": text,
        "channel": BETA if prerelease else STABLE,
        "prerelease": prerelease,
        "make_latest": not prerelease,
        # The moving alias this release claims. A beta claims `:beta`; a stable
        # claims `:latest` and *may* also claim `:beta` — see should_advance_beta.
        "alias": BETA if prerelease else "latest",
    }


def should_advance_beta(new_version, current_beta_version, *, beta_exists=True) -> bool:
    """May a stable release also take the ``:beta`` alias?

    Yes, when it is genuinely newer than whatever ``:beta`` points at now — a
    beta-channel deployment should receive final releases too, or it would sit
    on 1.4.0b3 for ever once 1.4.0 shipped.

    No, when ``:beta`` already points somewhere newer. Publishing stable 1.4.1
    must not drag a deployment back off 1.5.0b1: the beta channel is *ahead* of
    stable, and an alias that can move backwards makes "update" mean "downgrade"
    without anyone asking for one.

    ``beta_exists`` separates the two ways "no current version" happens, and
    they want opposite answers:

    * the alias does not exist at all — nothing to protect, so claim it;
    * the alias exists but its version could not be read — refuse. Overwriting
      an image whose version is unknown is exactly the move this function
      exists to prevent, and "I could not check" is not evidence that it is
      safe. A beta line that stops receiving finals is visible and recoverable;
      a silently downgraded one is neither.
    """
    new = try_parse(new_version)
    if new is None:
        return False
    if not beta_exists:
        return True
    current = try_parse(current_beta_version)
    if current is None:
        return False
    return new > current


def changelog_section(version, path="CHANGELOG.md") -> str:
    """The body of this exact version's ``## vX.Y.Z`` section.

    Whole-heading match: ``^## v1.3.14`` also prefix-matches ``## v1.3.14b1``, so
    a prefix test lets a stable release publish its beta's notes and look right
    doing it.
    """
    wanted = f"## v{version}"
    body = []
    grabbing = False
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        stripped = line.rstrip()
        if stripped.startswith("## "):
            if grabbing:
                break
            grabbing = stripped == wanted
            continue
        if grabbing:
            body.append(line)
    return "\n".join(body).strip()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="composer.release_tag")
    parser.add_argument("tag", nargs="?", default=os.getenv("GITHUB_REF_NAME", ""))
    parser.add_argument("--version-file", default="VERSION")
    parser.add_argument("--github-output", action="store_true")
    parser.add_argument(
        "--current-beta", default="",
        help="Version the :beta alias points at now, for the advance decision.",
    )
    parser.add_argument(
        "--beta-exists", default="",
        help=(
            "Whether the :beta alias exists at all ('true'/'false'). An alias "
            "that exists but cannot be read is never overwritten; one that does "
            "not exist is free to claim."
        ),
    )
    args = parser.parse_args(argv)

    try:
        declared = Path(args.version_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        print(f"::error::Could not read {args.version_file}: {exc}", file=sys.stderr)
        return 1
    try:
        decision = classify_tag(args.tag, version_file_value=declared)
    except ReleaseTagError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    if not changelog_section(decision["version"]):
        print(
            f"::error::CHANGELOG.md has no '## v{decision['version']}' section.",
            file=sys.stderr,
        )
        return 1
    # Default when the caller says nothing: assume the alias exists, which is
    # the cautious reading — an unreadable alias is then not overwritten.
    beta_exists = str(args.beta_exists or "").strip().lower() != "false"
    decision["advance_beta"] = decision["prerelease"] or should_advance_beta(
        decision["version"], args.current_beta, beta_exists=beta_exists,
    )
    print(json.dumps(decision, sort_keys=True))
    output = os.getenv("GITHUB_OUTPUT")
    if args.github_output and output:
        with open(output, "a", encoding="utf-8") as handle:
            for key, value in decision.items():
                handle.write(f"{key}={'true' if value is True else 'false' if value is False else value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
