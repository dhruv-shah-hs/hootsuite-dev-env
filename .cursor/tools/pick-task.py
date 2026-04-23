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
  JIRA_REPOSITORY_FIELDS  Comma-separated Jira field ids (e.g. customfield_12345) whose
                          values populate task.repository for align-branch verification.
                          Omit to leave repository unset unless you edit task JSON.

If ./.env exists in the current working directory, it is loaded (keys already set
in the environment are not overwritten).

Examples:
  ./pick-task                           # interactive Jira list
  ./pick-task --pick 2                  # 2nd task, no prompt (scripts / no TTY)
  ./pick-task --json
  ./pick-task --jql "project = ENG ORDER BY created DESC"
  ./pick-task --id PROJ-123             # fetch that issue directly (GET /issue/{key})
  python3 .cursor/tools/pick-task.py --id PROJ-123 | python3 .cursor/tools/save-task-context.py --stdin   # persist

When task.is_deployed is true (Jira Done), pick-task prompts on TTY before printing JSON (unless
CURSOR_SKIP_DEPLOYED_CONFIRM=1).
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
from typing import Any

_LIB = Path(__file__).resolve().parent.parent / "lib"
if _LIB.is_dir() and str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
from deployed_confirm import confirm_continue_if_deployed_complete  # noqa: E402


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


def jira_repository_field_ids() -> list[str]:
    raw = (os.environ.get("JIRA_REPOSITORY_FIELDS") or "").strip()
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _coerce_jira_value_to_repository_string(raw: Any) -> str:
    """Best-effort string for Git remote URL/slug from a Jira field value."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        for key in ("value", "name", "displayName", "url"):
            v = raw.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        inner = raw.get("option")
        if isinstance(inner, dict):
            v = inner.get("value") or inner.get("name")
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def is_deployed_from_jira_status(status_name: str) -> bool | None:
    """Deployment hint from Jira status name (case-insensitive). Done → shipped; canceled → not."""
    s = (status_name or "").strip().lower()
    if s == "done":
        return True
    if s in ("canceled", "cancelled"):
        return False
    return None


def extract_repository_from_issue_fields(fields: dict[str, Any]) -> str | None:
    """First non-empty repository string from configured Jira fields."""
    for fid in jira_repository_field_ids():
        raw = fields.get(fid)
        s = _coerce_jira_value_to_repository_string(raw)
        if s:
            return s
    return None


def jira_issue_api_field_names() -> list[str]:
    base = ["summary", "status", "issuetype"]
    extra = jira_repository_field_ids()
    seen = set(base)
    out = list(base)
    for f in extra:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


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
    repository = extract_repository_from_issue_fields(fields)
    out: dict[str, Any] = {
        "id": key,
        "label": f"{key}: {summary}",
        "description": description,
        "command": open_ticket_command(browse),
        "browse_url": browse,
        "jira_key": key,
    }
    if repository:
        out["repository"] = repository
    deployed = is_deployed_from_jira_status(status_name)
    if deployed is not None:
        out["is_deployed"] = deployed
    return out


def fetch_jira_issue_by_key(issue_key: str) -> dict:
    """GET /rest/api/3/issue/{key} — use for --id so keys need not appear in JQL results."""
    base = jira_base_url()
    names = jira_issue_api_field_names()
    params = urllib.parse.urlencode({"fields": ",".join(names)})
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
    for field in jira_issue_api_field_names():
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
    print("Pick # or q:", file=sys.stderr)
    for i, t in enumerate(tasks, start=1):
        label = t.get("label", t.get("id"))
        print(f"  {i}) {label}", file=sys.stderr)
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
    out = {
        "id": t["id"],
        "label": t.get("label", t["id"]),
        "description": t.get("description"),
        "command": t.get("command"),
        "browse_url": t.get("browse_url"),
        "jira_key": t.get("jira_key"),
    }
    if t.get("repository") is not None:
        out["repository"] = t.get("repository")
    if t.get("is_deployed") is not None:
        out["is_deployed"] = t["is_deployed"]
    return out


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

    cmd = chosen.get("command")
    if args.print_command:
        if cmd:
            print(cmd)
        else:
            sys.exit("This task has no command.")
        return

    confirm_continue_if_deployed_complete(chosen, program="pick-task")
    print(json.dumps(chosen, indent=2))


if __name__ == "__main__":
    main()
