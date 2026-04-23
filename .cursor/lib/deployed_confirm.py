"""Shared prompt when task.is_deployed is True (Jira Done)."""

from __future__ import annotations

import os
import sys


def _skip_confirm() -> bool:
    v = (os.environ.get("CURSOR_SKIP_DEPLOYED_CONFIRM") or "").strip().lower()
    return v in ("1", "true", "yes", "y")


def confirm_continue_if_deployed_complete(task: dict, *, program: str) -> None:
    """
    If task['is_deployed'] is True, notify and ask whether to continue when stdin+stderr are TTYs.

    Exits with status 1 if the user declines. Non-interactive stdin: returns without prompting
    (caller may still proceed — e.g. piping after pick-task already confirmed).

    Env: CURSOR_SKIP_DEPLOYED_CONFIRM=1 — skip notification and confirmation (always continue).
    """
    if task.get("is_deployed") is not True:
        return
    if _skip_confirm():
        return

    key = (task.get("jira_key") or task.get("id") or "this issue").strip()

    if not sys.stdin.isatty() or not sys.stderr.isatty():
        return

    print(
        f"{program}: {key} is Done. Continue? [y/N] ",
        file=sys.stderr,
        end="",
        flush=True,
    )
    try:
        line = input()
    except EOFError:
        sys.exit(f"{program}: aborted (EOF).")

    if (line or "").strip().lower() not in ("y", "yes"):
        sys.exit(f"{program}: aborted.")
