"""A search request: the arguments of ``search query`` read once, with the caller's reach applied."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from awm import config
from awm.config.peertoken import node_label

from awm.search import store
from awm.search.index import Filters

DEFAULT_LIMIT = 10
MAX_LIMIT = 100
#: The ``peers`` word for every domestic node, this one included.
FLEET = "fleet"
#: The ``peers`` word for this node in a list of names.
LOCAL = "local"


@dataclass
class Query:
    text: str
    sources: list[str] | None = None
    project: str = ""
    scope: str = ""
    kind: str = ""
    after: str = ""
    before: str = ""
    limit: int = DEFAULT_LIMIT
    local: bool = True
    peer_names: list[str] | str = field(default_factory=list)
    grants: frozenset[str] | None = None
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def fan_out(self) -> bool:
        return bool(self.peer_names)

    def allows(self, source: str) -> bool:
        return self.grants is None or source in self.grants

    def filters(self, sources: list[str]) -> Filters:
        return Filters(sources, self.project, self.scope, self.kind, self.after, self.before)

    def forwarded(self) -> dict[str, Any]:
        """The arguments a peer is asked with: the same question, never a further fan-out."""
        args: dict[str, Any] = {"query": self.text, "limit": self.limit, "peers": False, **self.raw}
        if self.sources is not None:
            args["sources"] = self.sources
        return args


def names(value: Any) -> list[str] | None:
    """A list of names from a list or a comma-separated string; ``None`` when nothing was given."""
    if value is None or value == "":
        return None
    items = value.split(",") if isinstance(value, str) else value
    return [str(n).strip() for n in items if str(n).strip()]


def _time(args: dict[str, Any], key: str) -> str:
    if not args.get(key):
        return ""
    stamp = store.normalize_ts(args[key])
    if not stamp:
        raise ValueError(f"{key}: {args[key]!r} is not an ISO 8601 date or time")
    return stamp


def peer_originated(as_: str | None) -> bool:
    """Whether another node made the call; such a call searches this node only."""
    return as_ == "peer" or (as_ or "").startswith("peer:")


def _peers(value: Any) -> tuple[bool, list[str] | str]:
    """``(search this node?, the peers to ask)``. Nothing named is this node alone, ``fleet`` adds every
    domestic node, and a list of names asks only those, with ``local`` standing for this node."""
    if value is None or value is False:
        return True, []
    if value is True:
        return True, FLEET
    if isinstance(value, str) and value.strip().lower() in ("", "false", "0", "no", LOCAL):
        return True, []
    if isinstance(value, str) and value.strip().lower() in ("true", "1", "yes", FLEET):
        return True, FLEET
    chosen = names(value) or []
    here = node_label(config.node_name())
    remote = [n for n in chosen if n != LOCAL and node_label(n) != here]
    if FLEET in remote:
        return True, remote
    return len(remote) < len(chosen), remote


def parse(args: dict[str, Any], as_: str | None = None) -> Query:
    text = str(args.get("query") or "").strip()
    if not text:
        raise ValueError("search query needs a query")
    local, peer_names = _peers(args.get("peers"))
    if peer_originated(as_):
        local, peer_names = True, []
    grants = args.get("_grants")
    return Query(
        text=text, sources=names(args.get("sources")),
        project=str(args.get("project") or ""), scope=str(args.get("scope") or ""),
        kind=str(args.get("kind") or ""), after=_time(args, "after"), before=_time(args, "before"),
        limit=max(1, min(int(args.get("limit") or DEFAULT_LIMIT), MAX_LIMIT)),
        local=local, peer_names=peer_names,
        grants=None if grants is None else frozenset(str(g) for g in grants),
        raw={k: str(args[k]) for k in ("project", "scope", "kind", "after", "before") if args.get(k)})
