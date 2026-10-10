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

import re
from typing import Any

#: The reserved top-level tool name. Not a domain.
MORE_TOOL = "more"

_PASSTHROUGH_KEYS = ("verb", "args", "peer")

_PLAIN_DOMAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def is_plain_domain(domain: Any) -> bool:
    """Whether ``domain`` is a bare domain name: no ``@``, ``/`` or whitespace.

    ``<domain>@<peer>`` is the legacy shim both proxies honour by dialling the
    peer edge directly. A rewritten ``more`` call must never reach it, so peer
    routing stays in the ``peer`` argument."""
    return isinstance(domain, str) and _PLAIN_DOMAIN.fullmatch(domain) is not None


def rewrite_call(name: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Map a ``more`` call to the direct call it stands for.

    ``more(domain=D, verb=V, args=A, peer=P)`` becomes ``(D, {verb, args, peer})``,
    carrying only the keys that were given. A call with no ``domain``, or one that
    is not a plain domain name, is returned unchanged: the gateway answers the
    first with the domain listing and the second with the unknown-domain error.
    Any other tool name passes through untouched.
    """
    if name != MORE_TOOL:
        return name, arguments
    domain = arguments.get("domain")
    if not is_plain_domain(domain):
        return name, arguments
    forwarded = {k: arguments[k] for k in _PASSTHROUGH_KEYS if k in arguments}
    return domain, forwarded
