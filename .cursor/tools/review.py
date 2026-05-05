#!/usr/bin/env python3
"""Fetch GitHub PR context (comments + reviews) for the Review agent.

Requires GitHub CLI (gh) authenticated for the repo.

Examples:
  python3 .cursor/tools/review.py --pr 123
  python3 .cursor/tools/review.py --pr https://github.com/org/repo/pull/123
  python3 .cursor/tools/review.py --repo-root ../service-entitlement --pr 123
  python3 .cursor/tools/review.py --json  # best-effort: infer PR for current branch
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

_CURSOR_DIR = Path(__file__).resolve().parent.parent
if str(_CURSOR_DIR) not in sys.path:
    sys.path.insert(0, str(_CURSOR_DIR))

from lib.git import run_gh  # noqa: E402


def _parse_dt(s: str | None) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    # GitHub timestamps are RFC3339, commonly like 2026-01-02T03:04:05Z
    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s[:-1] + "+00:00")
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _last_state_by_author(reviews: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Keep latest submitted review per author login."""
    latest: dict[str, dict[str, Any]] = {}
    for r in reviews:
        if not isinstance(r, dict):
            continue
        a = r.get("author")
        login = a.get("login") if isinstance(a, dict) else None
        if not login:
            continue
        ts = _parse_dt(r.get("submittedAt"))
        if ts is None:
            continue
        cur = latest.get(login)
        if cur is None:
            latest[login] = r
            continue
        cur_ts = _parse_dt(cur.get("submittedAt"))
        if cur_ts is None or ts > cur_ts:
            latest[login] = r
    return latest


@dataclass(frozen=True)
class ReviewStats:
    approvals: int
    changes_requested: int
    commented: int
    dismissed: int


def _stats_from_latest(latest: dict[str, dict[str, Any]]) -> ReviewStats:
    approvals = 0
    changes_requested = 0
    commented = 0
    dismissed = 0
    for r in latest.values():
        state = (r.get("state") or "").upper()
        if state == "APPROVED":
            approvals += 1
        elif state == "CHANGES_REQUESTED":
            changes_requested += 1
        elif state == "COMMENTED":
            commented += 1
        elif state == "DISMISSED":
            dismissed += 1
    return ReviewStats(
        approvals=approvals,
        changes_requested=changes_requested,
        commented=commented,
        dismissed=dismissed,
    )


def _gh_pr_view(repo_root: Path | None, pr_ref: str | None) -> dict[str, Any]:
    fields = [
        "number",
        "title",
        "url",
        "state",
        "isDraft",
        "createdAt",
        "updatedAt",
        "author",
        "baseRefName",
        "headRefName",
        "reviewDecision",
        "mergeable",
        "comments",
        "reviews",
        "files",
        "additions",
        "deletions",
        "commits",
        "labels",
        "milestone",
        "assignees",
    ]

    cmd = ["pr", "view"]
    if pr_ref:
        cmd.append(pr_ref)
    cmd.extend(["--json", ",".join(fields)])

    p = run_gh(*cmd, check=False, repo_root=repo_root)
    if p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip()
        raise RuntimeError(err or "gh pr view failed")
    raw = (p.stdout or "").strip()
    if not raw:
        raise RuntimeError("gh pr view returned empty output")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"gh pr view did not return valid JSON: {e}")
    if not isinstance(data, dict):
        raise RuntimeError("gh pr view JSON was not an object")
    return data


def normalize_pr_context(pr: dict[str, Any]) -> dict[str, Any]:
    reviews = pr.get("reviews")
    if not isinstance(reviews, list):
        reviews = []

    latest = _last_state_by_author([r for r in reviews if isinstance(r, dict)])
    stats = _stats_from_latest(latest)

    issue_comments = pr.get("comments") if isinstance(pr.get("comments"), list) else []

    out: dict[str, Any] = {
        "pr": {
            "number": pr.get("number"),
            "title": pr.get("title"),
            "url": pr.get("url"),
            "state": pr.get("state"),
            "isDraft": pr.get("isDraft"),
            "baseRefName": pr.get("baseRefName"),
            "headRefName": pr.get("headRefName"),
            "reviewDecision": pr.get("reviewDecision"),
            "mergeable": pr.get("mergeable"),
            "additions": pr.get("additions"),
            "deletions": pr.get("deletions"),
            "author": pr.get("author"),
            "createdAt": pr.get("createdAt"),
            "updatedAt": pr.get("updatedAt"),
            "labels": pr.get("labels"),
            "assignees": pr.get("assignees"),
            "milestone": pr.get("milestone"),
        },
        "counts": {
            "reviews_total": len(reviews),
            "issue_comments_total": len(issue_comments),
            "latest_review_states": {
                "approvals": stats.approvals,
                "changes_requested": stats.changes_requested,
                "commented": stats.commented,
                "dismissed": stats.dismissed,
            },
        },
        "reviews": reviews,
        "issue_comments": issue_comments,
        "files": pr.get("files") if isinstance(pr.get("files"), list) else [],
        "commits": pr.get("commits") if isinstance(pr.get("commits"), list) else [],
    }

    latest_compact: dict[str, Any] = {}
    for login, r in sorted(latest.items(), key=lambda kv: kv[0].lower()):
        latest_compact[login] = {
            "state": r.get("state"),
            "submittedAt": r.get("submittedAt"),
            "url": r.get("url"),
            "body": r.get("body"),
        }
    out["latest_review_by_author"] = latest_compact
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pr", metavar="PR", help="PR number or URL. If omitted, infer from current branch.")
    ap.add_argument(
        "--repo-root",
        type=Path,
        metavar="PATH",
        help="Repo root to run gh within (defaults to CURSOR_SERVICE_REPO when set, else cwd).",
    )
    ap.add_argument("--json", action="store_true", help="Print normalized JSON (default).")
    args = ap.parse_args()

    repo_root: Path | None
    if args.repo_root is not None:
        repo_root = args.repo_root.expanduser().resolve()
    else:
        env = (os.environ.get("CURSOR_SERVICE_REPO") or "").strip()
        repo_root = Path(env).expanduser().resolve() if env else None

    try:
        raw = _gh_pr_view(repo_root=repo_root, pr_ref=(args.pr or None))
    except RuntimeError as e:
        sys.exit(str(e))

    doc = normalize_pr_context(raw)
    print(json.dumps(doc, indent=2))


if __name__ == "__main__":
    main()
