"""Resolve the product service repo (env, code-workspace, or sibling), then scan tech stack and Make targets for service-context.json."""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib.task_context import read_current_task_for_workspace

# Stable VS Code launch configuration names (used to replace prior generator runs).
_LAUNCH_ATTACH_JAVA_NAME = "Attach: service (JDWP)"
_LAUNCH_ATTACH_NODE_NAME = "Attach: service (Node)"
_LAUNCH_ATTACH_PYTHON_NAME = "Attach: service (debugpy)"
_LAUNCH_ATTACH_GO_NAME = "Attach: service (Delve)"
_LEGACY_LAUNCH_ATTACH_NAMES = frozenset(
    {
        "Attach: reference service (JDWP)",
        "Attach: reference service (Node)",
        "Attach: reference service (debugpy)",
        "Attach: reference service (Delve)",
    }
)
_ALL_LAUNCH_ATTACH_NAMES = frozenset(
    {
        _LAUNCH_ATTACH_JAVA_NAME,
        _LAUNCH_ATTACH_NODE_NAME,
        _LAUNCH_ATTACH_PYTHON_NAME,
        _LAUNCH_ATTACH_GO_NAME,
        *_LEGACY_LAUNCH_ATTACH_NAMES,
    }
)

_MAKEFILE_DIRECTIVE = frozenset(
    {
        "ifeq",
        "ifneq",
        "ifdef",
        "ifndef",
        "else",
        "endif",
        "define",
        "endef",
        "include",
        "sinclude",
        "override",
        "export",
        "private",
        "vpath",
        "suffix",
    }
)


def service_entitlement_dir(dev_env_root: Path) -> Path:
    """Default sibling `service-entitlement` next to this dev-env repo; prefer :func:`resolve_service_repo`."""
    return (dev_env_root.resolve().parent / "service-entitlement").resolve()


def _is_probably_git_url(value: str) -> bool:
    t = value.strip()
    if t.startswith(("https://", "http://", "ssh://")):
        return True
    if t.startswith("git@"):
        return True
    return False


def _service_path_from_code_workspace(dev_env_root: Path) -> Path | None:
    """
    If ``hootsuite-dev-env.code-workspace`` exists, resolve the service folder path (VS Code–style;
    relative paths are relative to the workspace file directory).

    Precedence within the workspace file:
      1. ``settings.cursor.primaryServiceFolder`` (set by configure-workspace.py)
      2. First folder whose name starts with ``service-`` and is not ``*-models`` / ``*-idl``
      3. Legacy name ``service-entitlement``
      4. First folder other than ``hootsuite-dev-env``
    """
    ws = dev_env_root / "hootsuite-dev-env.code-workspace"
    if not ws.is_file():
        return None
    try:
        data = json.loads(ws.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    folders = data.get("folders")
    if not isinstance(folders, list):
        return None
    base = ws.parent.resolve()
    settings = data.get("settings")
    primary_name: str | None = None
    if isinstance(settings, dict):
        raw = settings.get("cursor.primaryServiceFolder")
        if isinstance(raw, str) and raw.strip():
            primary_name = raw.strip()

    def folder_path(entry: dict) -> Path | None:
        rel = entry.get("path")
        if not rel:
            return None
        return (base / str(rel)).resolve()

    if primary_name:
        for f in folders:
            if isinstance(f, dict) and f.get("name") == primary_name:
                return folder_path(f)

    for f in folders:
        if not isinstance(f, dict):
            continue
        n = f.get("name") or ""
        if not n.startswith("service-") or n.endswith(("-models", "-idl")):
            continue
        p = folder_path(f)
        if p is not None:
            return p

    for f in folders:
        if isinstance(f, dict) and f.get("name") == "service-entitlement":
            return folder_path(f)

    for f in folders:
        if not isinstance(f, dict):
            continue
        n = f.get("name")
        if n and n != "hootsuite-dev-env":
            return folder_path(f)
    return None


def resolve_service_repo(dev_env_root: Path) -> tuple[Path | None, str, list[str]]:
    """
    Locate the product service repository root for context generation.

    Precedence: ``CURSOR_SERVICE_REPO`` (if a valid local path), then
    ``hootsuite-dev-env.code-workspace`` service folder, else sibling ``../service-entitlement``.

    Returns (path or None if only a URL is set), source tag, and provenance notes.
    """
    root = dev_env_root.resolve()
    notes: list[str] = []
    raw = (os.environ.get("CURSOR_SERVICE_REPO") or "").strip()

    if raw:
        if _is_probably_git_url(raw):
            return (
                None,
                "unresolved",
                [
                    "CURSOR_SERVICE_REPO looks like a git/HTTPS URL; a local clone path is not yet supported. "
                    "Set CURSOR_SERVICE_REPO to your local clone path, or unset it to use the code-workspace or sibling default."
                ],
            )
        candidate = Path(os.path.expanduser(raw)).resolve()
        if candidate.is_dir():
            return (candidate, "env", notes)
        notes.append(f"CURSOR_SERVICE_REPO is not a directory ({raw}); trying code-workspace and default.")

    from_ws = _service_path_from_code_workspace(root)
    if from_ws is not None and from_ws.is_dir():
        return (from_ws, "code_workspace", notes)

    if from_ws is not None and not from_ws.is_dir():
        notes.append(f"Code-workspace service path is missing or not a directory: {from_ws}.")

    sibling = service_entitlement_dir(root)
    return (sibling, "sibling_default", notes)


class ServiceContextUnresolvedError(RuntimeError):
    """Raised when the service repo path cannot be resolved (e.g. URL-only ``CURSOR_SERVICE_REPO``)."""


_RUNNABLE_KINDS = frozenset({"service", "frontend"})

_SERVICE_INSTANCE_FIELDS = frozenset(
    {
        "service_root",
        "makefile",
        "tech_stack",
        "toolchain",
        "service_git",
        "make_targets",
        "primary_commands",
        "endpoints",
        "tests",
        "config_surface",
        "docs_index",
        "task_repo_fit",
        "vscode_launch",
        "notes",
    }
)


def workspace_services_catalog_path(dev_env_root: Path) -> Path:
    return dev_env_root.resolve() / ".cursor" / "context" / "workspace-services.json"


def load_workspace_services_catalog(dev_env_root: Path) -> dict[str, Any]:
    """Load workspace-services.json; returns by_id and by_folder indexes."""
    path = workspace_services_catalog_path(dev_env_root)
    empty: dict[str, Any] = {"by_id": {}, "by_folder": {}}
    if not path.is_file():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    by_id: dict[str, dict[str, Any]] = {}
    by_folder: dict[str, dict[str, Any]] = {}
    for entry in data.get("services") or []:
        if not isinstance(entry, dict):
            continue
        sid = entry.get("id")
        folder = entry.get("folder")
        if isinstance(sid, str) and sid:
            by_id[sid] = entry
        if isinstance(folder, str) and folder:
            by_folder[folder] = entry
    return {"by_id": by_id, "by_folder": by_folder}


def _code_workspace_folder_entries(dev_env_root: Path) -> list[tuple[str, Path]]:
    """(folder_name, absolute_path) for each folder in hootsuite-dev-env.code-workspace."""
    ws = dev_env_root / "hootsuite-dev-env.code-workspace"
    if not ws.is_file():
        return []
    try:
        data = json.loads(ws.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    folders = data.get("folders")
    if not isinstance(folders, list):
        return []
    base = ws.parent.resolve()
    out: list[tuple[str, Path]] = []
    for f in folders:
        if not isinstance(f, dict):
            continue
        name = f.get("name")
        rel = f.get("path")
        if not isinstance(name, str) or not name or not rel:
            continue
        if name == "hootsuite-dev-env":
            continue
        out.append((name, (base / str(rel)).resolve()))
    return out


def discover_workspace_runnable_services(dev_env_root: Path) -> list[dict[str, Any]]:
    """Runnable services in the workspace file + catalog (id, folder, kind, path)."""
    catalog = load_workspace_services_catalog(dev_env_root)
    by_folder = catalog["by_folder"]
    runnables: list[dict[str, Any]] = []
    for folder_name, abs_path in _code_workspace_folder_entries(dev_env_root):
        entry = by_folder.get(folder_name)
        if not entry:
            continue
        kind = entry.get("kind")
        if kind not in _RUNNABLE_KINDS:
            continue
        runnables.append(
            {
                "id": entry["id"],
                "folder": folder_name,
                "kind": kind,
                "path": abs_path,
            }
        )
    return runnables


def resolve_primary_service_id(
    dev_env_root: Path,
    runnable_ids: list[str],
    *,
    primary_path: Path | None = None,
) -> str | None:
    """Primary id from env path, workspace setting, or first runnable."""
    catalog = load_workspace_services_catalog(dev_env_root)
    by_id = catalog["by_id"]
    by_folder = catalog["by_folder"]

    if primary_path is not None and primary_path.is_dir():
        resolved = primary_path.resolve()
        for entry in by_id.values():
            p = entry.get("path")
            if isinstance(p, str):
                candidate = (dev_env_root.resolve() / p).resolve()
                if candidate == resolved:
                    return entry.get("id")
        name = resolved.name
        for entry in by_id.values():
            if entry.get("folder") == name:
                return entry.get("id")

    ws = dev_env_root / "hootsuite-dev-env.code-workspace"
    if ws.is_file():
        try:
            data = json.loads(ws.read_text(encoding="utf-8"))
            settings = data.get("settings")
            if isinstance(settings, dict):
                primary_folder = settings.get("cursor.primaryServiceFolder")
                if isinstance(primary_folder, str) and primary_folder in by_folder:
                    return by_folder[primary_folder].get("id")
        except (OSError, json.JSONDecodeError):
            pass

    return runnable_ids[0] if runnable_ids else None


def normalize_service_context(doc: dict[str, Any]) -> dict[str, Any]:
    """Ensure services map exists; upgrade legacy single-service documents."""
    services = doc.get("services")
    if isinstance(services, dict) and services:
        return doc

    primary_id = doc.get("primary_service_id")
    if not isinstance(primary_id, str) or not primary_id:
        sr = doc.get("service_root")
        primary_id = Path(str(sr or ".")).name if sr else "primary"

    instance: dict[str, Any] = {"id": primary_id}
    for key in _SERVICE_INSTANCE_FIELDS:
        if key in doc:
            instance[key] = doc[key]

    doc = dict(doc)
    doc["services"] = {primary_id: instance}
    doc.setdefault("primary_service_id", primary_id)
    doc.setdefault("workspace_service_ids", [primary_id])
    return doc


def get_service_instance(doc: dict[str, Any], service_id: str | None = None) -> dict[str, Any] | None:
    doc = normalize_service_context(doc)
    sid = service_id or doc.get("primary_service_id")
    if not isinstance(sid, str):
        return None
    services = doc.get("services")
    if not isinstance(services, dict):
        return None
    inst = services.get(sid)
    return inst if isinstance(inst, dict) else None


def list_runnable_service_instances(doc: dict[str, Any]) -> list[dict[str, Any]]:
    doc = normalize_service_context(doc)
    services = doc.get("services")
    if not isinstance(services, dict):
        return []
    out: list[dict[str, Any]] = []
    for sid in doc.get("workspace_service_ids") or list(services.keys()):
        inst = services.get(sid)
        if not isinstance(inst, dict):
            continue
        if inst.get("kind") not in _RUNNABLE_KINDS:
            continue
        out.append(inst)
    return out


def load_service_context(cwd: Path | None = None) -> dict[str, Any] | None:
    path = service_context_path(cwd)
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return normalize_service_context(doc) if isinstance(doc, dict) else None


def _denormalize_primary_fields(doc: dict[str, Any]) -> None:
    """Mirror primary instance to document root for legacy consumers."""
    primary_id = doc.get("primary_service_id")
    if not isinstance(primary_id, str):
        return
    inst = get_service_instance(doc, primary_id)
    if not inst:
        return
    for key in _SERVICE_INSTANCE_FIELDS:
        if key in inst:
            doc[key] = inst[key]


def _path_posix_relative_to(base: Path, target: Path) -> str:
    """POSIX path from `base` to `target`, using `..` when the service is a sibling repo."""
    br = base.resolve()
    tr = target.resolve()
    try:
        return tr.relative_to(br).as_posix()
    except ValueError:
        return Path(os.path.relpath(tr, br)).as_posix()


def _find_makefiles(search_root: Path, max_depth: int = 4) -> list[Path]:
    """Makefiles under the service discovery directory, bounded depth (skip huge trees)."""
    found: list[Path] = []

    def walk(d: Path, depth: int) -> None:
        if depth > max_depth:
            return
        mf = d / "Makefile"
        if mf.is_file():
            found.append(mf)
        try:
            for sub in sorted(d.iterdir(), key=lambda p: p.name):
                if sub.is_dir() and not sub.name.startswith("."):
                    walk(sub, depth + 1)
        except OSError:
            return

    walk(search_root, 0)
    return found


def _pick_service_root(makefiles: list[Path], search_root: Path) -> Path | None:
    if not makefiles:
        return None

    def score(mf: Path) -> tuple:
        root = mf.parent
        has_sbt = (root / "build.sbt").is_file()
        has_pkg = (root / "package.json").is_file()
        try:
            depth = len(root.relative_to(search_root).parts)
        except ValueError:
            depth = 99
        return (has_sbt, has_pkg, -depth, str(root))

    best = max(makefiles, key=score)
    return best.parent


def _parse_make_targets(makefile: Path) -> list[dict[str, Any]]:
    try:
        text = makefile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    targets: list[dict[str, Any]] = []
    seen: set[str] = set()

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith("\t"):
            continue
        if "##" in line:
            left, _, right = line.partition("##")
            desc = right.strip() or None
        else:
            left, desc = line, None
        pre_comment = left.split("#", 1)[0]
        if ":=" in pre_comment or "?=" in pre_comment or "+=" in pre_comment:
            continue
        if ":" not in pre_comment:
            continue
        head = pre_comment.split(":", 1)[0].strip()
        if not head or head.startswith(".") or head.startswith("$"):
            continue
        name = head.split()[0]
        if not name or not re.match(r"^[A-Za-z0-9_.-]+$", name):
            continue
        if name in _MAKEFILE_DIRECTIVE:
            continue
        if name in seen:
            continue
        seen.add(name)
        targets.append({"name": name, "description": desc})
    return targets


def _git(service_root: Path, *args: str) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(service_root), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def _detect_service_git(service_root: Path) -> dict[str, Any]:
    """Branch / HEAD / remote / dirty / ahead-behind for the service repo (sibling clone)."""
    info: dict[str, Any] = {
        "is_git_repo": False,
        "branch": None,
        "head_sha": None,
        "head_short_sha": None,
        "remote_url": None,
        "default_branch": None,
        "dirty": False,
        "ahead": 0,
        "behind": 0,
        "tracking": None,
    }
    rc, _, _ = _git(service_root, "rev-parse", "--is-inside-work-tree")
    if rc != 0:
        return info
    info["is_git_repo"] = True

    rc, branch, _ = _git(service_root, "branch", "--show-current")
    info["branch"] = branch or None

    rc, sha, _ = _git(service_root, "rev-parse", "HEAD")
    if rc == 0 and sha:
        info["head_sha"] = sha
        info["head_short_sha"] = sha[:12]

    rc, remote, _ = _git(service_root, "remote", "get-url", "origin")
    if rc == 0 and remote:
        info["remote_url"] = remote

    rc, default_ref, _ = _git(service_root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if rc == 0 and default_ref:
        info["default_branch"] = default_ref.split("/", 1)[-1] if "/" in default_ref else default_ref
    else:
        for candidate in ("main", "master"):
            rc, _, _ = _git(service_root, "show-ref", "--verify", f"refs/remotes/origin/{candidate}")
            if rc == 0:
                info["default_branch"] = candidate
                break

    rc, porcelain, _ = _git(service_root, "status", "--porcelain")
    info["dirty"] = bool(porcelain)

    rc, upstream, _ = _git(service_root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if rc == 0 and upstream:
        info["tracking"] = upstream
        rc, counts, _ = _git(service_root, "rev-list", "--left-right", "--count", f"HEAD...{upstream}")
        if rc == 0 and counts:
            parts = counts.split()
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                info["ahead"] = int(parts[0])
                info["behind"] = int(parts[1])

    return info


_SCALA_VERSION_RE = re.compile(r"""scalaVersion\s*:?=\s*['"]([^'"]+)['"]""")
_NODE_ENGINES_RE = re.compile(r'"node"\s*:\s*"([^"]+)"')
_REQUIRES_PYTHON_RE = re.compile(r'requires-python\s*=\s*["\']([^"\']+)["\']')
_GO_VERSION_RE = re.compile(r"^go\s+([0-9][^\s]*)\s*$", re.MULTILINE)


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _detect_toolchain(service_root: Path, manifests: list[str]) -> dict[str, Any]:
    """Best-effort runtime/toolchain version pins. Values are strings as found, or null."""
    out: dict[str, Any] = {
        "sbt": None,
        "scala": None,
        "java": None,
        "node": None,
        "python": None,
        "go": None,
        "sources": [],
    }

    bp = service_root / "project" / "build.properties"
    if bp.is_file():
        for line in _read_text(bp).splitlines():
            if line.strip().startswith("sbt.version"):
                _, _, v = line.partition("=")
                v = v.strip()
                if v:
                    out["sbt"] = v
                    out["sources"].append("project/build.properties")
                break

    if "build.sbt" in manifests:
        text = _read_text(service_root / "build.sbt")
        m = _SCALA_VERSION_RE.search(text)
        if m:
            out["scala"] = m.group(1)
            out["sources"].append("build.sbt")
        else:
            for sub in ("project/Settings.scala", "project/Dependencies.scala", "project/plugins.sbt"):
                p = service_root / sub
                if p.is_file():
                    m = _SCALA_VERSION_RE.search(_read_text(p))
                    if m:
                        out["scala"] = m.group(1)
                        out["sources"].append(sub)
                        break

    tv = service_root / ".tool-versions"
    if tv.is_file():
        for line in _read_text(tv).splitlines():
            parts = line.strip().split()
            if len(parts) < 2:
                continue
            tool, version = parts[0].lower(), parts[1]
            if tool in {"java", "scala", "sbt", "nodejs", "node", "python", "golang", "go"}:
                key = "node" if tool in {"nodejs", "node"} else ("go" if tool in {"golang", "go"} else tool)
                if not out.get(key):
                    out[key] = version
        out["sources"].append(".tool-versions")

    sdkmanrc = service_root / ".sdkmanrc"
    if sdkmanrc.is_file():
        for line in _read_text(sdkmanrc).splitlines():
            if "=" in line and line.lower().startswith("java"):
                _, _, v = line.partition("=")
                v = v.strip()
                if v and not out.get("java"):
                    out["java"] = v
        out["sources"].append(".sdkmanrc")

    if "package.json" in manifests:
        text = _read_text(service_root / "package.json")
        m = _NODE_ENGINES_RE.search(text)
        if m and not out["node"]:
            out["node"] = m.group(1)
            out["sources"].append("package.json#engines.node")
    nvmrc = service_root / ".nvmrc"
    if nvmrc.is_file() and not out["node"]:
        v = _read_text(nvmrc).strip().lstrip("v")
        if v:
            out["node"] = v
            out["sources"].append(".nvmrc")

    if "pyproject.toml" in manifests:
        text = _read_text(service_root / "pyproject.toml")
        m = _REQUIRES_PYTHON_RE.search(text)
        if m and not out["python"]:
            out["python"] = m.group(1)
            out["sources"].append("pyproject.toml#requires-python")
    py_ver = service_root / ".python-version"
    if py_ver.is_file() and not out["python"]:
        v = _read_text(py_ver).strip()
        if v:
            out["python"] = v
            out["sources"].append(".python-version")

    if "go.mod" in manifests:
        text = _read_text(service_root / "go.mod")
        m = _GO_VERSION_RE.search(text)
        if m and not out["go"]:
            out["go"] = m.group(1)
            out["sources"].append("go.mod")

    parts = []
    for label, key in (("sbt", "sbt"), ("Scala", "scala"), ("Java", "java"), ("Node", "node"), ("Python", "python"), ("Go", "go")):
        if out.get(key):
            parts.append(f"{label} {out[key]}")
    out["summary"] = ", ".join(parts) if parts else "no pinned versions detected"

    out["sources"] = sorted(set(out["sources"]))
    return out


_LOCALHOST_PORT_RE = re.compile(r"localhost:(\d{2,5})\b")
_CONF_PORT_RE = re.compile(r"\b(?:port|PORT)\s*[:=]\s*(\d{2,5})\b")
_HTTP_PATH_RE = re.compile(r"(localhost:\d+)(/[\w\-/{}]+)")
_EXPOSE_RE = re.compile(r"^\s*EXPOSE\s+(?P<ports>[\d\s]+)", re.MULTILINE)
_DOCKERFILE_IMAGE_RE = re.compile(r"^\s*FROM\s+(?P<image>\S+)", re.MULTILINE)


def _unique_preserve(items: list) -> list:
    seen: set = set()
    out: list = []
    for i in items:
        key = json.dumps(i, sort_keys=True) if isinstance(i, dict) else i
        if key in seen:
            continue
        seen.add(key)
        out.append(i)
    return out


def _detect_endpoints(service_root: Path, makefile_text: str, docker_text: str, readme_text: str) -> dict[str, Any]:
    http_examples: list[dict[str, Any]] = []
    ports: list[int] = []

    def add_port(raw: str) -> None:
        if raw.isdigit():
            p = int(raw)
            if 80 <= p <= 65535:
                ports.append(p)

    for m in _EXPOSE_RE.finditer(docker_text):
        for token in m.group("ports").split():
            add_port(token)

    for m in _LOCALHOST_PORT_RE.finditer(readme_text):
        add_port(m.group(1))
    for m in _CONF_PORT_RE.finditer(readme_text):
        add_port(m.group(1))

    for m in _HTTP_PATH_RE.finditer(readme_text):
        host, path = m.group(1), m.group(2)
        host_port = host.split(":", 1)[-1]
        if host_port.isdigit():
            http_examples.append(
                {
                    "port": int(host_port),
                    "path": path,
                    "example": f"curl http://{host}{path}",
                }
            )

    app_conf = service_root / "config" / "default" / "application.conf"
    if app_conf.is_file():
        text = _read_text(app_conf)
        for m in _CONF_PORT_RE.finditer(text):
            add_port(m.group(1))

    ports = _unique_preserve(sorted(set(ports)))

    debug: dict[str, Any] = {}
    sbt_opts = re.search(r"jdwp[^ \n]*address=\*?:(\d{2,5})", makefile_text, re.IGNORECASE)
    if sbt_opts:
        debug["jdwp"] = int(sbt_opts.group(1))

    return {
        "ports": ports,
        "http_examples": _unique_preserve(http_examples),
        "debug": debug,
    }


def _detect_tests(service_root: Path, target_names: set[str], service_rel: str, tech: dict[str, Any]) -> dict[str, Any]:
    runners: list[dict[str, Any]] = []
    directories: list[str] = []

    def add_dir(rel: str) -> None:
        p = service_root / rel
        if p.is_dir():
            directories.append(rel)

    if "test" in target_names:
        runners.append({"name": "make test", "command": _shell_in_service(service_rel, "make test"), "scope": "unit"})
    if "compile-and-test-service" in target_names:
        runners.append(
            {
                "name": "make compile-and-test-service",
                "command": _shell_in_service(service_rel, "make compile-and-test-service"),
                "scope": "unit+compile",
            }
        )

    if "sbt" in (tech.get("build_tools") or []):
        runners.append({"name": "sbt test", "command": _shell_in_service(service_rel, "sbt test"), "scope": "unit"})
        add_dir("service/src/test")
        add_dir("models/src/test")

    if "run-contract-tests" in target_names:
        runners.append(
            {
                "name": "make run-contract-tests",
                "command": _shell_in_service(service_rel, "make run-contract-tests"),
                "scope": "contract (wasabi)",
            }
        )
    if (service_root / "tests" / "wasabi").is_dir():
        directories.append("tests/wasabi")

    for d in ("src/test", "src/__tests__", "tests", "spec"):
        add_dir(d)

    return {
        "runners": _unique_preserve(runners),
        "directories": _unique_preserve(directories),
    }


def _detect_config_and_secrets(service_root: Path, target_names: set[str], service_rel: str) -> dict[str, Any]:
    config_dirs: list[str] = []
    config_files: list[str] = []
    env_files: list[str] = []

    cfg_root = service_root / "config"
    if cfg_root.is_dir():
        try:
            for sub in sorted(cfg_root.iterdir()):
                if sub.is_dir():
                    rel = f"config/{sub.name}"
                    config_dirs.append(rel)
        except OSError:
            pass
        for name in ("application.conf", "policy.conf", "base.properties", "logback.xml"):
            p = cfg_root / "default" / name
            if p.is_file():
                config_files.append(f"config/default/{name}")

    dl = service_root / "darklaunch" / "service" / "entitlement.conf"
    if dl.is_file():
        config_files.append("darklaunch/service/entitlement.conf")

    for name in (".env", ".env.example", ".env.sample", ".env.local"):
        p = service_root / name
        if p.is_file():
            env_files.append(name)

    setup: list[dict[str, str]] = []
    if "vault-setup-local-dev" in target_names:
        setup.append(
            {
                "name": "vault-setup-local-dev",
                "command": _shell_in_service(service_rel, "make vault-setup-local-dev"),
                "purpose": "Fetch Vault secrets for local dev",
            }
        )

    return {
        "config_dirs": config_dirs,
        "config_files": config_files,
        "env_files": env_files,
        "setup_commands": setup,
    }


_DOC_CANDIDATES = [
    ("README.md", "Project overview"),
    ("CONTRIBUTING.md", "Contribution guide"),
    ("ArchitectureDecisionRecord.md", "Architecture decision records"),
    ("CODEOWNERS", "Code owners"),
    ("developer-portal.yml", "Developer portal metadata"),
    ("Jenkinsfile", "CI pipeline"),
    ("contract-tests.Jenkinsfile", "Contract test pipeline"),
    ("Dockerfile", "Runtime image"),
]


def _first_meaningful_line(text: str, max_len: int = 160) -> str | None:
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            line = line.lstrip("# ").strip()
        if line:
            if len(line) > max_len:
                line = line[: max_len - 1].rstrip() + "\u2026"
            return line
    return None


def _detect_docs(service_root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name, role in _DOC_CANDIDATES:
        p = service_root / name
        if not p.is_file():
            continue
        excerpt = _first_meaningful_line(_read_text(p)) if name.lower().endswith((".md",)) else None
        out.append({"path": name, "role": role, "excerpt": excerpt})

    docs_dir = service_root / "docs"
    if docs_dir.is_dir():
        try:
            for entry in sorted(docs_dir.iterdir()):
                if entry.is_file() and entry.suffix.lower() in {".md", ".txt", ".yaml", ".yml", ".json"}:
                    out.append({"path": f"docs/{entry.name}", "role": "docs directory file", "excerpt": None})
        except OSError:
            pass
    return out


_FIT_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "into",
        "onto",
        "behind",
        "which",
        "that",
        "this",
        "these",
        "those",
        "new",
        "old",
        "task",
        "story",
        "bug",
        "issue",
        "clean",
        "up",
        "add",
        "remove",
        "fix",
        "refactor",
        "migrate",
        "migration",
        "configuration",
        "config",
        "service",
        "services",
        "records",
        "record",
        "legacy",
        "staged",
        "backfill",
        "network",
        "ticket",
        "todo",
        "in",
        "out",
        "of",
        "to",
        "be",
        "behind",
        "hidden",
        "opt",
        "update",
        "create",
        "delete",
        "cleanup",
        "enable",
        "disable",
    }
)


def _task_keywords(task: dict[str, Any]) -> list[str]:
    label = (task.get("label") or "").strip()
    _, _, after_colon = label.partition(":")
    summary = (after_colon or label).strip()
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", summary)
    jira_key = (task.get("jira_key") or task.get("id") or "").strip()
    numeric = ""
    if jira_key and "-" in jira_key:
        numeric = jira_key.split("-", 1)[1]

    normalized: list[str] = []
    for w in words:
        lw = w.lower()
        if lw in _FIT_STOPWORDS:
            continue
        normalized.append(w)

    keywords: list[str] = []
    seen_lower: set[str] = set()
    if jira_key:
        keywords.append(jira_key)
        seen_lower.add(jira_key.lower())
    if numeric:
        keywords.append(numeric)
        seen_lower.add(numeric.lower())
    for w in normalized:
        lw = w.lower()
        if lw in seen_lower:
            continue
        seen_lower.add(lw)
        keywords.append(w)
    return keywords[:12]


def _compute_task_repo_fit(service_root: Path, task: dict[str, Any] | None) -> dict[str, Any]:
    if task is None:
        return {"signal": "unknown", "keywords": [], "matches": [], "notes": "No task pinned; run pick-task/save-task-context."}
    keywords = _task_keywords(task)
    if not keywords:
        return {"signal": "unknown", "keywords": [], "matches": [], "notes": "Task label has no usable keywords."}

    matches: list[dict[str, Any]] = []
    for kw in keywords:
        try:
            proc = subprocess.run(
                [
                    "git",
                    "-C",
                    str(service_root),
                    "grep",
                    "--ignore-case",
                    "--word-regexp",
                    "-I",
                    "-l",
                    kw,
                    "--",
                    ".",
                    ":(exclude)target",
                    ":(exclude)node_modules",
                    ":(exclude).metals",
                    ":(exclude).bsp",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=6,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode not in (0, 1):
            continue
        files = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
        if not files:
            continue
        matches.append({"keyword": kw, "file_count": len(files), "top_files": files[:5]})
        if len(matches) >= 6:
            break

    if not matches:
        signal = "none"
        notes = "No service files mention task keywords. This repo may be wrong for the task."
    else:
        strong = any(m["file_count"] >= 3 for m in matches)
        signal = "strong" if strong else "weak"
        notes = None

    return {"signal": signal, "keywords": keywords, "matches": matches, "notes": notes}


def _detect_tech_stack(service_root: Path) -> dict[str, Any]:
    langs: list[str] = []
    tools: list[str] = []
    manifests: list[str] = []

    def add(manifest: str, language: str | None, tool: str | None) -> None:
        p = service_root / manifest
        if p.is_file():
            manifests.append(manifest)
            if language and language not in langs:
                langs.append(language)
            if tool and tool not in tools:
                tools.append(tool)

    add("build.sbt", "Scala", "sbt")
    add("project/build.properties", None, "sbt")
    add("package.json", "JavaScript", "npm")
    add("pnpm-lock.yaml", None, "pnpm")
    add("yarn.lock", None, "yarn")
    add("go.mod", "Go", "go")
    add("Cargo.toml", "Rust", "cargo")
    add("pyproject.toml", "Python", "uv/poetry/pip")
    add("requirements.txt", "Python", "pip")
    add("pom.xml", "Java", "Maven")
    add("build.gradle", "Java/Kotlin", "Gradle")
    add("build.gradle.kts", "Java/Kotlin", "Gradle")

    if (service_root / "Makefile").is_file() and "Make" not in tools:
        tools.append("Make")

    summary_parts = []
    if langs:
        summary_parts.append(", ".join(langs))
    if tools:
        summary_parts.append("tools: " + ", ".join(tools))
    summary = "; ".join(summary_parts) if summary_parts else "unknown (no common manifests found)"

    return {
        "languages": langs,
        "build_tools": tools,
        "manifests": sorted(set(manifests)),
        "summary": summary,
    }


def _shell_in_service(service_rel: str, make_args: str) -> str:
    return f"cd {service_rel} && {make_args}"


def _primary_commands(service_rel_posix: str, target_names: set[str]) -> dict[str, str]:
    """Map common roles to `make` invocations when those targets exist."""
    out: dict[str, str] = {}
    mapping = [
        ("run", "run"),
        ("start", "start"),
        ("test", "test"),
        ("compile_and_test", "compile-and-test-service"),
        ("vault_setup_local_dev", "vault-setup-local-dev"),
        ("build_service", "build-service"),
        ("scalafmt", "scalafmt"),
        ("run_minikube", "run-minikube"),
        ("help", "help"),
    ]
    for key, target in mapping:
        if target in target_names:
            out[key] = _shell_in_service(service_rel_posix, f"make {target}")
    return out


def _stack_suggests_jvm_attach(tech: dict[str, Any]) -> bool:
    langs = tech.get("languages") or []
    tools = tech.get("build_tools") or []
    manifests = tech.get("manifests") or []
    if "Scala" in langs or "Java/Kotlin" in langs:
        return True
    if any(t in tools for t in ("sbt", "Maven", "Gradle")):
        return True
    if any(m in manifests for m in ("build.sbt", "pom.xml", "build.gradle", "build.gradle.kts")):
        return True
    return False


def _stack_suggests_node_attach(tech: dict[str, Any]) -> bool:
    return "JavaScript" in (tech.get("languages") or [])


def _stack_suggests_python_attach(tech: dict[str, Any]) -> bool:
    return "Python" in (tech.get("languages") or [])


def _stack_suggests_go_attach(tech: dict[str, Any]) -> bool:
    return "Go" in (tech.get("languages") or [])


def _launch_attach_config_name(service_id: str, stack_label: str) -> str:
    return f"Attach: {service_id} ({stack_label})"


def _vscode_attach_configuration(
    doc: dict[str, Any], *, service_id: str | None = None
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """
    Returns (launch_configuration, workspace_debug_meta) or None if unsupported / no service.
    """
    tech = doc.get("tech_stack") or {}
    sr = doc.get("service_root")
    sid = service_id if isinstance(service_id, str) and service_id else doc.get("id")
    if not sr or not isinstance(sid, str) or not sid:
        return None

    if _stack_suggests_jvm_attach(tech):
        meta = {
            "kind": "jvm-jdwp",
            "port": 5005,
            "vscode_type": "java",
            "hint": (
                f"Start the service JVM with JDWP on port 5005, then start this attach config. "
                f"Example (sbt): cd {sr} && SBT_OPTS=\"-agentlib:jdwp=transport=dt_socket,server=y,suspend=n,address=*:5005\" "
                f"make run  — or use sbt's JVM debug flag if your project documents one."
            ),
        }
        cfg: dict[str, Any] = {
            "type": "java",
            "request": "attach",
            "name": _launch_attach_config_name(sid, "JDWP"),
            "hostName": "localhost",
            "port": meta["port"],
        }
        return cfg, meta

    if _stack_suggests_node_attach(tech):
        meta = {
            "kind": "node-inspector",
            "port": 9229,
            "vscode_type": "node",
            "hint": (
                f"Start Node with --inspect (e.g. node --inspect=9229 …) from {sr}, then attach. "
                "Adjust port if yours differs."
            ),
        }
        cfg = {
            "type": "node",
            "request": "attach",
            "name": _launch_attach_config_name(sid, "Node"),
            "address": "localhost",
            "port": meta["port"],
            "restart": True,
        }
        return cfg, meta

    if _stack_suggests_python_attach(tech):
        meta = {
            "kind": "debugpy",
            "port": 5678,
            "vscode_type": "debugpy",
            "hint": (
                f"Start Python with debugpy listening (e.g. python -m debugpy --listen 5678 --wait-for-client your_app.py) "
                f"under {sr}, then attach."
            ),
        }
        cfg = {
            "type": "debugpy",
            "request": "attach",
            "name": _launch_attach_config_name(sid, "debugpy"),
            "connect": {"host": "localhost", "port": meta["port"]},
            "justMyCode": False,
        }
        return cfg, meta

    if _stack_suggests_go_attach(tech):
        meta = {
            "kind": "delve-remote",
            "port": 2345,
            "vscode_type": "go",
            "hint": (
                f"Run Delve in headless mode (e.g. dlv debug --headless --listen=:2345 --api-version=2) for the binary under {sr}, "
                "then attach."
            ),
        }
        cfg = {
            "type": "go",
            "request": "attach",
            "name": _launch_attach_config_name(sid, "Delve"),
            "mode": "remote",
            "port": meta["port"],
            "host": "127.0.0.1",
        }
        return cfg, meta

    return None


def _launch_meta_from_pair(
    root: Path, cfg: dict[str, Any], meta: dict[str, Any], inst: dict[str, Any]
) -> dict[str, Any]:
    launch_path = root / ".vscode" / "launch.json"
    stable_name = cfg.get("name")
    try:
        rel = launch_path.relative_to(root).as_posix()
    except ValueError:
        rel = str(launch_path)
    sr = inst.get("service_root")
    service_name = Path(str(sr or ".")).name
    return {
        "path": rel,
        "configuration_name": stable_name,
        "service_name": service_name,
        "debug_attach": meta,
    }


def _is_generator_attach_name(name: str, service_ids: frozenset[str]) -> bool:
    if not isinstance(name, str):
        return False
    for sid in service_ids:
        prefix = f"Attach: {sid} ("
        if name.startswith(prefix) and name.endswith(")"):
            return True
    return False


def merge_vscode_launch_for_all_services(root: Path, services: dict[str, dict[str, Any]]) -> None:
    """
    Upsert attach configurations in `.vscode/launch.json` for every service instance.

    Mutates each instance: sets ``vscode_launch`` when an attach template exists, otherwise may append a note.
    """
    vscode_dir = root / ".vscode"
    vscode_dir.mkdir(parents=True, exist_ok=True)
    launch_path = vscode_dir / "launch.json"

    if launch_path.is_file():
        try:
            data = json.loads(launch_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {"version": "0.2.0", "configurations": []}
    else:
        data = {"version": "0.2.0", "configurations": []}

    if not isinstance(data, dict):
        data = {"version": "0.2.0", "configurations": []}
    cfgs = data.get("configurations")
    if not isinstance(cfgs, list):
        cfgs = []

    sid_set = frozenset(services.keys())
    cfgs = [
        c
        for c in cfgs
        if not (
            isinstance(c, dict)
            and (
                c.get("name") in _ALL_LAUNCH_ATTACH_NAMES
                or _is_generator_attach_name(c.get("name") or "", sid_set)
            )
        )
    ]

    pending: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for sid, inst in services.items():
        pair = _vscode_attach_configuration(inst, service_id=sid)
        if not pair:
            inst.pop("vscode_launch", None)
            if inst.get("service_root"):
                inst.setdefault("notes", []).append(
                    "No VS Code attach template for this stack; add an attach configuration to .vscode/launch.json manually."
                )
            continue
        cfg, meta = pair
        cfg_out = copy.deepcopy(cfg)
        pending.append((sid, cfg_out, meta))
        cfgs.append(cfg_out)

    data["configurations"] = cfgs
    data.setdefault("version", "0.2.0")
    launch_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    for sid, cfg_out, meta in pending:
        inst = services[sid]
        inst["vscode_launch"] = _launch_meta_from_pair(root, cfg_out, meta, inst)


def merge_vscode_launch_attach(
    root: Path, doc: dict[str, Any], *, service_id: str | None = None
) -> dict[str, Any] | None:
    """
    Upsert attach config(s) for a single service instance document.

    Delegates to :func:`merge_vscode_launch_for_all_services`.
    """
    sid = service_id if isinstance(service_id, str) and service_id else doc.get("id")
    if not isinstance(sid, str) or not sid:
        return None
    merge_vscode_launch_for_all_services(root, {sid: doc})
    vlc = doc.get("vscode_launch")
    return vlc if isinstance(vlc, dict) else None


def _empty_service_instance(service_id: str, kind: str) -> dict[str, Any]:
    return {
        "id": service_id,
        "kind": kind,
        "service_root": None,
        "makefile": None,
        "tech_stack": {"languages": [], "build_tools": [], "manifests": [], "summary": ""},
        "toolchain": {
            "sbt": None,
            "scala": None,
            "java": None,
            "node": None,
            "python": None,
            "go": None,
            "sources": [],
            "summary": "no pinned versions detected",
        },
        "service_git": {
            "is_git_repo": False,
            "branch": None,
            "head_sha": None,
            "head_short_sha": None,
            "remote_url": None,
            "default_branch": None,
            "dirty": False,
            "ahead": 0,
            "behind": 0,
            "tracking": None,
        },
        "make_targets": [],
        "primary_commands": {},
        "endpoints": {"ports": [], "http_examples": [], "debug": {}},
        "tests": {"runners": [], "directories": []},
        "config_surface": {"config_dirs": [], "config_files": [], "env_files": [], "setup_commands": []},
        "docs_index": [],
        "task_repo_fit": {"signal": "unknown", "keywords": [], "matches": [], "notes": None},
        "notes": [],
    }


def build_service_instance(
    root: Path,
    service_base: Path,
    service_id: str,
    kind: str,
    task: dict[str, Any] | None,
    is_primary: bool,
    resolve_notes: list[str],
) -> dict[str, Any]:
    inst = _empty_service_instance(service_id, kind)
    notes: list[str] = list(inst["notes"])

    if not service_base.is_dir():
        notes.append(
            f"Service directory is missing: {service_base}. "
            "Clone the repository or set CURSOR_SERVICE_REPO to a valid local path."
        )
        inst["tech_stack"]["summary"] = "unknown (service directory not found)"
        for n in resolve_notes:
            if n:
                notes.append(n)
        inst["notes"] = notes
        return inst

    makefiles = _find_makefiles(service_base)
    if not makefiles:
        notes.append(
            f"No Makefile found under the service root (within search depth): {service_base}."
        )
        inst["tech_stack"]["summary"] = "unknown (no Makefile)"
        for n in resolve_notes:
            if n:
                notes.append(n)
        inst["notes"] = notes
        return inst

    service_root = _pick_service_root(makefiles, service_base)
    assert service_root is not None
    makefile = service_root / "Makefile"
    if not makefile.is_file():
        notes.append("Selected service root has no Makefile at its root; using first Makefile path.")
        makefile = makefiles[0]
        service_root = makefile.parent

    service_rel = _path_posix_relative_to(root, service_root)

    targets = _parse_make_targets(makefile)
    names = {t["name"] for t in targets}

    inst["service_root"] = service_rel
    inst["makefile"] = _path_posix_relative_to(root, makefile)
    inst["make_targets"] = targets
    inst["tech_stack"] = _detect_tech_stack(service_root)
    inst["toolchain"] = _detect_toolchain(service_root, inst["tech_stack"]["manifests"])
    inst["service_git"] = _detect_service_git(service_root)
    inst["primary_commands"] = _primary_commands(service_rel, names)

    makefile_text = _read_text(makefile)
    docker_text = _read_text(service_root / "Dockerfile")
    readme_text = _read_text(service_root / "README.md")
    inst["endpoints"] = _detect_endpoints(service_root, makefile_text, docker_text, readme_text)
    inst["tests"] = _detect_tests(service_root, names, service_rel, inst["tech_stack"])
    inst["config_surface"] = _detect_config_and_secrets(service_root, names, service_rel)
    inst["docs_index"] = _detect_docs(service_root)
    if is_primary:
        inst["task_repo_fit"] = _compute_task_repo_fit(service_root, task)

    if not names:
        notes.append("Makefile present but no targets parsed (unusual syntax).")
    if "test" not in names:
        notes.append("Makefile has no `test` target; add one or use another test entrypoint.")
    if "run" not in names and "start" not in names:
        notes.append("Makefile has no `run` or `start` target; add one for local service startup.")
    if is_primary and inst["task_repo_fit"]["signal"] == "none":
        notes.append(
            "Task keywords did not match any service files; confirm this is the right repo or refine the Jira label."
        )
    if is_primary:
        for n in resolve_notes:
            if n:
                notes.append(n)

    inst["notes"] = notes
    return inst


def resolve_run_command(inst: dict[str, Any]) -> str | None:
    """Prefer ``primary_commands.run``, then ``start``."""
    pc = inst.get("primary_commands")
    if not isinstance(pc, dict):
        return None
    for key in ("run", "start"):
        raw = pc.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def build_service_context(cwd: Path | None = None) -> dict[str, Any]:
    """
    Build the service-context document (does not write to disk).

    Discovers runnable workspace services, fills ``services`` keyed by id, and mirrors the primary
    instance to the document root via :func:`_denormalize_primary_fields`.
    """
    root = (cwd or Path.cwd()).resolve()
    service_abs, src, resolve_notes = resolve_service_repo(root)
    generated_at = datetime.now(timezone.utc).isoformat()
    path_meta: dict[str, Any] = {"source": src, "provenance_notes": list(resolve_notes)}
    task = read_current_task_for_workspace(root)

    runnables = discover_workspace_runnable_services(root)
    runnable_ids: list[str] = []
    for r in runnables:
        rid = r.get("id")
        if isinstance(rid, str) and rid:
            runnable_ids.append(rid)

    primary_id = resolve_primary_service_id(root, runnable_ids, primary_path=service_abs)

    services: dict[str, dict[str, Any]] = {}
    for r in runnables:
        sid = r.get("id")
        rkind = r.get("kind")
        rpath = r.get("path")
        if not isinstance(sid, str) or not sid or rkind not in _RUNNABLE_KINDS or not isinstance(rpath, Path):
            continue
        inst = build_service_instance(
            root,
            rpath,
            sid,
            str(rkind),
            task,
            sid == primary_id,
            resolve_notes,
        )
        services[sid] = inst

    doc: dict[str, Any] = {
        "$schema": "./schema/service-context.schema.json",
        "generated_at": generated_at,
        "service_path_resolution": path_meta,
        "services": services,
        "primary_service_id": primary_id,
        "workspace_service_ids": runnable_ids,
    }

    if not services:
        for n in resolve_notes:
            if n:
                doc.setdefault("notes", []).append(n)

    _denormalize_primary_fields(doc)
    return doc


def service_context_path(cwd: Path | None = None) -> Path:
    root = (cwd or Path.cwd()).resolve()
    return root / ".cursor" / "context" / "service-context.json"


def write_service_context(cwd: Path | None = None) -> Path:
    """Write `.cursor/context/service-context.json` and update `.vscode/launch.json` attach configs."""
    root = (cwd or Path.cwd()).resolve()
    doc = build_service_context(root)
    spr = doc.get("service_path_resolution") or {}
    services = doc.get("services")
    if not isinstance(services, dict):
        services = {}

    if spr.get("source") == "unresolved" and not services:
        notes = spr.get("provenance_notes")
        if isinstance(notes, list) and notes:
            msg = " ".join(str(x) for x in notes)
        else:
            msg = "Could not resolve service path (e.g. CURSOR_SERVICE_REPO is a URL with no local clone)"
        raise ServiceContextUnresolvedError(msg)

    if services:
        merge_vscode_launch_for_all_services(root, services)
        _denormalize_primary_fields(doc)

    out = service_context_path(root)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return out
