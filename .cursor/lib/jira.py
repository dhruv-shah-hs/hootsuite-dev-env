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
from typing import Any

from .dotenv import try_load_dotenv

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


def jira_mime_to_kind(mime: str) -> str:
    m = (mime or "").lower().strip()
    if m.startswith("image/"):
        return "image"
    if m.startswith("video/"):
        return "video"
    if m.startswith("audio/"):
        return "audio"
    return "file"


def jira_fields_attachments_normalize(raw: object) -> list[dict[str, Any]]:
    """Normalize Jira `fields.attachment` into a list of plain dicts (ids as strings)."""
    if not isinstance(raw, list) or not raw:
        return []
    out: list[dict[str, Any]] = []
    for a in raw:
        if not isinstance(a, dict):
            continue
        mid = a.get("id")
        if mid is None:
            continue
        mid_s = str(mid)
        mime = (a.get("mimeType") or "") or ""
        c_url = a.get("content")
        t_url = a.get("thumbnail")
        out.append(
            {
                "id": mid_s,
                "filename": (a.get("filename") or "") or "",
                "mimeType": mime,
                "size": a.get("size"),
                "content_url": c_url if isinstance(c_url, str) else None,
                "thumbnail_url": t_url if isinstance(t_url, str) else None,
                "kind": jira_mime_to_kind(mime),
            }
        )
    return out


def jira_field_description_to_plaintext(
    raw: object,
    *,
    attachment_index: dict[str, dict] | None = None,
) -> str | None:
    """Jira API v3 description/comment is often Atlassian Document Format (ADF). Optional attachment_index maps Jira media node ids to attachment dicts (filename, kind, content_url)."""
    if raw is None:
        return None
    if isinstance(raw, str):
        t = raw.strip()
        return t or None
    if isinstance(raw, dict):
        return jira_adf_to_plaintext(raw, attachment_index=attachment_index)
    return None


def jira_adf_to_plaintext(
    adf: dict,
    attachment_index: dict[str, dict] | None = None,
) -> str | None:
    """Extract plain text from an ADF document, including image/video/file placeholders for media nodes."""

    def _media_line(attrs: dict) -> str:
        mid = str((attrs or {}).get("id") or "").strip()
        alt = ((attrs or {}).get("alt") or "").strip() if isinstance(attrs, dict) else ""
        mtype = ((attrs or {}).get("type") or "").strip() if isinstance(attrs, dict) else ""
        if mid and attachment_index and mid in attachment_index:
            a = attachment_index[mid]
            fn = (a.get("filename") or "file") or "file"
            k = a.get("kind") or "file"
            c_url = a.get("content_url")
            # kind is image|video|audio|file
            line = f"[{k} attachment: {fn} (id={mid})]"
            if c_url and isinstance(c_url, str) and c_url.strip():
                line = f"{line} {c_url.strip()}"
            if a.get("thumbnail_url"):
                th = a["thumbnail_url"]
                if isinstance(th, str) and th.strip():
                    line = f"{line} [thumbnail: {th.strip()}]"
            return line
        if mid:
            extra = f", {mtype}" if mtype else ""
            aextra = f", alt={alt!r}" if alt else ""
            return f"[image/video/file attachment id={mid}{extra}{aextra}]"
        if alt:
            return f"[media: {alt}]"
        return "[media]"

    def walk(node: object) -> list[str]:
        if not isinstance(node, dict):
            return []
        ntype = node.get("type")
        if ntype == "text":
            tx = node.get("text")
            if isinstance(tx, str) and tx:
                return [tx.replace("\u00a0", " ")]
            return []
        if ntype == "hardBreak":
            return ["\n"]
        if ntype == "media" or ntype == "file":
            attrs = node.get("attrs") or {}
            return [_media_line(attrs if isinstance(attrs, dict) else {})]
        if ntype == "emoji":
            attrs = node.get("attrs") or {}
            short = (attrs.get("shortName") or attrs.get("text") or "") if isinstance(attrs, dict) else ""
            if isinstance(short, str) and short:
                return [f":{short}:" if not short.startswith(":") else short]
            return ["[emoji]"]
        if ntype == "mention":
            attrs = node.get("attrs") or {}
            t = (attrs.get("text") or attrs.get("label") or "") if isinstance(attrs, dict) else ""
            if isinstance(t, str) and t.strip():
                return [f"@{t.strip()}" if not t.strip().startswith("@") else t.strip()]
            return ["@mention"]
        if ntype == "inlineCard" or ntype == "blockCard":
            attrs = node.get("attrs") or {}
            url = (attrs.get("url") or attrs.get("data") or "") if isinstance(attrs, dict) else ""
            if isinstance(url, str) and url.strip():
                return [url.strip()]
            return [f"[{ntype or 'card'}]"]
        out2: list[str] = []
        for c in node.get("content") or ():
            out2.extend(walk(c))
        return out2

    if not isinstance(adf, dict):
        return None
    if adf.get("type") == "doc" and isinstance(adf.get("content"), list):
        blocks: list[str] = []
        for block in adf["content"]:
            t = "".join(walk(block)).strip()
            if t:
                blocks.append(t)
        res = "\n\n".join(blocks) if blocks else ""
        return res if res else None
    res2 = "".join(walk(adf)).strip()
    return res2 or None


def jira_issue_to_task(issue: dict, base: str) -> dict:
    """Map a Jira issue JSON object (search or GET issue) to our task shape."""
    key = issue.get("key") or ""
    fields = issue.get("fields") or {}
    summary = (fields.get("summary") or "").strip() or "(no summary)"
    status_name = (fields.get("status") or {}).get("name") or ""
    st = fields.get("issuetype") or {}
    type_name = st.get("name") or ""
    browse = jira_browse_url(base, key)
    att_list = jira_fields_attachments_normalize(fields.get("attachment"))
    att_index = {a["id"]: a for a in att_list} if att_list else None
    description = jira_field_description_to_plaintext(
        fields.get("description"), attachment_index=att_index
    )
    if not description:
        desc_parts = [p for p in (status_name, type_name) if p]
        description = " · ".join(desc_parts) if desc_parts else None
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
    return out


def fetch_jira_issue_by_key(issue_key: str) -> dict:
    """GET /rest/api/3/issue/{key} — use for --id so keys need not appear in JQL results."""
    base = jira_base_url()
    params = urllib.parse.urlencode({"fields": "summary,status,issuetype,description,attachment"})
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
    for field in ("summary", "status", "issuetype", "description", "attachment"):
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
