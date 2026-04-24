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
  PICK_TASK_NO_RUN_SERVICE_PROMPT  Set to 1 to skip the optional start-service TTY prompts
                          after a successful interactive pick (stdout must be a TTY and not piped).

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

For the selected task, Jira issue comments are fetched and included as task.comments (plaintext body)
when any exist (separate /comment API, paginated). Omitted for --json when JQL returns multiple issues;
fetched for --id, a single JQL result, and interactive / --pick selection.

Jira `attachment` field is requested for each issue: `task.attachments` lists files with `kind` (image/video/audio/file) and Jira `content` URLs. Inlined media in the description and comments is turned into bracketed lines that reference the same files when the attachment id matches the ADF `media` node.

To refresh `.cursor/context/service-context.json`, use `python3 .cursor/tools/resolve-service.py`
(typically after `align-branch`).
For a dedicated chat workflow to start the service after context is ready, see `.cursor/agents/start-service.mdc`.
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
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

_CURSOR_DIR = Path(__file__).resolve().parent.parent
if str(_CURSOR_DIR) not in sys.path:
    sys.path.insert(0, str(_CURSOR_DIR))

from lib.jira import (  # noqa: E402
    jira_field_description_to_plaintext,
    jira_fields_attachments_normalize,
)
from lib.dotenv import try_load_dotenv  # noqa: E402

_START_SERVICE_PROMPT_FN: object = False  # False = not loaded yet; None = load failed


def _call_maybe_prompt_run_service_interactive() -> None:
    """Delegate to `.cursor/tools/start-service.py` (hyphenated path; loaded via importlib)."""
    global _START_SERVICE_PROMPT_FN
    if _START_SERVICE_PROMPT_FN is False:
        path = Path(__file__).resolve().parent / "start-service.py"
        spec = importlib.util.spec_from_file_location("_cursor_tools_start_service", path)
        if spec is None or spec.loader is None:
            _START_SERVICE_PROMPT_FN = None
        else:
            mod = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(mod)
                _START_SERVICE_PROMPT_FN = getattr(mod, "maybe_prompt_run_service_interactive", None)
            except Exception:
                _START_SERVICE_PROMPT_FN = None
    fn = _START_SERVICE_PROMPT_FN
    if callable(fn):
        fn()


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


def _attachment_index_from_task(task: dict[str, Any]) -> dict[str, dict] | None:
    """id -> attachment dict for ADF media / comment body resolution."""
    raw = task.get("attachments")
    if not isinstance(raw, list) or not raw:
        return None
    m: dict[str, dict] = {}
    for a in raw:
        if isinstance(a, dict) and a.get("id") is not None:
            m[str(a["id"])] = a
    return m or None


def jira_comment_to_item(
    comment: dict[str, Any],
    attachment_index: dict[str, dict] | None = None,
) -> dict[str, Any]:
    """Map one Jira comment JSON object to a small plaintext shape."""
    body = jira_field_description_to_plaintext(
        comment.get("body"), attachment_index=attachment_index
    )
    author = comment.get("author") or comment.get("updateAuthor") or {}
    display: str | None
    if isinstance(author, dict):
        display = (author.get("displayName") or author.get("name") or "") or None
    else:
        display = None
    raw_id = comment.get("id")
    return {
        "id": str(raw_id) if raw_id is not None else "",
        "author": display,
        "created": comment.get("created"),
        "updated": comment.get("updated"),
        "body": body,
    }


def jira_max_comments_per_issue() -> int:
    """0 = no cap (page until done). Otherwise stop after this many comments."""
    try:
        return int((os.environ.get("JIRA_MAX_COMMENTS_PER_ISSUE") or "0").strip() or 0)
    except ValueError:
        return 0


def fetch_jira_issue_all_comments(
    issue_key: str,
    attachment_index: dict[str, dict] | None = None,
) -> list[dict[str, Any]]:
    """GET /rest/api/3/issue/{key}/comment with pagination."""
    base = jira_base_url()
    cap = jira_max_comments_per_issue()
    out: list[dict[str, Any]] = []
    start = 0
    page_size = 100
    safe_key = urllib.parse.quote(issue_key, safe="")
    while True:
        q = urllib.parse.urlencode({"startAt": str(start), "maxResults": str(page_size)})
        url = f"{base}/rest/api/3/issue/{safe_key}/comment?{q}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", jira_auth_header())
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            sys.exit(f"Jira API (comments) HTTP {e.code}: {detail[:500]}")
        except urllib.error.URLError as e:
            sys.exit(f"Jira request (comments) failed: {e}")
        batch = data.get("comments") or []
        for c in batch:
            if not isinstance(c, dict):
                continue
            out.append(jira_comment_to_item(c, attachment_index=attachment_index))
            if cap > 0 and len(out) >= cap:
                return out[:cap]
        if not batch:
            break
        start += len(batch)
        total = data.get("total")
        if total is not None and start >= int(total):
            break
    return out


def extract_repository_from_issue_fields(fields: dict[str, Any]) -> str | None:
    """First non-empty repository string from configured Jira fields."""
    for fid in jira_repository_field_ids():
        raw = fields.get(fid)
        s = _coerce_jira_value_to_repository_string(raw)
        if s:
            return s
    return None


def jira_issue_api_field_names() -> list[str]:
    base = ["summary", "status", "issuetype", "description", "attachment"]
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
    iss = fields.get("issuetype") or {}
    type_name = iss.get("name") or ""
    browse = jira_browse_url(base, key)
    att_list = jira_fields_attachments_normalize(fields.get("attachment"))
    att_index = {a["id"]: a for a in att_list} if att_list else None
    description = jira_field_description_to_plaintext(
        fields.get("description"), attachment_index=att_index
    )
    if not description:
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
    if att_list:
        out["attachments"] = att_list
    if repository:
        out["repository"] = repository
    status_s = (status_name or "").strip()
    out["status"] = status_s if status_s else None
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
    task = jira_issue_to_task(body, base)
    k = (task.get("jira_key") or task.get("id") or issue_key or "").strip()
    if k:
        att_idx = _attachment_index_from_task(task)
        comments = fetch_jira_issue_all_comments(k, attachment_index=att_idx)
        if comments:
            task["comments"] = comments
    return task


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
    if "status" in t:
        out["status"] = t.get("status")
    if "comments" in t:
        out["comments"] = t.get("comments")
    if "attachments" in t:
        out["attachments"] = t.get("attachments")
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
        if len(tasks) == 1 and not args.id:
            t0 = tasks[0]
            k0 = (t0.get("jira_key") or t0.get("id") or "").strip()
            if k0 and "comments" not in t0:
                idx0 = _attachment_index_from_task(t0)
                c0 = fetch_jira_issue_all_comments(k0, attachment_index=idx0)
                if c0:
                    tasks = [{**t0, "comments": c0}]
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

    if not args.id:
        k = (chosen.get("jira_key") or chosen.get("id") or "").strip()
        if k and "comments" not in chosen:
            aidx = _attachment_index_from_task(chosen)
            comments = fetch_jira_issue_all_comments(k, attachment_index=aidx)
            if comments:
                chosen = {**chosen, "comments": comments}

    cmd = chosen.get("command")
    if args.print_command:
        if cmd:
            print(cmd)
        else:
            sys.exit("This task has no command.")
        return

    confirm_continue_if_deployed_complete(chosen, program="pick-task")
    print(json.dumps(chosen, indent=2))
    _call_maybe_prompt_run_service_interactive()


if __name__ == "__main__":
    main()
