#!/usr/bin/env python3
"""
Persist the resolved task from pick-task to JSON on disk.

Uses the same task shape as pick-task stdout (single JSON object).

Default output: .cursor/context/current-task.local.json (override with --out).
That path is gitignored — copy or commit a redacted snapshot elsewhere if needed.

Examples:
  python3 .cursor/tools/pick-task --id ENG-123 | python3 .cursor/tools/save-task-context --stdin
  python3 .cursor/tools/save-task-context --from-id ENG-123

If task.is_deployed is true (Jira Done), prompts on an interactive terminal before writing;
set CURSOR_SKIP_DEPLOYED_CONFIRM=1 to proceed without prompting (CI/automation).

branch_alignment is left null here; checkout-jira-branch / align-branch may populate it later.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "lib"
if _LIB.is_dir() and str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
from deployed_confirm import confirm_continue_if_deployed_complete  # noqa: E402
from task_context import task_context_file_path  # noqa: E402


def script_dir() -> Path:
    return Path(__file__).resolve().parent


def default_out_path() -> Path:
    return task_context_file_path(Path.cwd()).resolve()


def pick_task_script() -> Path:
    d = script_dir()
    for name in ("pick-task.py", "pick-task"):
        p = d / name
        if p.is_file():
            return p
    sys.exit(f"No pick-task script found in {d}")


def run_pick_task(pick: int | None, issue_id: str | None) -> dict:
    pick_task = pick_task_script()
    cmd = [sys.executable, str(pick_task)]
    if pick is not None:
        cmd.extend(["--pick", str(pick)])
    elif issue_id:
        cmd.extend(["--id", issue_id])
    else:
        sys.exit("Internal: pick-task requires --pick or --id")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        sys.exit(err or "pick-task failed")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        sys.exit(f"Could not parse pick-task output as JSON: {e}")


def _git_prefix() -> list[str]:
    root = os.environ.get("CURSOR_SERVICE_REPO", "").strip()
    if root:
        return ["git", "-C", root]
    return ["git"]


def git_branch() -> str:
    proc = subprocess.run(_git_prefix() + ["branch", "--show-current"], capture_output=True, text=True)
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def git_dirty() -> bool:
    proc = subprocess.run(_git_prefix() + ["status", "--porcelain"], capture_output=True, text=True)
    if proc.returncode != 0:
        return False
    return bool((proc.stdout or "").strip())


def normalize_task(obj: dict) -> dict:
    out: dict = {
        "id": obj.get("id") or obj.get("jira_key") or "",
        "label": obj.get("label") or obj.get("id") or "",
        "description": obj.get("description"),
        "command": obj.get("command"),
        "browse_url": obj.get("browse_url"),
        "jira_key": obj.get("jira_key"),
    }
    repo = obj.get("repository")
    if isinstance(repo, str) and repo.strip():
        out["repository"] = repo.strip()
    elif obj.get("repository") is not None:
        out["repository"] = obj.get("repository")
    if obj.get("is_deployed") is not None:
        out["is_deployed"] = bool(obj["is_deployed"])
    return out


def load_stdin_task() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        sys.exit("stdin is empty; pipe pick-task JSON or use --from-id / --from-pick")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.exit(f"stdin is not valid JSON: {e}")
    if isinstance(data, dict) and "tasks" in data:
        sys.exit(
            "stdin contained { tasks: [...] }; pipe a single task object from pick-task stdout, "
            "or use --from-id / --from-pick"
        )
    if not isinstance(data, dict):
        sys.exit("stdin JSON must be an object")
    return normalize_task(data)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--stdin",
        action="store_true",
        help="Read one pick-task JSON object from stdin",
    )
    src.add_argument("--from-id", metavar="KEY", help="Fetch task via pick-task --id KEY")
    src.add_argument("--from-pick", type=int, metavar="N", help="Fetch task via pick-task --pick N")
    p.add_argument(
        "--out",
        type=Path,
        metavar="PATH",
        help=f"Output file (default: {default_out_path()})",
    )
    args = p.parse_args()

    if args.stdin:
        task = load_stdin_task()
    elif args.from_id:
        task = normalize_task(run_pick_task(None, args.from_id.strip()))
    else:
        task = normalize_task(run_pick_task(args.from_pick, None))

    confirm_continue_if_deployed_complete(task, program="save-task-context")

    out_path = args.out.resolve() if args.out else default_out_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    doc: dict = {
        "$schema": "./schema/tasks.schema.json",
        "resolved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_by": "save-task-context",
        "task": task,
        "branch_alignment": None,
        "repo_snapshot": {
            "git_branch": git_branch(),
            "git_dirty_hint": git_dirty(),
        },
        "repo_fit": {"status": "unknown", "notes": None},
    }

    if out_path.is_file():
        try:
            prev = json.loads(out_path.read_text(encoding="utf-8"))
            rf = prev.get("repo_fit")
            if isinstance(rf, dict) and rf.get("status") not in (None, "unknown"):
                doc["repo_fit"] = rf
            sr = prev.get("service_repo_root")
            if isinstance(sr, str) and sr.strip():
                doc["service_repo_root"] = sr.strip()
        except (OSError, json.JSONDecodeError):
            pass

    out_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(str(out_path))


if __name__ == "__main__":
    main()
