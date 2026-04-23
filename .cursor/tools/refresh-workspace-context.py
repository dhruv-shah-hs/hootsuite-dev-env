#!/usr/bin/env python3
"""
Write `.cursor/task-context/workspace-context.json` without Jira or pick-task.

Run from the **hootsuite-dev-env** repository root (the directory that contains
`.cursor/`). Loads `./.env` when present without overriding existing env vars
(same behavior as `pick-task.py`), so `CURSOR_SERVICE_REPO` and similar apply.

Examples:
  python3 .cursor/tools/refresh-workspace-context.py
  python3 .cursor/tools/refresh-workspace-context.py -q
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "lib"
if _LIB.is_dir() and str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

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


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Print only the output path (stdout); no stderr status line",
    )
    args = p.parse_args()

    try_load_dotenv()
    cwd = Path.cwd()
    try:
        out = write_workspace_context(cwd)
        rel = out.relative_to(cwd.resolve())
    except (OSError, ValueError) as e:
        sys.exit(f"Could not write workspace-context.json: {e}")

    if args.quiet:
        print(rel.as_posix())
    else:
        print(f"Wrote workspace context: {rel}", file=sys.stderr)


if __name__ == "__main__":
    main()
