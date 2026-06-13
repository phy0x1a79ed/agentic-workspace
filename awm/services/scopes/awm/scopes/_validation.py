"""Input validation for user-supplied names that become filesystem paths.

Reject project and scope names containing path separators, leading dots,
or other characters that could escape or nest the workspace layout. The
bug class this guards against: ``awm_refresh scope=metasmith/dev`` joining
the slashed scope literally onto the project dir and producing the orphan
``projects/metasmith/metasmith/dev/``.
"""

from __future__ import annotations


_FORBIDDEN_CHARS = ("/", "\\", "\x00")


def validate_name(name: str, kind: str = "name") -> str:
    """Return ``name`` if it's safe to join into a filesystem path; raise
    ``ValueError`` otherwise. ``kind`` is used only in the error message."""
    if not isinstance(name, str) or not name:
        raise ValueError(f"{kind} must be a non-empty string")
    if name in (".", ".."):
        raise ValueError(f"{kind} cannot be '.' or '..'")
    if name.startswith("."):
        raise ValueError(f"{kind} cannot start with '.' (got {name!r})")
    for ch in _FORBIDDEN_CHARS:
        if ch in name:
            raise ValueError(
                f"{kind} cannot contain {ch!r} (got {name!r})"
            )
    return name
