"""Shared pytest configuration for Aetheer v3 tests."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_tv_unified_pkg() -> None:
    """Load mcp-servers/tv-unified/ as the importable package `tv_unified_pkg`.

    The directory name has a dash, so plain `import` won't work. The MCP
    server uses the same shim — we mirror it here so tests can write
    `from tv_unified_pkg.cdp_drawing import ...`.
    """
    if "tv_unified_pkg" in sys.modules:
        return
    pkg_dir = REPO_ROOT / "mcp-servers" / "tv-unified"
    init_path = pkg_dir / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "tv_unified_pkg",
        init_path,
        submodule_search_locations=[str(pkg_dir)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load tv-unified package at {pkg_dir}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["tv_unified_pkg"] = module
    spec.loader.exec_module(module)


_load_tv_unified_pkg()


pytest_plugins = ["tests.fixtures.cdp_fixtures"]
