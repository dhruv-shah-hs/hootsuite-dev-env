"""Shared helpers for pick-task / save-task-context / checkout-jira-branch."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lib.git import branches_with_prefix, current_branch_name, git_lines, run_git

_CURSOR_DIR = Path(__file__).resolve().parent.parent


def task_context_file_path(workspace_root: Path) -> Path:
    """Path to ``current-task.local.json`` for a workspace root (folder containing ``.cursor``)."""
    return workspace_root.resolve() / ".cursor" / "context" / "current-task.local.json"


def task_context_path() -> Path:
    """Task context file for this dev-env checkout (the repo that contains ``.cursor/lib``)."""
    return task_context_file_path(_CURSOR_DIR.parent)


def load_task_context_document(path: Path | None = None) -> dict[str, Any]:
    """Load the full JSON document from disk.

    Raises:
        FileNotFoundError: File missing.
        json.JSONDecodeError: Invalid JSON.
        ValueError: Root value is not a JSON object.
    """
    p = task_context_path() if path is None else path
    if not p.is_file():
        raise FileNotFoundError(
            f"Missing task context file: {p} (run save-task-context after pick-task)"
        )
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid task context document (expected JSON object): {p}")
    return raw


def extract_task_from_document(
    doc: dict[str, Any],
    *,
    path_for_errors: Path | None = None,
) -> dict[str, Any]:
    """Return the ``task`` object from a task context document."""
    task = doc.get("task")
    if not isinstance(task, dict):
        loc = str(path_for_errors) if path_for_errors is not None else "task context document"
        raise ValueError(f"{loc} has no 'task' object")
    return task


def extract_task_object(parsed: Any) -> dict[str, Any]:
    if isinstance(parsed, dict) and isinstance(parsed.get("task"), dict):
        return parsed["task"]
    if isinstance(parsed, dict) and isinstance(parsed.get("id"), str) and isinstance(parsed.get("label"), str):
        return parsed
    raise ValueError("JSON must be a task object with id and label, or {\"task\": {...}}")


def try_load_task_context_document(path: Path) -> dict[str, Any] | None:
    """Return the parsed document, or ``None`` if missing, unreadable, or not a JSON object."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def task_from_document_optional(doc: dict[str, Any]) -> dict[str, Any] | None:
    """Return ``task`` if present and is an object; otherwise ``None``."""
    task = doc.get("task")
    return task if isinstance(task, dict) else None


def read_current_task_for_workspace(workspace_root: Path) -> dict[str, Any] | None:
    """Best-effort ``task`` dict from a workspace's ``current-task.local.json``."""
    path = task_context_file_path(workspace_root)
    doc = try_load_task_context_document(path)
    if doc is None:
        return None
    return task_from_document_optional(doc)


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


def load_task_from_context_file(path: Path | None = None) -> dict[str, Any]:
    """Read ``task`` from ``current-task.local.json`` (raises on missing file or invalid JSON)."""
    p = task_context_path() if path is None else path
    try:
        doc = load_task_context_document(p)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {p}: {e}") from e
    return extract_task_from_document(doc, path_for_errors=p)
