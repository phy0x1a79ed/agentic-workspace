"""Registered MCP-config backends.

Add a new backend by creating a module in this directory that calls
``awm.exports.mcp.register(...)`` at import time, then add a single
``from . import <module>`` line below. The import is what activates it.
"""

from . import opencode  # noqa: F401
