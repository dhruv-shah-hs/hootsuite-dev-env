#!/usr/bin/env python3
"""
List Jira issues as tasks, then pick one interactively or emit JSON for
Cursor AskQuestion / automation.

Uses Jira Cloud/Server REST API. Same variables as Jira MCP:
  JIRA_INSTANCE_URL   e.g. https://yourorg.atlassian.net
  JIRA_USER_EMAIL
  JIRA_API_KEY        API token (Atlassian account settings)
Optional:
  JIRA_JQL            JQL query (default: unresolved assigned to you, by updated)
  JIRA_MAX_RESULTS    Max issues (default: 50)

If ./.env exists in the current working directory, it is loaded (keys already set
in the environment are not overwritten).

Examples:
  ./pick-task                           # interactive Jira list
  ./pick-task --pick 2                  # 2nd task, no prompt (scripts / no TTY)
  ./pick-task --json
  ./pick-task --jql "project = ENG ORDER BY created DESC"
  ./pick-task --id PROJ-123             # fetch that issue directly (GET /issue/{key})
  python3 .cursor/tools/pick-task.py --id PROJ-123 | python3 .cursor/tools/save-task-context.py --stdin   # persist

Also writes `.cursor/task-context/workspace-context.json` when a single task is resolved (not with --json
list mode). Service repo is expected under `./.reference`. Use --no-workspace-context to skip.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_CURSOR_DIR = Path(__file__).resolve().parent.parent
if str(_CURSOR_DIR) not in sys.path:
    sys.path.insert(0, str(_CURSOR_DIR))

from lib.workspace_context import write_workspace_context  # noqa: E402


def try_load_dotenv() -> None:
    """Load ./.env if present; does not override existing environment variables."""
    path = Path.cwd() / ".env"
    if not path.is_file():
        return
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        os.environ[key] = val


def jira_base_url() -> str:
    u = (os.environ.get("JIRA_INSTANCE_URL") or "").strip().rstrip("/")
    if not u:
        sys.exit(
            "Jira: set JIRA_INSTANCE_URL (e.g. https://yourorg.atlassian.net), "
            "JIRA_USER_EMAIL, and JIRA_API_KEY"
        )
    return u


def jira_auth_header() -> str:
    email = (os.environ.get("JIRA_USER_EMAIL") or "").strip()
    token = (os.environ.get("JIRA_API_KEY") or "").strip()
    if not email or not token:
        sys.exit("Jira: set JIRA_USER_EMAIL and JIRA_API_KEY (API token)")
    raw = f"{email}:{token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def jira_browse_url(base: str, issue_key: str) -> str:
    return f"{base}/browse/{urllib.parse.quote(issue_key)}"


def open_ticket_command(url: str) -> str:
    """Shell command to open the ticket URL (best-effort per OS)."""
    if sys.platform == "darwin":
        return f"open {shlex.quote(url)}"
    if sys.platform == "win32":
        return f'start "" {shlex.quote(url)}'
    return f"xdg-open {shlex.quote(url)}"


def jira_issue_to_task(issue: dict, base: str) -> dict:
    """Map a Jira issue JSON object (search or GET issue) to our task shape."""
    key = issue.get("key") or ""
    fields = issue.get("fields") or {}
    summary = (fields.get("summary") or "").strip() or "(no summary)"
    status_name = (fields.get("status") or {}).get("name") or ""
    st = fields.get("issuetype") or {}
    type_name = st.get("name") or ""
    browse = jira_browse_url(base, key)
    desc_parts = [p for p in (status_name, type_name) if p]
    description = " · ".join(desc_parts) if desc_parts else None
    return {
        "id": key,
        "label": f"{key}: {summary}",
        "description": description,
        "command": open_ticket_command(browse),
        "browse_url": browse,
        "jira_key": key,
    }


def fetch_jira_issue_by_key(issue_key: str) -> dict:
    """GET /rest/api/3/issue/{key} — use for --id so keys need not appear in JQL results."""
    base = jira_base_url()
    params = urllib.parse.urlencode({"fields": "summary,status,issuetype"})
    safe_key = urllib.parse.quote(issue_key, safe="")
    url = f"{base}/rest/api/3/issue/{safe_key}?{params}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", jira_auth_header())
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        sys.exit(f"Jira API HTTP {e.code}: {detail[:500]}")
    except urllib.error.URLError as e:
        sys.exit(f"Jira request failed: {e}")
    return jira_issue_to_task(body, base)


def fetch_jira_issues(jql: str, max_results: int) -> list[dict]:
    base = jira_base_url()
    # Enhanced search (GET /rest/api/3/search/jql) — legacy /rest/api/3/search was removed (CHANGE-2046).
    # fields is an array in the API; repeat the query parameter for each field.
    query_parts = [
        ("jql", jql),
        ("maxResults", str(max_results)),
    ]
    for field in ("summary", "status", "issuetype"):
        query_parts.append(("fields", field))
    params = urllib.parse.urlencode(query_parts)
    url = f"{base}/rest/api/3/search/jql?{params}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", jira_auth_header())
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        sys.exit(f"Jira API HTTP {e.code}: {detail[:500]}")
    except urllib.error.URLError as e:
        sys.exit(f"Jira request failed: {e}")

    issues = body.get("issues") or []
    return [jira_issue_to_task(issue, base) for issue in issues]


def default_jira_jql() -> str:
    return (
        os.environ.get("JIRA_JQL", "").strip()
        or "assignee = currentUser() AND resolution = Unresolved ORDER BY updated DESC"
    )


def pick_interactive(tasks: list[dict]) -> dict:
    print("Select a task (number), or q to quit:\n", file=sys.stderr)
    for i, t in enumerate(tasks, start=1):
        label = t.get("label", t.get("id"))
        desc = t.get("description") or ""
        extra = f" — {desc}" if desc else ""
        print(f"  {i}) {label}{extra}", file=sys.stderr)
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("", file=sys.stderr)
            sys.exit(1)
        if line.lower() in ("q", "quit", "exit"):
            sys.exit(1)
        if not line.isdigit():
            print("Enter a number from the list.", file=sys.stderr)
            continue
        n = int(line)
        if 1 <= n <= len(tasks):
            return tasks[n - 1]
        print(f"Choose between 1 and {len(tasks)}.", file=sys.stderr)


def task_to_json_shape(t: dict) -> dict:
    return {
        "id": t["id"],
        "label": t.get("label", t["id"]),
        "description": t.get("description"),
        "command": t.get("command"),
        "browse_url": t.get("browse_url"),
        "jira_key": t.get("jira_key"),
    }


def main() -> None:
    try_load_dotenv()

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--jql", metavar="JQL", help="Jira JQL (overrides JIRA_JQL env for this run)")
    p.add_argument(
        "--max-results",
        type=int,
        metavar="N",
        help="Max Jira issues (default: JIRA_MAX_RESULTS or 50)",
    )
    p.add_argument("--json", action="store_true", help="Print tasks as JSON (for agents)")
    p.add_argument("--id", metavar="ID", help="Pick task by id without prompting (Jira issue key)")
    p.add_argument(
        "--pick",
        type=int,
        metavar="N",
        help="Select Nth task (1-based) without prompting; overrides --id when both set",
    )
    p.add_argument(
        "--print-command",
        action="store_true",
        help="After selection, print only the command string (if any)",
    )
    p.add_argument(
        "--no-workspace-context",
        action="store_true",
        help="Do not write .cursor/task-context/workspace-context.json",
    )
    args = p.parse_args()

    if args.id:
        tasks = [fetch_jira_issue_by_key(args.id)]
    else:
        jql = (args.jql or "").strip() or default_jira_jql()
        max_r = args.max_results
        if max_r is None:
            max_r = int(os.environ.get("JIRA_MAX_RESULTS", "50"))
        tasks = fetch_jira_issues(jql, max_r)
        if not tasks:
            sys.exit("Jira returned no issues for this JQL. Adjust JIRA_JQL or use --jql.")

    if args.json:
        out = [task_to_json_shape(t) for t in tasks]
        print(json.dumps({"tasks": out}, indent=2))
        return

    def refresh_workspace_context() -> None:
        if args.no_workspace_context:
            return
        try:
            cwd = Path.cwd()
            out = write_workspace_context(cwd)
            rel = out.relative_to(cwd.resolve())
            print(f"Wrote workspace context: {rel}", file=sys.stderr)
        except (OSError, ValueError) as e:
            print(f"Warning: could not write workspace-context.json: {e}", file=sys.stderr)

    if args.pick is not None:
        if args.pick < 1 or args.pick > len(tasks):
            sys.exit(f"--pick must be between 1 and {len(tasks)} (inclusive)")
        chosen = tasks[args.pick - 1]
    elif args.id:
        chosen = next((t for t in tasks if t.get("id") == args.id), None)
        if not chosen:
            sys.exit(f"Unknown task id: {args.id}")
    else:
        chosen = pick_interactive(tasks)

    refresh_workspace_context()

    cmd = chosen.get("command")
    if args.print_command:
        if cmd:
            print(cmd)
        else:
            sys.exit("This task has no command.")
        return

    print(json.dumps(chosen, indent=2))


if __name__ == "__main__":
    main()
