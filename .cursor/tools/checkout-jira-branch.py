#!/usr/bin/env python3
"""
Read the selected task from `.cursor/task-context/current-task.local.json`
(save-task-context / pick-task workflow), then check out a git branch named
  JIRA-TICKET-ID_<suffix>
If no branch exists with that prefix, prompt for <suffix> and create the branch.

Examples:
  ./checkout-jira-branch
  ./checkout-jira-branch --dry-run-json   # plan only (agents / no TTY)

--dry-run-json prints JSON describing Jira key, branch prefix, matching branches,
current HEAD branch, and what checkout would do — without prompting or mutating git.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

_CURSOR_DIR = Path(__file__).resolve().parent.parent
if str(_CURSOR_DIR) not in sys.path:
    sys.path.insert(0, str(_CURSOR_DIR))

from lib.git import (  # noqa: E402
    branches_with_prefix,
    checkout_branch,
    current_branch_name,
    run_git,
)


def task_context_path() -> Path:
    return _CURSOR_DIR / "task-context" / "current-task.local.json"


def load_task_data() -> dict[str, Any]:
    path = task_context_path()
    if not path.is_file():
        sys.exit(
            f"Missing task context file: {path} (run save-task-context after pick-task)"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"Invalid JSON in {path}: {e}")
    task = raw.get("task")
    if not isinstance(task, dict):
        sys.exit(f"{path} has no 'task' object")
    return task


def prompt_suffix(prefix: str) -> str:
    print(
        "Branch name format: <JIRA-TICKET-ID>_<your-descriptor>",
        file=sys.stderr,
    )
    print(
        f"Enter the descriptor (full branch will be {prefix}<descriptor>):",
        file=sys.stderr,
    )
    try:
        line = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("", file=sys.stderr)
        sys.exit(1)
    if not line:
        sys.exit("Suffix is empty; aborting.")
    if "/" in line or line.startswith(".") or ".." in line:
        sys.exit("Invalid suffix: avoid '/', leading '.', and '..'.")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", line):
        sys.exit("Use only letters, digits, ._- in the suffix.")
    return line


def pick_from_list(items: list[str], label: str) -> str:
    print(f"{label}\n", file=sys.stderr)
    for i, name in enumerate(items, start=1):
        print(f"  {i}) {name}", file=sys.stderr)
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("", file=sys.stderr)
            sys.exit(1)
        if not line.isdigit():
            print("Enter a number from the list.", file=sys.stderr)
            continue
        n = int(line)
        if 1 <= n <= len(items):
            return items[n - 1]
        print(f"Choose between 1 and {len(items)}.", file=sys.stderr)


def dry_run_payload(data: dict) -> dict:
    """Describe Jira ↔ branch linkage without prompting or changing git."""
    jira_key = (data.get("jira_key") or data.get("id") or "").strip()
    if not jira_key:
        return {
            "ok": False,
            "error": "Selected task has no jira_key",
            "jira_key": "",
        }
    prefix = f"{jira_key}_"
    matches = branches_with_prefix(prefix)
    head = current_branch_name()
    if len(matches) == 0:
        action = "would_prompt_new_branch"
        detail = (
            "No local or origin/* branch starts with this prefix; interactive run would "
            "prompt for a suffix and run git checkout -b <prefix><suffix>. "
            "Suffix rules: letters, digits, ._- only; no '/', leading '.', or '..'."
        )
    elif len(matches) == 1:
        action = "would_checkout"
        detail = f"Single match; interactive run would git checkout {matches[0]}."
    else:
        action = "would_prompt_pick_branch"
        detail = (
            "Multiple branches share this prefix; interactive run would list them and "
            "wait for a numeric choice."
        )
    aligned = head in matches if matches else False
    return {
        "ok": True,
        "jira_key": jira_key,
        "branch_prefix": prefix,
        "matching_branches": matches,
        "current_branch": head,
        "branch_aligned_with_jira": aligned,
        "planned_action": action,
        "planned_action_detail": detail,
        "suffix_validation": {
            "pattern": r"[A-Za-z0-9._-]+",
            "reject_substrings": ["/", ".."],
            "reject_prefix": ".",
        },
    }


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--dry-run-json",
        action="store_true",
        help="Print JSON plan only (no git checkout; no prompts). For agents / automation.",
    )
    args = p.parse_args()

    data = load_task_data()
    if args.dry_run_json:
        payload = dry_run_payload(data)
        print(json.dumps(payload, indent=2))
        sys.exit(0 if payload.get("ok") else 1)

    jira_key = (data.get("jira_key") or data.get("id") or "").strip()
    if not jira_key:
        sys.exit("Selected task has no jira_key")

    prefix = f"{jira_key}_"
    matches = branches_with_prefix(prefix)

    if len(matches) == 0:
        suffix = prompt_suffix(prefix)
        full = f"{prefix}{suffix}"
        proc = run_git("checkout", "-b", full, check=False)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            sys.exit(f"git checkout -b failed: {err}")
        print(f"Created and checked out {full}", file=sys.stderr)
        print(full)
        return

    if len(matches) == 1:
        checkout_branch(matches[0])
        print(matches[0])
        return

    chosen = pick_from_list(matches, "Multiple branches match; pick one:")
    checkout_branch(chosen)
    print(chosen)


if __name__ == "__main__":
    main()
