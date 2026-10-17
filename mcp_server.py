#!/usr/bin/env python3
"""Backward-compatibility shim for memcore.mcp_server."""
from __future__ import annotations

import importlib.util
import pathlib
import sys

_OUTER_DIR = pathlib.Path(__file__).resolve().parent
_INNER_DIR = _OUTER_DIR / "memcore"
_INNER_FILE = _INNER_DIR / "mcp_server.py"

if str(_OUTER_DIR) not in sys.path:
    sys.path.insert(0, str(_OUTER_DIR))

_spec = importlib.util.spec_from_file_location("memcore.mcp_server", _INNER_FILE)
if _spec is None or _spec.loader is None:
    raise ImportError(f"Cannot load {_INNER_FILE}")
_mcp_server = importlib.util.module_from_spec(_spec)
sys.modules["memcore.mcp_server"] = _mcp_server
sys.modules[__name__] = _mcp_server
_spec.loader.exec_module(_mcp_server)

if __name__ == "__main__":
    sys.exit(_mcp_server.main())

