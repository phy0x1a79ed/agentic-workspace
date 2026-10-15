"""The one-service-one-tool registration rule.

A service publishes under the ``tool`` its ``service.toml`` names (the folder
name when absent), and every function it registers is published as
``<tool>_<verb>``. The published name is the manifest entry's ``tool`` key, else
``<service>_<name>``; ``name`` itself stays the service-internal handle and is
never checked. Two services may share a tool, but not a verb within it.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from awm.gateway.hub import discovery

#: Services exempt from the ``<tool>_<verb>`` rule while a branch converts
#: them. Their tool is the prefix of each published name.
LEGACY_ALLOWLIST: frozenset[str] = frozenset()

#: Identity RPCs whose published names are frozen by IDENTITY_CONTRACT.md.
FROZEN_RPCS: frozenset[str] = frozenset({
    "resolveScope", "resolveRef", "project_ensure", "scope_ensure",
    "scope_resolve", "ref_resolve",
})

_TOOL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")


class RegistrationError(ValueError):
    """A service's manifest breaks the registration rule."""


def published_name(service: str, fn: dict) -> str:
    """The name a manifest function is published under on every surface."""
    return fn.get("tool") or f"{service}_{fn['name']}"


def is_legacy(service: str) -> bool:
    return service in LEGACY_ALLOWLIST


def split_published(service: str, published: str) -> tuple[str, str]:
    """``(tool, verb)`` of a published name. A legacy service takes the text
    before the first underscore as its tool; the verb may be empty."""
    if not is_legacy(service):
        tool = discovery.service_tool(service)
        if published.startswith(tool + "_"):
            return tool, published[len(tool) + 1:]
    tool, _, verb = published.partition("_")
    return tool, verb


def tool_violations(service: str, tool: str,
                    functions: Iterable[dict]) -> list[str]:
    """One message per function whose published name is not ``<tool>_<verb>``."""
    bad: list[str] = []
    if not _TOOL_NAME.fullmatch(tool):
        bad.append(f"tool {tool!r} must be letters, digits and hyphens only")
    for fn in functions:
        if not (isinstance(fn, dict) and fn.get("name")):
            continue
        published = published_name(service, fn)
        if published in FROZEN_RPCS:
            continue
        if not (published.startswith(tool + "_") and len(published) > len(tool) + 1):
            bad.append(f"{published!r} (function {fn['name']!r}) is not named "
                       f"'{tool}_<verb>'")
    return bad


def check_registration(service: str, api: dict[str, Any],
                       others: Iterable[tuple[str, dict[str, Any]]]) -> None:
    """Raise :class:`RegistrationError` naming every offending function when
    ``service`` breaks the rule. ``others`` is ``(service, api)`` for each other
    registered service, to catch a verb two services both claim in one tool."""
    if is_legacy(service):
        return
    tool = discovery.service_tool(service)
    functions = [f for f in (api or {}).get("functions", []) or []
                 if isinstance(f, dict) and f.get("name")]
    problems = tool_violations(service, tool, functions)
    taken: dict[str, str] = {}
    for other, other_api in others:
        if other == service or is_legacy(other):
            continue
        if discovery.service_tool(other) != tool:
            continue
        for fn in (other_api or {}).get("functions", []) or []:
            if isinstance(fn, dict) and fn.get("name"):
                taken[published_name(other, fn)] = other
    seen: set[str] = set()
    for fn in functions:
        published = published_name(service, fn)
        if published in seen:
            problems.append(f"{published!r} is registered twice by {service!r}")
        elif published in taken:
            problems.append(
                f"{published!r} is already held by service {taken[published]!r} "
                f"in tool {tool!r}")
        seen.add(published)
    if problems:
        raise RegistrationError(
            f"service {service!r} (tool {tool!r}) refused: " + "; ".join(problems))
