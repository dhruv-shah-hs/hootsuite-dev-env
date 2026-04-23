from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def run_git(
    *args: str,
    check: bool = True,
    repo_root: Path | str | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = ["git"]
    if repo_root is not None:
        cmd.extend(["-C", str(Path(repo_root).resolve())])
    cmd.extend(args)
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def git_lines(*args: str, repo_root: Path | str | None = None) -> list[str]:
    p = run_git(*args, check=False, repo_root=repo_root)
    if p.returncode != 0:
        return []
    return [ln.strip() for ln in p.stdout.splitlines() if ln.strip()]


def remote_origin_url(repo_root: Path | str | None = None) -> str:
    """Best-effort `origin` remote URL; empty string if missing or not a git repo."""
    p = run_git("remote", "get-url", "origin", check=False, repo_root=repo_root)
    if p.returncode != 0:
        return ""
    return (p.stdout or "").strip()


def normalize_git_remote(ref: str) -> str:
    """Comparable form for SSH / HTTPS Git remote URLs (best-effort)."""
    from urllib.parse import urlparse

    s = ref.strip().lower()
    if s.endswith(".git"):
        s = s[:-4]
    if s.startswith("git@"):
        _, _, tail = s.partition("@")
        host, _, path = tail.partition(":")
        return f"{host}/{path}".strip("/")
    if "://" in s:
        u = urlparse(s)
        netloc = (u.netloc or "").lower()
        if "@" in netloc:
            netloc = netloc.split("@")[-1]
        path = (u.path or "").strip("/")
        return f"{netloc}/{path}".strip("/")
    return s.strip("/")


def branches_with_prefix(prefix: str, repo_root: Path | str | None = None) -> list[str]:
    """Local and remote-tracking branch short names starting with prefix (deduped, sorted)."""
    names: set[str] = set()
    for ref in git_lines(
        "for-each-ref", "--format=%(refname:short)", "refs/heads/", repo_root=repo_root
    ):
        if ref.startswith(prefix):
            names.add(ref)
    for ref in git_lines(
        "for-each-ref", "--format=%(refname:short)", "refs/remotes/", repo_root=repo_root
    ):
        if ref.startswith("origin/"):
            short = ref[len("origin/") :]
            if short.startswith(prefix):
                names.add(short)
    return sorted(names)


def checkout_branch(name: str, repo_root: Path | str | None = None) -> None:
    p = run_git("checkout", name, check=False, repo_root=repo_root)
    if p.returncode == 0:
        print(f"Checked out {name}", file=sys.stderr)
        return
    err = (p.stderr or p.stdout or "").strip()
    sys.exit(f"git checkout failed: {err}")


def current_branch_name(repo_root: Path | str | None = None) -> str:
    p = run_git("branch", "--show-current", check=False, repo_root=repo_root)
    if p.returncode != 0:
        return ""
    return (p.stdout or "").strip()


def branch_exists_local(name: str, repo_root: Path | str | None = None) -> bool:
    """True if a local branch named `name` exists."""
    p = run_git(
        "show-ref", "--verify", f"refs/heads/{name}", check=False, repo_root=repo_root
    )
    return p.returncode == 0


def branch_exists_on_origin(name: str, repo_root: Path | str | None = None) -> bool:
    """True if `origin/<name>` remote-tracking ref exists."""
    p = run_git(
        "show-ref",
        "--verify",
        f"refs/remotes/origin/{name}",
        check=False,
        repo_root=repo_root,
    )
    return p.returncode == 0


def is_branch_deleted(name: str, repo_root: Path | str | None = None) -> bool:
    """
    Best-effort: consider the branch deleted if it exists neither locally nor as
    an `origin/*` remote-tracking ref.

    Note: This does not contact the network; it relies on current remote refs.
    """
    return (not branch_exists_local(name, repo_root=repo_root)) and (
        not branch_exists_on_origin(name, repo_root=repo_root)
    )


def is_branch_merged_into_master(
    name: str, master_ref: str = "master", repo_root: Path | str | None = None
) -> bool:
    """True if `name` is fully merged into `master_ref` (default: `master`)."""
    p = run_git(
        "merge-base",
        "--is-ancestor",
        name,
        master_ref,
        check=False,
        repo_root=repo_root,
    )
    return p.returncode == 0


def working_tree_dirty(repo_root: Path | str | None = None) -> bool:
    p = run_git("status", "--porcelain", check=False, repo_root=repo_root)
    if p.returncode != 0:
        return False
    return bool((p.stdout or "").strip())
