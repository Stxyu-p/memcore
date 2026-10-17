#!/usr/bin/env python3
"""Backward-compatibility shim for memcore.mcp_server."""
from __future__ import annotations

import sys
from memcore import mcp_server as _mcp_server

sys.modules[__name__] = _mcp_server

if __name__ == "__main__":
    _mcp_server.main()
