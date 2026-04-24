#!/usr/bin/env python3
"""
Build `.cursor/context/service-context.json` and refresh the VS Code attach entry in `.vscode/launch.json`.

Run from the hootsuite-dev-env repo root (after `resolve-task` and `align-branch`).

loads `./.env` so `CURSOR_SERVICE_REPO` is available to discovery (see `.cursor/lib/service_context.resolve_service_repo`).
Path resolution: `CURSOR_SERVICE_REPO` (local path), else `hootsuite-dev-env.code-workspace`, else sibling `../service-entitlement`.
"""

from __future__ import annotations

import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "lib"
if _LIB.is_dir() and str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

_CURSOR_DIR = Path(__file__).resolve().parent.parent
if str(_CURSOR_DIR) not in sys.path:
    sys.path.insert(0, str(_CURSOR_DIR))

from lib.dotenv import try_load_dotenv  # noqa: E402
from lib.service_context import (  # noqa: E402
    ServiceContextUnresolvedError,
    write_service_context,
)


def main() -> None:
    cwd = Path.cwd().resolve()
    try_load_dotenv(cwd)
    try:
        out = write_service_context(cwd)
    except ServiceContextUnresolvedError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    try:
        rel = out.relative_to(cwd)
    except ValueError:
        rel = out
    print(f"Wrote service context: {rel.as_posix()}", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
