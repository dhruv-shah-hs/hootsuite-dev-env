from __future__ import annotations

import subprocess
import sys


def run_git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=check,
    )


def git_lines(*args: str) -> list[str]:
    p = run_git(*args, check=False)
    if p.returncode != 0:
        return []
    return [ln.strip() for ln in p.stdout.splitlines() if ln.strip()]


def branches_with_prefix(prefix: str) -> list[str]:
    """Local and remote-tracking branch short names starting with prefix (deduped, sorted)."""
    names: set[str] = set()
    for ref in git_lines("for-each-ref", "--format=%(refname:short)", "refs/heads/"):
        if ref.startswith(prefix):
            names.add(ref)
    for ref in git_lines("for-each-ref", "--format=%(refname:short)", "refs/remotes/"):
        if ref.startswith("origin/"):
            short = ref[len("origin/") :]
            if short.startswith(prefix):
                names.add(short)
    return sorted(names)


def checkout_branch(name: str) -> None:
    p = run_git("checkout", name, check=False)
    if p.returncode == 0:
        print(f"Checked out {name}", file=sys.stderr)
        return
    err = (p.stderr or p.stdout or "").strip()
    sys.exit(f"git checkout failed: {err}")


def current_branch_name() -> str:
    p = run_git("branch", "--show-current", check=False)
    if p.returncode != 0:
        return ""
    return (p.stdout or "").strip()

def branch_exists_local(name: str) -> bool:
    """True if a local branch named `name` exists."""
    p = run_git("show-ref", "--verify", f"refs/heads/{name}", check=False)
    return p.returncode == 0


def branch_exists_on_origin(name: str) -> bool:
    """True if `origin/<name>` remote-tracking ref exists."""
    p = run_git("show-ref", "--verify", f"refs/remotes/origin/{name}", check=False)
    return p.returncode == 0


def is_branch_deleted(name: str) -> bool:
    """
    Best-effort: consider the branch deleted if it exists neither locally nor as
    an `origin/*` remote-tracking ref.

    Note: This does not contact the network; it relies on current remote refs.
    """
    return (not branch_exists_local(name)) and (not branch_exists_on_origin(name))


def is_branch_merged_into_master(name: str, master_ref: str = "master") -> bool:
    """True if `name` is fully merged into `master_ref` (default: `master`)."""
    # `merge-base --is-ancestor A B` exits 0 iff A is an ancestor of B.
    p = run_git("merge-base", "--is-ancestor", name, master_ref, check=False)
    return p.returncode == 0
