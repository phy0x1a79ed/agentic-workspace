"""Who may read and write which journals (fleet.md R107 item 3).

The bound follows the calling session's trust level, which the gateway passes as
``_caller_trust``. A call with no level is untrusted: the gateway stamps every call
it routes. A call over an edge comes from the stamped peer node. A
cooperative or untrusted session needs its own scope, ``_caller_scope``.
"""

from __future__ import annotations

from awm.config import edges
from awm.trustpolicy import JournalBound, journal_bound


class Refused(PermissionError):
    pass


def of(args: dict, as_: str | None) -> JournalBound:
    level = str(args.get("_caller_trust") or "untrusted")
    if level == "trusted":
        # Over an edge, the gateway already admitted the calling node (R107 item 4).
        return JournalBound(None, None, None)
    stamp = (as_ or "").strip()
    node = edges.label(stamp[len("peer:"):]) if stamp.lower().startswith("peer:") else edges.here()
    own = str(args.get("_caller_scope") or "")
    project, _, scope = own.partition("/")
    if level != "trusted" and not (project and scope):
        raise Refused(f"a {level} session reaches the journal only from its own scope, "
                      "and none came with this call")
    return journal_bound(level, node=node, project=project or None, scope=scope or None)


def read(bound: JournalBound, project: str | None, scope: str | None) -> None:
    if not bound.may_read(edges.here(), project, scope):
        raise Refused(f"this session may not read {project}/{scope} on {edges.here()}")


def write(bound: JournalBound, project: str | None, scope: str | None) -> None:
    read(bound, project, scope)
    if not bound.may_write(project, scope):
        raise Refused(f"this session writes only under {bound.write_scope}")


def narrow(bound: JournalBound, project: str | None, scope: str | None) -> tuple[str | None, str | None]:
    """The project and scope a fetch may cover: an untrusted session's own scope only."""
    if bound.read_nodes is not None and edges.here() not in bound.read_nodes:
        raise Refused(f"this session may not read the journals on {edges.here()}")
    if bound.read_scope is None:
        return project, scope
    own_project, _, own_scope = bound.read_scope.partition("/")
    if (project or own_project) != own_project or (scope or own_scope) != own_scope:
        raise Refused(f"this session reads only {bound.read_scope}")
    return own_project, own_scope
