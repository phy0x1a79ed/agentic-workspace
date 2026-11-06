"""Name helpers shared by the journal modules. ``SYSTEM_REF`` matches the scopes service's own."""

from __future__ import annotations

SYSTEM_REF = "system"


def validate_project(name: str) -> str:
    """``name`` if it is safe to join into a path under ``projects/``."""
    if not isinstance(name, str) or not name:
        raise ValueError("project name must be a non-empty string")
    if name in (".", "..") or name.startswith("."):
        raise ValueError(f"project name cannot start with '.' (got {name!r})")
    for ch in ("/", "\\", "\x00"):
        if ch in name:
            raise ValueError(f"project name cannot contain {ch!r} (got {name!r})")
    return name


def validate_scope(scope: str) -> str:
    """``scope`` if every ``/``-separated segment is safe to join into a path."""
    if not isinstance(scope, str) or not scope:
        raise ValueError("scope name must be a non-empty string")
    if scope.startswith("/") or scope.endswith("/"):
        raise ValueError(f"scope name cannot start or end with '/' (got {scope!r})")
    for segment in scope.split("/"):
        if not segment or segment.startswith("."):
            raise ValueError(f"scope name has an empty or dot-leading segment (got {scope!r})")
        for ch in ("\\", "\x00"):
            if ch in segment:
                raise ValueError(f"scope name cannot contain {ch!r} (got {scope!r})")
    return scope
