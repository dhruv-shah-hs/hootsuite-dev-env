#!/usr/bin/env python3
"""
Read the selected task from `.cursor/context/current-task.local.json`
(save-task-context / pick-task workflow), then check out a git branch named
  JIRA-TICKET-ID_<suffix>
If no branch exists with that prefix, propose <suffix> from the task summary,
ask [Y/n] to accept, else prompt for a custom suffix, then create the branch.

The service codebase usually lives next to `.cursor/` in the workspace; pass the
service clone via --git-cwd, CURSOR_SERVICE_REPO, or service_repo_root in the
task context JSON.

Environment:
  CURSOR_REQUIRE_TASK_REPOSITORY   When unset or 1 (default), task.repository is required for
                                   alignment; missing or wrong origin yields "Mismatch in repo".
                                   Set to 0 to restore optional repo checks (legacy).
  CURSOR_SERVICE_REPO              Absolute path to the service clone. If unset or blank, alignment
                                   also accepts service_repo_root in current-task.local.json or
                                   --git-cwd. Falling back to the shell cwd without one of these is
                                   disabled (exit 3) so the dev-env repo is not mistaken for the service.
                                   Set CURSOR_ALLOW_ALIGN_BRANCH_CWD=1 only to allow cwd fallback.

Examples:
  python3 .cursor/tools/checkout-jira-branch.py
  python3 .cursor/tools/checkout-jira-branch.py --dry-run-json
  python3 .cursor/tools/checkout-jira-branch.py --git-cwd ../my-service

--dry-run-json prints JSON describing Jira key, branch prefix, repository check,
matching branches, current HEAD branch, and what checkout would do — without
prompting or mutating git.

After a successful checkout, branch alignment is merged into current-task.local.json
unless --no-write-task-context is passed.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_CURSOR_DIR = Path(__file__).resolve().parent.parent
_WORKSPACE_ROOT = _CURSOR_DIR.parent
if str(_CURSOR_DIR) not in sys.path:
    sys.path.insert(0, str(_CURSOR_DIR))

from lib.git import (  # noqa: E402
    branches_with_prefix,
    checkout_branch,
    current_branch_name,
    normalize_git_remote,
    remote_origin_url,
    run_git,
    working_tree_dirty,
)
from lib.task_context import (  # noqa: E402
    extract_task_from_document,
    load_task_context_document,
    task_context_path,
)


EXIT_MISMATCH_REPO = 2
EXIT_NO_SERVICE_REPO = 3

# Max length for the suffix segment after ISSUE-KEY_ (keep full branch names manageable).
_MAX_PROPOSED_SUFFIX_LEN = 32

# Short headline-style slugs (not full-summary snake_case).
_MAX_SLUG_PARTS = 6
_MAX_SLUG_WORDS = 4
_MAX_SLUG_ACRONYMS = 2
_MAX_SLUG_WORD_CHARS = 12

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "for",
        "to",
        "of",
        "in",
        "on",
        "at",
        "by",
        "with",
        "from",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "must",
        "can",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "we",
        "you",
        "they",
        "them",
        "he",
        "she",
        "who",
        "whom",
        "which",
        "what",
        "where",
        "when",
        "why",
        "how",
        "all",
        "each",
        "every",
        "both",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "no",
        "not",
        "only",
        "own",
        "same",
        "so",
        "than",
        "too",
        "very",
        "just",
        "into",
        "over",
        "after",
        "before",
        "above",
        "below",
        "between",
        "under",
        "again",
        "further",
        "then",
        "once",
        "here",
        "there",
        "also",
        "about",
        "behind",
        "during",
        "through",
        "any",
        "per",
        "via",
        "if",
        "but",
        "because",
        "until",
        "while",
        "although",
        "though",
        "please",
        "need",
        "needs",
        "needed",
        "should",
        "could",
        "would",
        "using",
        "use",
        "used",
        "new",
        "old",
        "add",
        "remove",
        "update",
        "fix",
        "fixed",
        "ensure",
        "make",
        "making",
        "made",
        "get",
        "got",
        "set",
        "see",
        "seen",
        "like",
        "well",
        "etc",
    }
)

# Acronyms first so "DL" is not parsed as a normal word.
_ORDERED_TOKEN_RE = re.compile(r"(?:\b[A-Z]{2,}\b)|(?:\b[A-Za-z][a-zA-Z0-9]*\b)")


def _is_acronym_token(raw: str) -> bool:
    return bool(raw) and raw.isalpha() and raw.isupper() and len(raw) >= 2


def _format_acronym_for_slug(raw: str) -> str:
    """Most 2–4 letter caps stay upper (DL, API); common expansions read better lower (ai)."""
    low = raw.lower()
    if low == "ai":
        return "ai"
    return raw


def _truncate_proposed_suffix(s: str, max_len: int = _MAX_PROPOSED_SUFFIX_LEN) -> str:
    """Shorten slug; prefer breaking at the last underscore so words are not cut mid-token."""
    if len(s) <= max_len:
        return s
    chunk = s[:max_len].rstrip("._-")
    if "_" in chunk:
        head, _, _tail = chunk.rpartition("_")
        if len(head) >= 6:
            return head.rstrip("._-")
    return chunk or s[:max_len].rstrip("._-")


def _validate_existing_dir(label: str, path: Path) -> Path:
    if not path.exists():
        sys.exit(f"{label}: path does not exist: {path}")
    if not path.is_dir():
        sys.exit(f"{label}: not a directory: {path}")
    return path


def resolve_git_root(cli_git_cwd: Path | None, doc: dict[str, Any]) -> Path:
    """Resolve the service clone root; exit if unset when cwd fallback is not allowed."""
    if cli_git_cwd is not None:
        return _validate_existing_dir("--git-cwd", cli_git_cwd.expanduser().resolve())

    env = os.environ.get("CURSOR_SERVICE_REPO", "").strip()
    if env:
        return _validate_existing_dir(
            "CURSOR_SERVICE_REPO",
            Path(env).expanduser().resolve(),
        )

    raw = doc.get("service_repo_root")
    if isinstance(raw, str) and raw.strip():
        p = Path(raw.strip())
        resolved = p.resolve() if p.is_absolute() else (_WORKSPACE_ROOT / p).resolve()
        return _validate_existing_dir("service_repo_root in task context", resolved)

    allow_cwd = os.environ.get("CURSOR_ALLOW_ALIGN_BRANCH_CWD", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if allow_cwd:
        return Path.cwd().resolve()

    sys.stderr.write(
        "branch alignment: no service git root configured.\n"
        "Set CURSOR_SERVICE_REPO to an absolute path to your service clone,\n"
        "or set service_repo_root in .cursor/context/current-task.local.json,\n"
        "or pass --git-cwd DIR.\n"
        "(The previous default used the shell working directory and often picked the wrong repo;\n"
        "set CURSOR_ALLOW_ALIGN_BRANCH_CWD=1 only if you intentionally run from inside the service clone.)\n",
    )
    sys.exit(EXIT_NO_SERVICE_REPO)


def branch_alignment_requires_task_repository() -> bool:
    """When True (default), branch alignment requires task.repository. Opt out with CURSOR_REQUIRE_TASK_REPOSITORY=0."""
    raw = os.environ.get("CURSOR_REQUIRE_TASK_REPOSITORY")
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


def verify_repository_alignment(task: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    """Compare task.repository to git origin under repo_root."""
    expected = (task.get("repository") or "").strip()
    require_repo = branch_alignment_requires_task_repository()

    if not expected:
        if require_repo:
            return {
                "status": "missing_task_repository",
                "detail": (
                    "task.repository is empty. Load it from Jira via JIRA_REPOSITORY_FIELDS on pick-task, "
                    "or set task.repository in current-task.local.json to your service git remote URL/slug. "
                    "Use CURSOR_REQUIRE_TASK_REPOSITORY=0 only to skip this gate."
                ),
            }
        return {
            "status": "skipped_no_task_repository",
            "detail": "task.repository is not set; repo check skipped (CURSOR_REQUIRE_TASK_REPOSITORY=0).",
        }
    origin = remote_origin_url(repo_root=repo_root)
    if not origin:
        return {
            "status": "error_no_origin",
            "task_repository": expected,
            "origin": "",
            "detail": "No origin remote in this clone; cannot verify task.repository",
        }
    nt = normalize_git_remote(expected)
    no = normalize_git_remote(origin)
    out: dict[str, Any] = {
        "status": "match",
        "task_repository": expected,
        "origin": origin,
        "normalized_task_repository": nt,
        "normalized_origin": no,
    }
    if nt != no:
        out["status"] = "mismatch"
        out["detail"] = (
            "Mismatch in repo: task.repository does not match git remote origin for the service workspace folder."
        )
    return out


def proposed_suffix_from_task(task: dict[str, Any]) -> str:
    """Short headline-style suffix from Jira summary (keywords + acronyms, not a full sentence)."""
    label = (task.get("label") or task.get("summary") or "").strip()
    jira_key = (task.get("jira_key") or task.get("id") or "").strip()
    if jira_key and label.upper().startswith(jira_key.upper()):
        label = label[len(jira_key) :].lstrip(" :_-—\t")
    if not label:
        return "wip"

    parts: list[str] = []
    seen_lower: set[str] = set()
    n_words = 0
    n_acros = 0

    for m in _ORDERED_TOKEN_RE.finditer(label):
        if len(parts) >= _MAX_SLUG_PARTS:
            break
        raw = m.group(0)
        if _is_acronym_token(raw):
            if n_acros >= _MAX_SLUG_ACRONYMS:
                continue
            low = raw.lower()
            if low in seen_lower:
                continue
            parts.append(_format_acronym_for_slug(raw))
            seen_lower.add(low)
            n_acros += 1
            continue

        w = raw.lower()
        if len(w) < 2 or w in _STOPWORDS:
            continue
        if len(w) > _MAX_SLUG_WORD_CHARS:
            continue
        if w in seen_lower:
            continue
        if n_words >= _MAX_SLUG_WORDS:
            continue
        parts.append(w)
        seen_lower.add(w)
        n_words += 1

    if not parts:
        # Fallback: first alnum chunk from label (legacy-ish), still short.
        m = re.search(r"[A-Za-z][A-Za-z0-9]{1,11}", label)
        if not m:
            return "wip"
        parts = [m.group(0).lower()]

    s = "_".join(parts)
    s = _truncate_proposed_suffix(s)
    return s or "wip"


def _validate_suffix_fragment(suffix: str) -> None:
    if not suffix:
        sys.exit("Suffix is empty; aborting.")
    if "/" in suffix or suffix.startswith(".") or ".." in suffix:
        sys.exit("Invalid suffix: avoid '/', leading '.', and '..'.")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", suffix):
        sys.exit("Use only letters, digits, ._- in the suffix.")


def prompt_suffix(prefix: str, jira_key: str, task: dict[str, Any]) -> str:
    proposed = proposed_suffix_from_task(task)
    full_proposed = f"{prefix}{proposed}"
    print(
        f"No {jira_key}_* branch yet.\n"
        f"Proposed new branch: {full_proposed}",
        file=sys.stderr,
    )
    try:
        yn = input("Use this name? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("", file=sys.stderr)
        sys.exit(1)
    if yn in ("", "y", "yes"):
        _validate_suffix_fragment(proposed)
        return proposed

    print(
        f"Enter a different suffix only ({prefix}<suffix>; letters, digits, ._-):",
        file=sys.stderr,
    )
    try:
        line = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("", file=sys.stderr)
        sys.exit(1)
    _validate_suffix_fragment(line)
    return line


def pick_from_list(items: list[str], label: str) -> str:
    print(label, file=sys.stderr)
    for i, name in enumerate(items, start=1):
        print(f"  {i}) {name}", file=sys.stderr)
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("", file=sys.stderr)
            sys.exit(1)
        if not line.isdigit():
            print("Enter a number from the list.", file=sys.stderr)
            continue
        n = int(line)
        if 1 <= n <= len(items):
            return items[n - 1]
        print(f"Choose between 1 and {len(items)}.", file=sys.stderr)


def _proposal_block(task: dict[str, Any], jira_key: str) -> dict[str, str]:
    """Always-available proposed suffix/full branch (depends only on task data, not git)."""
    proposed_suffix = proposed_suffix_from_task(task) if task else "wip"
    return {
        "proposed_suffix": proposed_suffix,
        "proposed_full_branch": f"{jira_key}_{proposed_suffix}",
    }


def dry_run_payload(
    task: dict[str, Any],
    *,
    repo_root: Path,
    repository_check: dict[str, Any],
) -> dict[str, Any]:
    """Describe Jira ↔ branch linkage without prompting or changing git."""
    jira_key = (task.get("jira_key") or task.get("id") or "").strip()
    if not jira_key:
        return {
            "ok": False,
            "error": "Selected task has no jira_key",
            "jira_key": "",
            "repository_check": repository_check,
            "service_git_root": str(repo_root),
        }

    proposal = _proposal_block(task, jira_key)

    if repository_check.get("status") == "missing_task_repository":
        return {
            "ok": False,
            "error": "Mismatch in repo",
            "jira_key": jira_key,
            "branch_prefix": f"{jira_key}_",
            **proposal,
            "repository_check": repository_check,
            "service_git_root": str(repo_root),
        }

    if repository_check.get("status") == "mismatch":
        return {
            "ok": False,
            "error": "Mismatch in repo",
            "jira_key": jira_key,
            "branch_prefix": f"{jira_key}_",
            **proposal,
            "repository_check": repository_check,
            "service_git_root": str(repo_root),
        }

    if repository_check.get("status") == "error_no_origin":
        return {
            "ok": False,
            "error": repository_check.get("detail") or "Cannot verify repository",
            "jira_key": jira_key,
            "branch_prefix": f"{jira_key}_",
            **proposal,
            "repository_check": repository_check,
            "service_git_root": str(repo_root),
        }

    prefix = f"{jira_key}_"
    matches = branches_with_prefix(prefix, repo_root=repo_root)
    head = current_branch_name(repo_root=repo_root)
    proposed_suffix = proposal["proposed_suffix"]
    proposed_full_branch = proposal["proposed_full_branch"]
    if len(matches) == 0:
        action = "would_prompt_new_branch"
        detail = (
            "No local or origin/* branch starts with this prefix; interactive run would "
            "show a proposed branch name from the task summary, ask [Y/n] to accept or "
            "prompt for a custom suffix, then run git checkout -b <prefix><suffix>. "
            "Suffix rules: letters, digits, ._- only; no '/', leading '.', or '..'."
        )
    elif len(matches) == 1:
        action = "would_checkout"
        detail = f"Single match; interactive run would git checkout {matches[0]}."
    else:
        action = "would_prompt_pick_branch"
        detail = (
            "Multiple branches share this prefix; interactive run would list them and "
            "wait for a numeric choice."
        )
    aligned = head in matches if matches else False
    out: dict[str, Any] = {
        "ok": True,
        "jira_key": jira_key,
        "branch_prefix": prefix,
        "matching_branches": matches,
        "current_branch": head,
        "branch_aligned_with_jira": aligned,
        "planned_action": action,
        "planned_action_detail": detail,
        "suffix_validation": {
            "pattern": r"[A-Za-z0-9._-]+",
            "reject_substrings": ["/", ".."],
            "reject_prefix": ".",
        },
        "repository_check": repository_check,
        "service_git_root": str(repo_root),
        "proposed_suffix": proposed_suffix,
        "proposed_full_branch": proposed_full_branch,
    }
    return out


def merge_branch_alignment_into_context(
    *,
    repo_root: Path,
    branch_alignment: dict[str, Any],
    repository_check: dict[str, Any],
) -> None:
    path = task_context_path()
    try:
        doc = load_task_context_document(path)
    except FileNotFoundError as e:
        sys.exit(str(e))
    except json.JSONDecodeError as e:
        sys.exit(f"Cannot update {path}: {e}")
    except ValueError as e:
        sys.exit(str(e))

    prev_ba = doc.get("branch_alignment")
    if not isinstance(prev_ba, dict):
        prev_ba = {}

    merged_ba = {**prev_ba, **branch_alignment}
    # Preserve prior dry-run / preview text when post-checkout planned fields replace it.
    pa_prev = prev_ba.get("planned_action")
    pd_prev = prev_ba.get("planned_action_detail")
    if pa_prev is not None and pa_prev != merged_ba.get("planned_action"):
        merged_ba.setdefault("dry_run_planned_action", pa_prev)
    if pd_prev is not None and pd_prev != merged_ba.get("planned_action_detail"):
        merged_ba.setdefault("dry_run_planned_action_detail", pd_prev)

    checked = merged_ba.get("checked_out_branch")
    if isinstance(checked, str) and checked.strip():
        merged_ba["current_branch"] = checked.strip()
        merged_ba["branch_aligned_with_jira"] = True

    doc["branch_alignment"] = merged_ba

    prev_snap = doc.get("repo_snapshot")
    if not isinstance(prev_snap, dict):
        prev_snap = {}
    doc["repo_snapshot"] = {
        **prev_snap,
        "git_branch": current_branch_name(repo_root=repo_root),
        "git_dirty_hint": working_tree_dirty(repo_root=repo_root),
        "service_git_root": str(repo_root),
    }

    prev_fit = doc.get("repo_fit")
    if not isinstance(prev_fit, dict):
        prev_fit = {}

    if repository_check.get("status") == "match":
        doc["repo_fit"] = {
            **prev_fit,
            "status": "likely_fit",
            "notes": "task.repository matched origin",
        }
    elif repository_check.get("status") == "skipped_no_task_repository":
        # Keep existing notes (including null); do not inject explanatory text over saved context.
        doc["repo_fit"] = {**prev_fit, "status": "unknown"}

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--dry-run-json",
        action="store_true",
        help="Print JSON plan only (no git checkout; no prompts). For agents / automation.",
    )
    p.add_argument(
        "--git-cwd",
        type=Path,
        metavar="DIR",
        help="Root of the service git clone (overrides CURSOR_SERVICE_REPO and service_repo_root)",
    )
    p.add_argument(
        "--no-write-task-context",
        action="store_true",
        help="Do not write branch_alignment to current-task.local.json",
    )
    args = p.parse_args()
    write_task_context = not args.no_write_task_context

    try:
        raw_doc = load_task_context_document()
    except FileNotFoundError as e:
        sys.exit(str(e))
    except json.JSONDecodeError as e:
        sys.exit(f"Invalid JSON in {task_context_path()}: {e}")
    except ValueError as e:
        sys.exit(str(e))
    try:
        task = extract_task_from_document(raw_doc, path_for_errors=task_context_path())
    except ValueError as e:
        sys.exit(str(e))
    repo_root = resolve_git_root(args.git_cwd, raw_doc)
    repository_check = verify_repository_alignment(task, repo_root)

    if args.dry_run_json:
        payload = dry_run_payload(task, repo_root=repo_root, repository_check=repository_check)
        print(json.dumps(payload, indent=2))
        sys.exit(0 if payload.get("ok") else 1)

    if repository_check.get("status") == "missing_task_repository":
        print("Mismatch in repo", file=sys.stderr)
        print(
            json.dumps({"repository_check": repository_check}, indent=2),
            file=sys.stderr,
        )
        sys.exit(EXIT_MISMATCH_REPO)

    if repository_check.get("status") == "mismatch":
        print("Mismatch in repo", file=sys.stderr)
        print(
            json.dumps({"repository_check": repository_check}, indent=2),
            file=sys.stderr,
        )
        sys.exit(EXIT_MISMATCH_REPO)

    if repository_check.get("status") == "error_no_origin":
        sys.exit(
            repository_check.get("detail") or "Cannot verify task.repository without origin"
        )

    jira_key = (task.get("jira_key") or task.get("id") or "").strip()
    if not jira_key:
        sys.exit("Selected task has no jira_key")

    prefix = f"{jira_key}_"
    matches = branches_with_prefix(prefix, repo_root=repo_root)

    aligned_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if len(matches) == 0:
        suffix = prompt_suffix(prefix, jira_key, task)
        full = f"{prefix}{suffix}"
        proc = run_git("checkout", "-b", full, check=False, repo_root=repo_root)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            sys.exit(f"git checkout -b failed: {err}")
        print(f"Checked out branch: {full}", file=sys.stderr)
        print(full)
        if write_task_context:
            merge_branch_alignment_into_context(
                repo_root=repo_root,
                repository_check=repository_check,
                branch_alignment={
                    "ok": True,
                    "jira_key": jira_key,
                    "branch_prefix": prefix,
                    "checked_out_branch": full,
                    "planned_action": "created_branch",
                    "planned_action_detail": "Created new branch from Jira prefix + user suffix",
                    "aligned_at": aligned_at,
                    "suffix_entered": suffix,
                },
            )
        return

    if len(matches) == 1:
        checkout_branch(matches[0], repo_root=repo_root)
        print(f"Checked out branch: {matches[0]}", file=sys.stderr)
        print(matches[0])
        if write_task_context:
            merge_branch_alignment_into_context(
                repo_root=repo_root,
                repository_check=repository_check,
                branch_alignment={
                    "ok": True,
                    "jira_key": jira_key,
                    "branch_prefix": prefix,
                    "checked_out_branch": matches[0],
                    "planned_action": "checked_out_existing",
                    "planned_action_detail": "Single local or origin branch matched prefix",
                    "aligned_at": aligned_at,
                },
            )
        return

    chosen = pick_from_list(matches, "Pick branch #:")
    checkout_branch(chosen, repo_root=repo_root)
    print(f"Checked out branch: {chosen}", file=sys.stderr)
    print(chosen)
    if write_task_context:
        merge_branch_alignment_into_context(
            repo_root=repo_root,
            repository_check=repository_check,
            branch_alignment={
                "ok": True,
                "jira_key": jira_key,
                "branch_prefix": prefix,
                "checked_out_branch": chosen,
                "planned_action": "checked_out_existing",
                "planned_action_detail": "User picked from multiple matching branches",
                "matching_branches": matches,
                "aligned_at": aligned_at,
            },
        )


if __name__ == "__main__":
    main()
