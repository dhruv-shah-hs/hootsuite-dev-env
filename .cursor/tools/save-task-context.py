#!/usr/bin/env python3
"""
Write `.cursor/task-context/current-task.local.json` from pick-task JSON.

Reads a single task object (same shape as `pick-task.py` stdout) or a wrapper
`{"task": {...}}` from stdin or a file, validates `id` / `label`, then writes
the schema-shaped document including optional branch dry-run and git snapshot.

Examples:
  python3 .cursor/tools/pick-task.py --id PROJ-123 | python3 .cursor/tools/save-task-context.py --stdin
  python3 .cursor/tools/save-task-context.py --stdin < task.json
  python3 .cursor/tools/save-task-context.py path/to/task.json

Run from the repository root so `./.env` is found by other tools when needed.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_CURSOR_DIR = Path(__file__).resolve().parent.parent
if str(_CURSOR_DIR) not in sys.path:
    sys.path.insert(0, str(_CURSOR_DIR))

from lib.task_context import (  # noqa: E402
    dry_run_branch_alignment,
    extract_task_object,
    git_repo_snapshot,
    task_context_path,
)
from lib.workspace_context import write_workspace_context  # noqa: E402


def validate_task(task: dict[str, Any]) -> None:
    tid = task.get("id")
    label = task.get("label")
    if not isinstance(tid, str) or not tid.strip():
        sys.exit("task must have a non-empty string 'id' (Jira key or issue id)")
    if not isinstance(label, str) or not label.strip():
        sys.exit("task must have a non-empty string 'label'")


def read_input_json(path: Path | None, use_stdin: bool) -> Any:
    if use_stdin:
        raw = sys.stdin.read()
    elif path is not None:
        raw = path.read_text(encoding="utf-8")
    else:
        sys.exit("Provide --stdin or a path to a JSON file")
    raw = raw.strip()
    if not raw:
        sys.exit("Input is empty")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        sys.exit(f"Invalid JSON: {e}")


def build_document(task: dict[str, Any], *, skip_repo_metadata: bool) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "$schema": "./tasks.schema.json",
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": "save-task-context",
        "task": task,
    }
    if not skip_repo_metadata:
        doc["branch_alignment"] = dry_run_branch_alignment(task)
        doc["repo_snapshot"] = git_repo_snapshot()
        doc["repo_fit"] = {"status": "unknown", "notes": None}
    return doc


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--stdin",
        action="store_true",
        help="Read JSON from stdin (e.g. after piping pick-task.py)",
    )
    p.add_argument(
        "file",
        nargs="?",
        type=Path,
        help="Optional path to JSON file (ignored if --stdin)",
    )
    p.add_argument(
        "--skip-repo-metadata",
        action="store_true",
        help="Omit branch_alignment, repo_snapshot, and repo_fit (task + timestamps only)",
    )
    p.add_argument(
        "--print-json",
        action="store_true",
        help="Echo the written document to stdout (default: stderr path only)",
    )
    p.add_argument(
        "--no-workspace-context",
        action="store_true",
        help="Skip refreshing .cursor/task-context/workspace-context.json after saving the task",
    )
    args = p.parse_args()

    if args.stdin and args.file is not None:
        sys.exit("Use either --stdin or a file path, not both")

    parsed = read_input_json(args.file, args.stdin)
    try:
        task = extract_task_object(parsed)
    except ValueError as e:
        sys.exit(str(e))
    validate_task(task)

    doc = build_document(task, skip_repo_metadata=args.skip_repo_metadata)
    out = task_context_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out}", file=sys.stderr)

    if not args.no_workspace_context:
        try:
            ws_out = write_workspace_context(Path.cwd())
            print(f"Refreshed {ws_out}", file=sys.stderr)
        except (OSError, ValueError) as e:
            print(f"Warning: could not refresh workspace-context.json: {e}", file=sys.stderr)

    if args.print_json:
        print(json.dumps(doc, indent=2))


if __name__ == "__main__":
    main()
