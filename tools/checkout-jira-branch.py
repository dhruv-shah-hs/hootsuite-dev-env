#!/usr/bin/env python3
"""
Pick a Jira task (via pick-task), then check out a git branch named
  JIRA-TICKET-ID_<suffix>
If no branch exists with that prefix, prompt for <suffix> and create the branch.

Uses ./pick-task from the same directory for task selection (--id, --pick, or interactive).

Examples:
  ./checkout-jira-branch
  ./checkout-jira-branch --id ENG-1234
  ./checkout-jira-branch --pick 2
  ./checkout-jira-branch --id ENG-1234 --dry-run-json   # plan only (agents / no TTY)

--dry-run-json prints JSON describing Jira key, branch prefix, matching branches,
current HEAD branch, and what checkout would do — without prompting or mutating git.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional


def script_dir() -> Path:
    return Path(__file__).resolve().parent


def run_pick_task(pick: Optional[int], issue_id: Optional[str]) -> dict:
    pick_task = script_dir() / "pick-task"
    if not pick_task.is_file():
        sys.exit(f"Missing {pick_task}")
    cmd = [str(pick_task)]
    if pick is not None:
        cmd.extend(["--pick", str(pick)])
    elif issue_id:
        cmd.extend(["--id", issue_id])
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        sys.exit(err or "pick-task failed")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        sys.exit(f"Could not parse pick-task output as JSON: {e}")


def run_git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=check,
    )


def git_lines(*args: str) -> list[str]:
    p = run_git(*args, check=False)
    if p.returncode != 0:
        return []
    return [ln.strip() for ln in p.stdout.splitlines() if ln.strip()]


def branches_with_prefix(prefix: str) -> list[str]:
    """Local and remote-tracking branch short names starting with prefix (deduped, sorted)."""
    names: set[str] = set()
    for ref in git_lines("for-each-ref", "--format=%(refname:short)", "refs/heads/"):
        if ref.startswith(prefix):
            names.add(ref)
    for ref in git_lines("for-each-ref", "--format=%(refname:short)", "refs/remotes/"):
        if ref.startswith("origin/"):
            short = ref[len("origin/") :]
            if short.startswith(prefix):
                names.add(short)
    return sorted(names)


def checkout_branch(name: str) -> None:
    p = run_git("checkout", name, check=False)
    if p.returncode == 0:
        print(f"Checked out {name}", file=sys.stderr)
        return
    err = (p.stderr or p.stdout or "").strip()
    sys.exit(f"git checkout failed: {err}")


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


def current_branch_name() -> str:
    p = run_git("branch", "--show-current", check=False)
    if p.returncode != 0:
        return ""
    return (p.stdout or "").strip()


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
    p.add_argument("--id", metavar="KEY", help="Jira issue key (skip interactive list)")
    p.add_argument("--pick", type=int, metavar="N", help="Select Nth task from pick-task list (1-based)")
    p.add_argument(
        "--dry-run-json",
        action="store_true",
        help="Print JSON plan only (no git checkout; no prompts). For agents / automation.",
    )
    args = p.parse_args()
    if args.pick is not None and args.id:
        sys.exit("Use only one of --pick or --id")
    if args.dry_run_json and args.pick is None and not args.id:
        sys.exit("With --dry-run-json, pass --id ISSUE-KEY or --pick N (non-interactive).")

    data = run_pick_task(args.pick, args.id)
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
