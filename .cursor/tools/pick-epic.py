#!/usr/bin/env python3
"""
List Jira Epics, pick one, and load child issues for epic context.

Uses the same Jira env as pick-task. Persist with save-epic-context.py.

Default epic JQL (override with JIRA_EPIC_JQL or --jql):
  issuetype = Epic AND assignee = currentUser() AND resolution = Unresolved ORDER BY updated DESC

Child issues (override JIRA_EPIC_CHILDREN_JQL with {epic_key} placeholder):
  parent = {epic_key} OR "Epic Link" = {epic_key} ORDER BY rank

Examples:
  python3 .cursor/tools/pick-epic.py --json
  python3 .cursor/tools/pick-epic.py --id ENG-100
  python3 .cursor/tools/pick-epic.py --pick 2
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

_LIB = Path(__file__).resolve().parent.parent / "lib"
if _LIB.is_dir() and str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
from dotenv import try_load_dotenv  # noqa: E402


def _load_pick_task():
    script = Path(__file__).resolve().parent / "pick-task.py"
    spec = importlib.util.spec_from_file_location("pick_task", script)
    if spec is None or spec.loader is None:
        sys.exit(f"Cannot load pick-task from {script}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def default_epic_jql() -> str:
    return (
        os.environ.get("JIRA_EPIC_JQL", "").strip()
        or 'issuetype = Epic AND assignee = currentUser() AND resolution = Unresolved ORDER BY updated DESC'
    )


def children_jql(epic_key: str) -> str:
    tpl = (
        os.environ.get("JIRA_EPIC_CHILDREN_JQL", "").strip()
        or 'parent = {epic_key} OR "Epic Link" = {epic_key} ORDER BY rank'
    )
    return tpl.format(epic_key=epic_key)


def normalize_epic(task: dict[str, Any]) -> dict[str, Any]:
    out = dict(task)
    out["issue_type"] = "Epic"
    return out


def normalize_child(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": task.get("id") or task.get("jira_key") or "",
        "label": task.get("label") or task.get("id") or "",
        "jira_key": task.get("jira_key"),
        "status": task.get("status"),
        "description": task.get("description"),
        "browse_url": task.get("browse_url"),
    }


def build_epic_payload(pt, epic_task: dict[str, Any], max_children: int) -> dict[str, Any]:
    key = (epic_task.get("jira_key") or epic_task.get("id") or "").strip()
    children: list[dict[str, Any]] = []
    if key:
        try:
            raw_children = pt.fetch_jira_issues(children_jql(key), max_children)
            children = [normalize_child(c) for c in raw_children]
        except SystemExit:
            children = []
    suggested = [c["id"] for c in children if c.get("id")]
    return {
        "epic": normalize_epic(epic_task),
        "children": children,
        "suggested_linked_tickets": suggested,
    }


def pick_interactive(epics: list[dict]) -> dict:
    print("Pick epic # or q:", file=sys.stderr)
    for i, t in enumerate(epics, start=1):
        print(f"  {i}) {t.get('label', t.get('id'))}", file=sys.stderr)
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            sys.exit(1)
        if line.lower() in ("q", "quit", "exit"):
            sys.exit(1)
        if not line.isdigit():
            print("Enter a number from the list.", file=sys.stderr)
            continue
        n = int(line)
        if 1 <= n <= len(epics):
            return epics[n - 1]
        print(f"Choose between 1 and {len(epics)}.", file=sys.stderr)


def main() -> None:
    try_load_dotenv()
    pt = _load_pick_task()

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--jql", metavar="JQL", help="Epic JQL (overrides JIRA_EPIC_JQL)")
    p.add_argument("--max-results", type=int, metavar="N", help="Max epics (default JIRA_MAX_RESULTS or 50)")
    p.add_argument("--max-children", type=int, metavar="N", default=100, help="Max child issues (default 100)")
    p.add_argument("--json", action="store_true", help="Print { epics: [...] } for agents")
    p.add_argument("--id", metavar="KEY", help="Fetch epic by Jira key")
    p.add_argument("--pick", type=int, metavar="N", help="Select Nth epic (1-based)")
    args = p.parse_args()

    max_r = args.max_results
    if max_r is None:
        max_r = int(os.environ.get("JIRA_MAX_RESULTS", "50"))

    if args.id:
        epic_task = pt.fetch_jira_issue_by_key(args.id.strip())
        payload = build_epic_payload(pt, epic_task, args.max_children)
        print(json.dumps(payload, indent=2))
        return

    jql = (args.jql or "").strip() or default_epic_jql()
    epics = pt.fetch_jira_issues(jql, max_r)
    if not epics:
        sys.exit("Jira returned no epics for this JQL. Adjust JIRA_EPIC_JQL or use --jql.")

    if args.json:
        print(json.dumps({"epics": [normalize_epic(e) for e in epics]}, indent=2))
        return

    if args.pick is not None:
        if args.pick < 1 or args.pick > len(epics):
            sys.exit(f"--pick must be between 1 and {len(epics)}")
        chosen = epics[args.pick - 1]
    else:
        chosen = pick_interactive(epics)

    payload = build_epic_payload(pt, chosen, args.max_children)
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
