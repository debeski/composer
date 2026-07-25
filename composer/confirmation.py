import os
import sys
from typing import List, Optional, Sequence

ASSUME_YES_ENV = "COMPOSER_ASSUME_YES"
ACCEPTED_ANSWERS = {"y", "yes"}
TRUTHY = {"1", "true", "yes", "y", "on"}


def assume_yes_from_env(environ: Optional[dict] = None) -> bool:
    source = os.environ if environ is None else environ
    return str(source.get(ASSUME_YES_ENV, "")).strip().lower() in TRUTHY


def confirm(
    action: str,
    consequences: Sequence[str] = (),
    *,
    assume_yes: bool = False,
    reader=input,
    interactive: Optional[bool] = None,
    stream=None,
) -> bool:
    """Require an explicit `y`/`yes` before a destructive action.

    Returns True when the action may proceed. `-y`/`--yes` and
    COMPOSER_ASSUME_YES skip the prompt; a non-interactive stdin without either
    fails closed so scripted runs never destroy data by default.
    """
    if assume_yes or assume_yes_from_env():
        return True

    out = stream or sys.stderr
    if interactive is None:
        try:
            interactive = sys.stdin.isatty()
        except (AttributeError, ValueError):
            interactive = False
    detail: List[str] = [f"   - {item}" for item in consequences]
    if not interactive:
        print(
            "\n".join(
                [f"✖ {action}", *detail]
                + [
                    "   Confirmation is required but stdin is not interactive; pass",
                    f"   -y/--yes (or set {ASSUME_YES_ENV}=1) to confirm up front.",
                ]
            ),
            file=out,
        )
        return False

    print("\n".join([f"⚠ {action}", *detail]), file=out, flush=True)
    try:
        answer = reader("   Type 'y' or 'yes' to continue: ")
    except (EOFError, KeyboardInterrupt):
        print("", file=out)
        answer = ""
    if str(answer).strip().lower() in ACCEPTED_ANSWERS:
        return True
    print("✖ Aborted; nothing was changed.", file=out)
    return False
