"""Shared helpers for pick-task / save-task-context / checkout-jira-branch."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lib.git import branches_with_prefix, current_branch_name, git_lines, run_git

_CURSOR_DIR = Path(__file__).resolve().parent.parent


def task_context_path() -> Path:
    return _CURSOR_DIR / "task-context" / "current-task.local.json"


def extract_task_object(parsed: Any) -> dict[str, Any]:
    if isinstance(parsed, dict) and isinstance(parsed.get("task"), dict):
        return parsed["task"]
    if isinstance(parsed, dict) and isinstance(parsed.get("id"), str) and isinstance(parsed.get("label"), str):
        return parsed
    raise ValueError("JSON must be a task object with id and label, or {\"task\": {...}}")


def dry_run_branch_alignment(task: dict[str, Any]) -> dict[str, Any]:
    """Same shape as checkout-jira-branch --dry-run-json (for persisting on save)."""
    jira_key = (str(task.get("jira_key") or task.get("id") or "")).strip()
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


def git_repo_snapshot() -> dict[str, Any]:
    branch = current_branch_name()
    dirty = bool(git_lines("status", "--porcelain"))
    return {"git_branch": branch, "git_dirty_hint": dirty}


def load_task_from_context_file() -> dict[str, Any]:
    """Read task from current-task.local.json (used by checkout-jira-branch)."""
    path = task_context_path()
    if not path.is_file():
        raise FileNotFoundError(
            f"Missing task context file: {path} (run save-task-context after pick-task)"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {path}: {e}") from e
    task = raw.get("task")
    if not isinstance(task, dict):
        raise ValueError(f"{path} has no 'task' object")
    return task
