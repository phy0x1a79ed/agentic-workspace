"""The ``more`` call-through tool: the one place its rewrite is defined.

The agent-facing MCP surface advertises only the core domains. Every other
(discoverable) domain is reached through ``more(domain, verb, args, peer)``. Both
MCP proxies rewrite such a call into the ordinary call for the real domain
before anything else happens, so the gateway, its caller stamping and its gates
see the true domain and verb, and the result, the peer redirect and the error for
an unknown domain are those of a direct call.

Dependency-free on purpose: the stdio proxy imports this on its startup path.
"""

from __future__ import annotations

from typing import Any

#: The reserved top-level tool name. Not a domain.
MORE_TOOL = "more"

_PASSTHROUGH_KEYS = ("verb", "args", "peer")


def rewrite_call(name: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Map a ``more`` call to the direct call it stands for.

    ``more(domain=D, verb=V, args=A, peer=P)`` becomes ``(D, {verb, args, peer})``,
    carrying only the keys that were given. A call with no usable ``domain`` is
    returned unchanged so the gateway answers it with the domain listing. Any
    other tool name passes through untouched.
    """
    if name != MORE_TOOL:
        return name, arguments
    domain = arguments.get("domain")
    if not isinstance(domain, str) or not domain.strip():
        return name, arguments
    forwarded = {k: arguments[k] for k in _PASSTHROUGH_KEYS if k in arguments}
    return domain.strip(), forwarded
