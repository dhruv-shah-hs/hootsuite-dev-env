#!/usr/bin/env python3
"""
Update hootsuite-dev-env.code-workspace from the service catalog.

Examples:
  python3 .cursor/tools/configure-workspace.py --json
  python3 .cursor/tools/configure-workspace.py \\
    --primary service-organization \\
    --folders service-organization,service-organization-models,service-organization-idl
  python3 .cursor/tools/configure-workspace.py --interactive
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_CATALOG = Path(__file__).resolve().parent.parent / "context" / "workspace-services.json"
_WORKSPACE = Path(__file__).resolve().parent.parent.parent / "hootsuite-dev-env.code-workspace"
_DEV_ENV_FOLDER = "hootsuite-dev-env"


def _load_catalog() -> dict:
    data = json.loads(_CATALOG.read_text(encoding="utf-8"))
    services = {s["id"]: s for s in data.get("services", [])}
    always = data.get("always_include") or []
    presets = {p["id"]: p for p in data.get("workspace_presets") or [] if p.get("id")}
    return {"always_include": always, "services": services, "presets": presets, "raw": data}


def _preset_matches_current(preset: dict, current: dict | None) -> bool:
    if not current:
        return False
    return (
        current.get("primary_id") == preset.get("primary")
        and sorted(current.get("folder_ids") or []) == sorted(preset.get("folders") or [])
    )


def resolve_preset_id(query: str, presets: dict[str, dict]) -> str | None:
    """Map user text (e.g. 'organization', 'switch to entitlement') to a preset id."""
    q = query.strip().lower()
    if not q:
        return None
    for noise in ("switch to", "switch", "workspace", "stack", "use", "open", "the"):
        q = q.replace(noise, " ")
    q = " ".join(q.split())
    if q in presets:
        return q
    for pid, preset in presets.items():
        if q == preset.get("label", "").lower():
            return pid
        if q == preset.get("primary", "").lower():
            return pid
        for alias in preset.get("aliases") or []:
            al = str(alias).lower()
            if q == al or q in al or al in q:
                return pid
    return None



def _folder_order(primary: str, folder_ids: list[str], catalog: dict) -> list[str]:
    """Primary service first, then declared companions, then any remaining picks."""
    services = catalog["services"]
    companions = list(services.get(primary, {}).get("companions") or [])
    seen: set[str] = set()
    ordered: list[str] = []

    def add(fid: str) -> None:
        if fid in seen or fid not in folder_ids:
            return
        seen.add(fid)
        ordered.append(fid)

    add(primary)
    for c in companions:
        add(c)
    for fid in folder_ids:
        add(fid)
    return ordered


def build_workspace_doc(primary: str, folder_ids: list[str], catalog: dict) -> dict:
    services = catalog["services"]
    if primary not in services:
        raise ValueError(f"Unknown primary service: {primary}")
    unknown = [f for f in folder_ids if f not in services]
    if unknown:
        raise ValueError(f"Unknown folder id(s): {', '.join(unknown)}")

    folders: list[dict[str, str]] = list(catalog["always_include"])
    for fid in _folder_order(primary, folder_ids, catalog):
        entry = services[fid]
        folders.append({"name": entry["folder"], "path": entry["path"]})

    primary_folder = services[primary]["folder"]
    env_value = f"${{workspaceFolder:{primary_folder}}}"
    return {
        "folders": folders,
        "settings": {
            "cursor.primaryServiceFolder": primary_folder,
            "files.watcherExclude": {"**/target": True},
            "terminal.integrated.env.osx": {"CURSOR_SERVICE_REPO": env_value},
            "terminal.integrated.env.linux": {"CURSOR_SERVICE_REPO": env_value},
            "terminal.integrated.env.windows": {"CURSOR_SERVICE_REPO": env_value},
        },
    }


def write_workspace(doc: dict, workspace_path: Path) -> None:
    workspace_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")



def read_current_workspace(workspace_path: Path, catalog: dict) -> dict | None:
    """Parse current primary + folder ids from an existing .code-workspace file."""
    if not workspace_path.is_file():
        return None
    try:
        data = json.loads(workspace_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    services = catalog["services"]
    folder_by_name = {s["folder"]: sid for sid, s in services.items()}
    folder_names: list[str] = []
    for f in data.get("folders") or []:
        if isinstance(f, dict) and f.get("name"):
            folder_names.append(str(f["name"]))
    folder_ids = [folder_by_name[n] for n in folder_names if n in folder_by_name]
    primary_id: str | None = None
    settings = data.get("settings")
    if isinstance(settings, dict):
        pf = settings.get("cursor.primaryServiceFolder")
        if isinstance(pf, str) and pf in folder_by_name:
            primary_id = folder_by_name[pf]
    if primary_id is None:
        for fid in folder_ids:
            if services.get(fid, {}).get("kind") == "service":
                primary_id = fid
                break
    return {
        "primary_id": primary_id,
        "primary_service_folder": (
            services[primary_id]["folder"] if primary_id and primary_id in services else None
        ),
        "folder_ids": folder_ids,
        "folder_names": folder_names,
    }


def build_list_json(workspace_path: Path, catalog: dict) -> dict:
    """Compact catalog for agents (AskQuestion), including current workspace state."""
    services = catalog["services"]
    primary_options: list[dict] = []
    for sid, entry in services.items():
        if entry.get("kind") != "service":
            continue
        companions = list(entry.get("companions") or [])
        default_folders = [sid, *companions]
        primary_options.append(
            {
                "id": sid,
                "label": sid,
                "default_folders": default_folders,
                "companions": companions,
            }
        )
    folder_options = [
        {
            "id": sid,
            "label": sid,
            "kind": entry.get("kind"),
            "parent_service": entry.get("parent_service"),
        }
        for sid, entry in services.items()
    ]
    current = read_current_workspace(workspace_path, catalog)
    presets = catalog.get("presets") or {}
    workspace_presets: list[dict] = []
    for pid, preset in presets.items():
        workspace_presets.append(
            {
                "id": pid,
                "label": preset.get("label") or pid,
                "description": preset.get("description"),
                "primary": preset.get("primary"),
                "folders": list(preset.get("folders") or []),
                "is_current": _preset_matches_current(preset, current),
            }
        )
    return {
        "workspace_file": workspace_path.name,
        "current": current,
        "workspace_presets": workspace_presets,
        "primary_options": primary_options,
        "folder_options": folder_options,
    }


def _interactive_pick(catalog: dict) -> tuple[str, list[str]]:
    services = catalog["services"]
    service_ids = [sid for sid, s in services.items() if s.get("kind") == "service"]
    print("Primary service (CURSOR_SERVICE_REPO):", file=sys.stderr)
    for i, sid in enumerate(service_ids, 1):
        print(f"  {i}. {sid}", file=sys.stderr)
    while True:
        raw = input("> ").strip()
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(service_ids):
                primary = service_ids[idx]
                break
        elif raw in service_ids:
            primary = raw
            break
        print("Enter a number or service id.", file=sys.stderr)

    entry = services[primary]
    default_folders = [primary, *list(entry.get("companions") or [])]
    all_ids = list(services.keys())
    print("\nWorkspace folders (comma-separated numbers or ids; hootsuite-dev-env is always included):", file=sys.stderr)
    for i, sid in enumerate(all_ids, 1):
        mark = "*" if sid in default_folders else " "
        print(f" {mark}{i}. {sid}", file=sys.stderr)
    print(f"\nDefault for {primary}: {','.join(default_folders)}", file=sys.stderr)
    raw = input("> ").strip()
    if not raw:
        return primary, default_folders
    picked: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(all_ids):
                picked.append(all_ids[idx])
        elif part in services:
            picked.append(part)
    if primary not in picked:
        picked.insert(0, primary)
    return primary, picked


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure hootsuite-dev-env.code-workspace folders and primary service.")
    parser.add_argument("--primary", help="Primary service id (e.g. service-organization)")
    parser.add_argument("--folders", help="Comma-separated folder ids to include")
    parser.add_argument(
        "--preset",
        help="Apply a workspace preset id from workspace-services.json (e.g. organization, entitlement)",
    )
    parser.add_argument("--interactive", "-i", action="store_true", help="Prompt for primary and folders")
    parser.add_argument("--json", action="store_true", help="Print full catalog and exit")
    parser.add_argument(
        "--list-json",
        action="store_true",
        help="Print pick list for agents (primary_options, folder_options, current)",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=_WORKSPACE,
        help="Path to .code-workspace file (default: hootsuite-dev-env.code-workspace)",
    )
    args = parser.parse_args()

    catalog = _load_catalog()
    workspace_path = args.workspace.resolve()
    if args.json:
        print(json.dumps(catalog["raw"], indent=2))
        return
    if args.list_json:
        print(json.dumps(build_list_json(workspace_path, catalog), indent=2))
        return

    if args.preset:
        preset = (catalog.get("presets") or {}).get(args.preset)
        if not preset:
            parser.error(f"Unknown preset: {args.preset}")
        primary = str(preset["primary"])
        folder_ids = [str(f) for f in preset.get("folders") or []]
    elif args.interactive:
        primary, folder_ids = _interactive_pick(catalog)
    elif args.primary and args.folders:
        primary = args.primary
        folder_ids = [f.strip() for f in args.folders.split(",") if f.strip()]
    else:
        parser.error("Use --preset, --interactive, or both --primary and --folders")

    doc = build_workspace_doc(primary, folder_ids, catalog)
    write_workspace(doc, workspace_path)
    print(
        f"Wrote {args.workspace.name}: primary={primary}, folders="
        + ", ".join(f["name"] for f in doc["folders"]),
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
