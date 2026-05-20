#!/usr/bin/env python3
"""
TTY helper: from `hootsuite-dev-env` root, optionally prompt whether to show local start
commands from `.cursor/context/service-context.json` (multi-service `services` map).

Run after `pick-task` + `save-task-context` when you want the interactive flow.

Environment (skips prompts when set to 1/true/yes):
  START_SERVICE_NO_PROMPT
  PICK_TASK_NO_RUN_SERVICE_PROMPT  (legacy)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_CURSOR_DIR = Path(__file__).resolve().parent.parent
if str(_CURSOR_DIR) not in sys.path:
    sys.path.insert(0, str(_CURSOR_DIR))

from lib.service_context import (  # noqa: E402
    get_service_instance,
    list_runnable_service_instances,
    load_service_context,
    resolve_run_command as resolve_run_command_for_instance,
)


def _skip_prompt_via_env() -> bool:
    for key in ("START_SERVICE_NO_PROMPT", "PICK_TASK_NO_RUN_SERVICE_PROMPT"):
        if os.environ.get(key, "").strip().lower() in ("1", "true", "yes"):
            return True
    return False


def resolve_run_command(cwd: Path | None = None, service_id: str | None = None) -> str:
    """Primary (or named) service: prefer run, then start; else entitlement fallback."""
    root = (cwd or Path.cwd()).resolve()
    doc = load_service_context(root)
    if doc:
        inst = get_service_instance(doc, service_id)
        if inst:
            cmd = resolve_run_command_for_instance(inst)
            if cmd:
                return cmd
    return "cd ../service-entitlement && make run"


def resolve_all_run_commands(cwd: Path | None = None) -> list[tuple[str, str]]:
    """(service_id, command) for each runnable instance with a run/start command."""
    root = (cwd or Path.cwd()).resolve()
    doc = load_service_context(root)
    if not doc:
        return []
    out: list[tuple[str, str]] = []
    for inst in list_runnable_service_instances(doc):
        sid = inst.get("id")
        cmd = resolve_run_command_for_instance(inst)
        if isinstance(sid, str) and cmd:
            out.append((sid, cmd))
    return out


def maybe_prompt_run_service_interactive(cwd: Path | None = None) -> None:
    if _skip_prompt_via_env():
        return
    if not (sys.stdout.isatty() and sys.stdin.isatty()):
        return

    commands = resolve_all_run_commands(cwd)
    if not commands:
        commands = [("primary", resolve_run_command(cwd))]

    print("\nRun local service(s) now? [y/N] ", end="", file=sys.stderr, flush=True)
    try:
        line = input()
    except (EOFError, KeyboardInterrupt):
        print("", file=sys.stderr)
        return
    if line.strip().lower() not in ("y", "yes"):
        return

    print(
        "\nWill you run these start commands yourself in your terminal? [Y/n]\n> ",
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
    header = (
        "\nRun these yourself from the hootsuite-dev-env repo root:\n"
        if self_run
        else "\nStart commands — use Terminal → New Terminal or background/agent shells from dev-env root:\n"
    )
    print(header, file=sys.stderr)
    for sid, cmd in commands:
        print(f"  [{sid}] {cmd}", file=sys.stderr)
    print("", file=sys.stderr)


def main() -> None:
    maybe_prompt_run_service_interactive()


if __name__ == "__main__":
    main()
