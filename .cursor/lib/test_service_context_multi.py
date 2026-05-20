"""Tests for multi-service service-context helpers."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_LIB = Path(__file__).resolve().parent
if str(_LIB.parent) not in sys.path:
    sys.path.insert(0, str(_LIB.parent))

from lib.service_context import (  # noqa: E402
    build_service_context,
    discover_workspace_runnable_services,
    get_service_instance,
    normalize_service_context,
    resolve_run_command,
)


class TestMultiServiceContext(unittest.TestCase):
    def test_normalize_legacy_document(self) -> None:
        legacy = {
            "service_root": "../dashboard",
            "primary_commands": {"start": "cd ../dashboard && make start"},
        }
        doc = normalize_service_context(legacy)
        self.assertIn("services", doc)
        self.assertIn("dashboard", doc["services"])
        inst = get_service_instance(doc, "dashboard")
        assert inst is not None
        self.assertEqual(inst["primary_commands"]["start"], legacy["primary_commands"]["start"])

    def test_resolve_run_command_prefers_run_then_start(self) -> None:
        self.assertEqual(
            resolve_run_command({"primary_commands": {"start": "make start", "run": "make run"}}),
            "make run",
        )
        self.assertEqual(
            resolve_run_command({"primary_commands": {"start": "make start"}}),
            "make start",
        )

    def test_build_includes_workspace_runnables(self) -> None:
        root = Path(__file__).resolve().parent.parent.parent
        runnables = discover_workspace_runnable_services(root)
        ids = {r["id"] for r in runnables}
        self.assertIn("dashboard", ids)

        doc = build_service_context(root)
        services = doc.get("services")
        self.assertIsInstance(services, dict)
        self.assertIn("dashboard", services)
        self.assertEqual(doc.get("primary_service_id"), "dashboard")


if __name__ == "__main__":
    unittest.main()
