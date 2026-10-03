"""Input validation for user-supplied names that become filesystem paths.

The guarantee is that a validated name, joined onto the workspace layout,
cannot escape it: no traversal, no absolute path, no hidden entry, no NUL.

Scope names may nest (``allow_nesting=True``): ``fabfos/dev`` is a scope whose
worktree is ``projects/metasmith/fabfos/dev``, which is how a workspace holding
four pipelines stays navigable. Each segment is validated as if it were a flat
name, so the traversal guarantee is unchanged — what nesting gives up is the
*shape* check: ``scope=metasmith/dev`` under project ``metasmith`` is now a
legitimate (if redundant) name rather than the double-join bug it used to be.

Project names never nest. A slashed project name would put a second bare
repository one level down, which is the opposite of the shared-``.bare`` design.
"""

from __future__ import annotations


_FORBIDDEN_CHARS = ("/", "\\", "\x00")

# Within a segment of a nested name, "/" is the separator rather than a
# forbidden character; everything else is still forbidden.
_FORBIDDEN_SEGMENT_CHARS = ("\\", "\x00")


def _validate_segment(segment: str, kind: str, whole: str) -> None:
    shown = whole if whole != segment else segment
    if not segment:
        raise ValueError(f"{kind} cannot contain an empty segment (got {shown!r})")
    if segment in (".", ".."):
        raise ValueError(f"{kind} cannot contain '.' or '..' (got {shown!r})")
    if segment.startswith("."):
        raise ValueError(
            f"{kind} cannot have a segment starting with '.' (got {shown!r})"
        )
    for ch in _FORBIDDEN_SEGMENT_CHARS:
        if ch in segment:
            raise ValueError(f"{kind} cannot contain {ch!r} (got {shown!r})")


def validate_name(name: str, kind: str = "name", *, allow_nesting: bool = False) -> str:
    """Return ``name`` if it's safe to join into a filesystem path; raise
    ``ValueError`` otherwise. ``kind`` is used only in the error message.

    With ``allow_nesting`` the name may contain ``/`` as a separator, but not
    lead or trail with one and not hold an empty, ``.``, ``..`` or dot-leading
    segment.
    """
    if not isinstance(name, str) or not name:
        raise ValueError(f"{kind} must be a non-empty string")

    if not allow_nesting:
        if name in (".", ".."):
            raise ValueError(f"{kind} cannot be '.' or '..'")
        if name.startswith("."):
            raise ValueError(f"{kind} cannot start with '.' (got {name!r})")
        for ch in _FORBIDDEN_CHARS:
            if ch in name:
                raise ValueError(f"{kind} cannot contain {ch!r} (got {name!r})")
        return name

    if name.startswith("/") or name.endswith("/"):
        raise ValueError(
            f"{kind} cannot start or end with '/' (got {name!r})"
        )
    for segment in name.split("/"):
        _validate_segment(segment, kind, name)
    return name


def validate_scope_name(scope: str, kind: str = "scope name") -> str:
    """``validate_name`` with nesting enabled — the form every scope-name call
    site wants, named so the intent is visible at the call site."""
    return validate_name(scope, kind=kind, allow_nesting=True)
