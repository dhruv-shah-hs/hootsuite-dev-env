#!/usr/bin/env python3
"""
TTY helper: from `hootsuite-dev-env` root, optionally prompt whether to show the local
`primary_commands.run` command, then whether you will run that command yourself (wording
tailored for self-run vs paste/background). Uses `.cursor/context/service-context.json` when
present (see `resolve_service` / `align-branch` to refresh it).

Run this after `pick-task` + `save-task-context` when you want the interactive flow; it is
not invoked by `pick-task`.

Environment (any skips the prompt when set to 1/true/yes):
  START_SERVICE_NO_PROMPT
  PICK_TASK_NO_RUN_SERVICE_PROMPT  (legacy name; same behavior)

Skipped when stdout or stdin is not a TTY (e.g. automation) or when skipped via env.

Examples:
  python3 .cursor/tools/start-service.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_CURSOR_DIR = Path(__file__).resolve().parent.parent
if str(_CURSOR_DIR) not in sys.path:
    sys.path.insert(0, str(_CURSOR_DIR))

from lib.service_context import service_context_path  # noqa: E402


def _skip_prompt_via_env() -> bool:
    for key in ("START_SERVICE_NO_PROMPT", "PICK_TASK_NO_RUN_SERVICE_PROMPT"):
        if os.environ.get(key, "").strip().lower() in ("1", "true", "yes"):
            return True
    return False


def resolve_run_command(cwd: Path | None = None) -> str:
    """Best-effort `primary_commands.run` from service-context.json, else make run fallback."""
    root = (cwd or Path.cwd()).resolve()
    ctx_path = service_context_path(root)
    if ctx_path.is_file():
        try:
            doc = json.loads(ctx_path.read_text(encoding="utf-8"))
            pc = doc.get("primary_commands")
            if isinstance(pc, dict):
                raw = pc.get("run")
                if isinstance(raw, str) and raw.strip():
                    return raw.strip()
        except (OSError, json.JSONDecodeError):
            pass
    return "cd ../service-entitlement && make run"


def maybe_prompt_run_service_interactive(cwd: Path | None = None) -> None:
    """
    In an interactive terminal from dev-env root, optionally ask whether to show the run command,
    then whether the user will run it themselves (so the hint text matches intent).
    """
    if _skip_prompt_via_env():
        return
    if not (sys.stdout.isatty() and sys.stdin.isatty()):
        return

    run_cmd = resolve_run_command(cwd)

    print("\nRun the local service now? [y/N] ", end="", file=sys.stderr, flush=True)
    try:
        line = input()
    except (EOFError, KeyboardInterrupt):
        print("", file=sys.stderr)
        return
    if line.strip().lower() not in ("y", "yes"):
        return

    print(
        "\nWill you run this start command yourself in your terminal? [Y/n] "
        "(Y = instructions for pasting yourself; n = same command, phrased for a new tab / background / agent)\n> ",
        end="",
        file=sys.stderr,
        flush=True,
    )
    try:
        line2 = input()
    except (EOFError, KeyboardInterrupt):
        print("", file=sys.stderr)
        return
    self_run = line2.strip().lower() in ("", "y", "yes")
    if self_run:
        print(
            "\nRun this yourself from the hootsuite-dev-env repo root "
            "(so cd ../service-entitlement in the command resolves):\n"
            f"  {run_cmd}\n",
            file=sys.stderr,
        )
    else:
        print(
            "\nStart command — open Terminal → New Terminal or use a background/agent shell "
            "from dev-env root:\n"
            f"  {run_cmd}\n",
            file=sys.stderr,
        )


def main() -> None:
    maybe_prompt_run_service_interactive()


if __name__ == "__main__":
    main()
