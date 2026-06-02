#!/usr/bin/env python3
"""
Persist epic context to .cursor/context/current-epic.local.json.

Requires user_input with: description, goal, requirements, linked_tickets (non-empty).

Examples:
  python3 .cursor/tools/pick-epic.py --id ENG-100 > /tmp/epic.json
  python3 .cursor/tools/save-epic-context.py --epic-file /tmp/epic.json --user-input-file user.json

  python3 .cursor/tools/save-epic-context.py --stdin   # full or partial doc on stdin
  python3 .cursor/tools/save-epic-context.py --from-epic-id ENG-100 --user-input-file user.json --cloud
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LIB = Path(__file__).resolve().parent.parent / "lib"
if _LIB.is_dir() and str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
_CURSOR_DIR = Path(__file__).resolve().parent.parent
if str(_CURSOR_DIR) not in sys.path:
    sys.path.insert(0, str(_CURSOR_DIR))
from epic_context import (  # noqa: E402
    cloud_epic_dir,
    cloud_epic_file_path,
    epic_context_file_path,
    normalize_user_input,
    try_load_epic_document,
    validate_user_input,
)


def script_dir() -> Path:
    return Path(__file__).resolve().parent


def pick_epic_script() -> Path:
    p = script_dir() / "pick-epic.py"
    if not p.is_file():
        sys.exit(f"No pick-epic.py in {script_dir()}")
    return p


def run_pick_epic(issue_id: str) -> dict[str, Any]:
    cmd = [sys.executable, str(pick_epic_script()), "--id", issue_id]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit((proc.stderr or proc.stdout or "pick-epic failed").strip())
    data = json.loads(proc.stdout)
    if not isinstance(data, dict):
        sys.exit("pick-epic output must be a JSON object")
    return data


def load_json_file(path: Path, label: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"Could not read {label} {path}: {e}")
    if not isinstance(data, dict):
        sys.exit(f"{label} must be a JSON object")
    return data


def normalize_epic_obj(obj: dict[str, Any]) -> dict[str, Any]:
    epic = obj.get("epic") if isinstance(obj.get("epic"), dict) else obj
    if not isinstance(epic, dict):
        sys.exit("Missing epic object")
    return {
        "id": epic.get("id") or epic.get("jira_key") or "",
        "label": epic.get("label") or epic.get("id") or "",
        "description": epic.get("description"),
        "browse_url": epic.get("browse_url"),
        "jira_key": epic.get("jira_key"),
        "status": epic.get("status"),
        "issue_type": epic.get("issue_type") or "Epic",
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--stdin", action="store_true", help="Read JSON document from stdin")
    p.add_argument("--from-epic-id", metavar="KEY", help="Fetch epic via pick-epic --id")
    p.add_argument("--epic-file", type=Path, metavar="PATH", help="JSON from pick-epic stdout")
    p.add_argument(
        "--user-input-file",
        type=Path,
        metavar="PATH",
        help="JSON: description, goal, requirements, linked_tickets (required)",
    )
    p.add_argument("--plan-file", type=Path, metavar="PATH", help="Markdown plan to store under plan.markdown")
    p.add_argument("--cloud", action="store_true", help="Also copy snapshot to ~/.cursor/hootsuite-epics/")
    p.add_argument("--out", type=Path, metavar="PATH", help="Output path (default current-epic.local.json)")
    args = p.parse_args()

    if not args.stdin and not args.from_epic_id and not args.epic_file:
        sys.exit("Provide --stdin, --from-epic-id, or --epic-file")

    if args.stdin:
        raw = sys.stdin.read()
        if not raw.strip():
            sys.exit("stdin is empty")
        base = json.loads(raw)
        if not isinstance(base, dict):
            sys.exit("stdin must be a JSON object")
    elif args.epic_file:
        base = load_json_file(args.epic_file.resolve(), "epic-file")
    else:
        base = run_pick_epic(args.from_epic_id.strip())

    epic = normalize_epic_obj(base)
    children = base.get("children") if isinstance(base.get("children"), list) else []

    ui_raw: dict[str, Any] = {}
    if args.user_input_file:
        ui_raw = load_json_file(args.user_input_file.resolve(), "user-input")
    elif isinstance(base.get("user_input"), dict):
        ui_raw = base["user_input"]
    else:
        sys.exit(
            "user_input is required: use --user-input-file with description, goal, requirements, linked_tickets"
        )

    user_input = normalize_user_input(ui_raw)
    missing = validate_user_input(user_input)
    if missing:
        sys.exit(f"user_input missing or empty: {', '.join(missing)}")

    plan = None
    if args.plan_file:
        md = args.plan_file.read_text(encoding="utf-8")
        plan = {
            "markdown": md,
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    elif isinstance(base.get("plan"), dict) and base["plan"].get("markdown"):
        plan = dict(base["plan"])

    prev = try_load_epic_document()
    if plan is None and prev and isinstance(prev.get("plan"), dict):
        plan = prev.get("plan")

    out_path = args.out.resolve() if args.out else epic_context_file_path(Path.cwd()).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    doc: dict[str, Any] = {
        "$schema": "./schema/epic.schema.json",
        "resolved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_by": "save-epic-context",
        "epic": epic,
        "user_input": user_input,
        "children": children,
        "plan": plan,
        "cloud_snapshot_path": None,
    }

    out_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    cloud_path = None
    if args.cloud:
        key = (epic.get("jira_key") or epic.get("id") or "epic").strip()
        dest = cloud_epic_file_path(key)
        cloud_epic_dir().mkdir(parents=True, exist_ok=True)
        shutil.copy2(out_path, dest)
        cloud_path = str(dest)
        doc["cloud_snapshot_path"] = cloud_path
        out_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    print(str(out_path))
    if cloud_path:
        print(cloud_path, file=sys.stderr)


if __name__ == "__main__":
    main()
