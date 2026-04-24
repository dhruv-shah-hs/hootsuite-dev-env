"""Load ./.env for CLI tools; does not override existing environment variables."""

from __future__ import annotations

import os
from pathlib import Path


def try_load_dotenv(cwd: Path | None = None) -> None:
    """Load ``cwd``/``.env`` if present; does not override existing environment variables."""
    base = (cwd or Path.cwd()).resolve()
    path = base / ".env"
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
