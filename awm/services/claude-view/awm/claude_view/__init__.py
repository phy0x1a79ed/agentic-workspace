"""awm claude-view service — supervises the upstream claude-view server.

PEP 420 namespace package (no ``awm/__init__.py``); the namespace dir is
``awm/claude_view/``. The service *folder* is ``claude-view`` with a hyphen
because awm service names cannot contain ``_`` (the CLI/MCP surface splits a
projected tool name on the first underscore), while the Python package must
use one — the same split ``rlm-browser`` → ``awm/rlm_browser`` uses.
"""
