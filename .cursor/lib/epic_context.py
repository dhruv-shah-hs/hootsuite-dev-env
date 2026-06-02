"""Shared helpers for pick-epic / save-epic-context."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_CURSOR_DIR = Path(__file__).resolve().parent.parent


def epic_context_file_path(workspace_root: Path) -> Path:
    return workspace_root.resolve() / ".cursor" / "context" / "current-epic.local.json"


def epic_context_path() -> Path:
    return epic_context_file_path(_CURSOR_DIR.parent)


def cloud_epic_dir() -> Path:
    """Per-user epic snapshots (optional --cloud target)."""
    return Path.home() / ".cursor" / "hootsuite-epics"


def cloud_epic_file_path(jira_key: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in jira_key.strip())
    return cloud_epic_dir() / f"{safe or 'epic'}.json"


def try_load_epic_document(path: Path | None = None) -> dict[str, Any] | None:
    p = epic_context_path() if path is None else path
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def extract_epic_from_document(doc: dict[str, Any]) -> dict[str, Any] | None:
    epic = doc.get("epic")
    return epic if isinstance(epic, dict) else None


def normalize_user_input(raw: dict[str, Any]) -> dict[str, Any]:
    """Required epic fields collected from the user (save / pick flow)."""
    desc = (raw.get("description") or "").strip()
    goal = (raw.get("goal") or "").strip()
    requirements = (raw.get("requirements") or "").strip()
    linked = raw.get("linked_tickets")
    tickets: list[str] = []
    if isinstance(linked, str):
        for part in linked.replace(",", " ").split():
            p = part.strip()
            if p:
                tickets.append(p)
    elif isinstance(linked, list):
        for item in linked:
            if isinstance(item, str) and item.strip():
                tickets.append(item.strip())
            elif isinstance(item, dict):
                k = (item.get("jira_key") or item.get("id") or item.get("key") or "").strip()
                if k:
                    tickets.append(k)
    return {
        "description": desc,
        "goal": goal,
        "requirements": requirements,
        "linked_tickets": tickets,
    }


def validate_user_input(ui: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if not (ui.get("description") or "").strip():
        missing.append("description")
    if not (ui.get("goal") or "").strip():
        missing.append("goal")
    if not (ui.get("requirements") or "").strip():
        missing.append("requirements")
    if not ui.get("linked_tickets"):
        missing.append("linked_tickets")
    return missing
