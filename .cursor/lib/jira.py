from __future__ import annotations

import base64
import json
import os
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


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
