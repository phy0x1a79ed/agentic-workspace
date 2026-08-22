"""Entry point for the ``awm-mcp`` stdio proxy — picks the implementation.

Two proxies exist and both speak the same MCP surface to the same core:

- :mod:`awm.gateway.mcp_stdio` (default) — hand-rolled JSON-RPC over
  :mod:`urllib`, no third-party imports on the startup path.
- :mod:`awm.gateway.mcp_server_sdk` — the original, built on the ``mcp`` SDK.

The default is the fast one because an MCP client cannot use *any* tool until
the server answers ``initialize``, so framework import time is dead time added
to every session launch: measured on altair, 0.96s to first ``tools/list``
through the SDK against 0.15s here, for a request the core answers in ~19ms.

This module stays a dispatcher rather than becoming the implementation so that
the console script generated at install time — ``awm.gateway.mcp_server:main``
— never has to change. Switching implementations is then a deploy, not a
reinstall on every node, and ``AWM_MCP_SDK=1`` in the environment (or in the
client's server ``env`` block) falls back to the SDK proxy without one either.
Keep that escape hatch: it is the only rollback that does not need a redeploy.
"""

from __future__ import annotations

import os


def main() -> None:
    if os.environ.get("AWM_MCP_SDK") == "1":
        from awm.gateway.mcp_server_sdk import main as _impl
    else:
        from awm.gateway.mcp_stdio import main as _impl
    _impl()


if __name__ == "__main__":
    main()
