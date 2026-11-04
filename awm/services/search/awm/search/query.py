"""A search request: the arguments of ``search query`` read once, with the caller's reach applied."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from awm import config
from awm.config import privacy
from awm.config.peertoken import node_label

from awm.search import store
from awm.search.index import Filters

DEFAULT_LIMIT = 10
MAX_LIMIT = 100
#: The links a ``hop`` walks. kb answers the first three over papers, and the archive walks ``reply_to``.
VIA = ("citations", "cited_by", "authors", "reply_to")
MAX_HOP_FROM = 100


@dataclass
class Hop:
    """A walk from the hits along one link type. Empty ``start`` starts from the hits for the words."""

    via: str
    start: list[str] = field(default_factory=list)
    limit: int = 20

    def forwarded(self) -> dict[str, Any]:
        return {"via": self.via, "limit": self.limit, **({"from": self.start} if self.start else {})}


@dataclass
class Query:
    text: str
    #: Source names on this node, in the order named.
    sources: list[str] = field(default_factory=list)
    #: Node → the source names asked of that node's search.
    remote: dict[str, list[str]] = field(default_factory=dict)
    project: str = ""
    scope: str = ""
    #: ``[(negated, tag), ...]``: every term must hold, as the journal's tag filter reads ``"a and !b"``.
    tags: list[tuple[bool, str]] = field(default_factory=list)
    after: str = ""
    before: str = ""
    limit: int = DEFAULT_LIMIT
    hop: Hop | None = None
    #: Another node made the call, so it answers from this node alone.
    relayed: bool = False
    #: The scope the call is made from, as project/scope; no order depends on it.
    context: str = ""
    hidden: Sequence[dict] | None = ()
    #: Sources named for another node on a relayed call, which this node does not answer for.
    elsewhere: list[str] = field(default_factory=list)
    raw: dict[str, str] = field(default_factory=dict)

    def filters(self, sources: list[str]) -> Filters:
        return Filters(sources, self.project, self.scope, self.tags, self.after, self.before, self.hidden)

    def tag_expr(self) -> str:
        return " and ".join(("!" if neg else "") + name for neg, name in self.tags)

    def forwarded(self, names: list[str]) -> dict[str, Any]:
        """The arguments a peer is asked with: the same question over its own sources, by their bare names."""
        args: dict[str, Any] = {"limit": self.limit, "sources": names, **self.raw}
        if self.text:
            args["query"] = self.text
        if self.tags:
            args["tags"] = self.tag_expr()
        if self.hop is not None:
            args["hop"] = self.hop.forwarded()
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


def hidden_for(as_: str | None) -> list[dict] | None:
    """The private marks a caller may not see past: none for a local caller, ``None`` when unreadable."""
    if not privacy.from_peer(as_):
        return []
    try:
        return privacy.marks()
    except privacy.MarksUnreadable:
        return None


def is_here(node: str) -> bool:
    return not node or node_label(node) == node_label(config.node_name())


def split_sources(value: Any) -> tuple[list[str], dict[str, list[str]]]:
    """``(this node's names, {node: names})`` from ``name`` and ``name@node`` entries; a bare name is here."""
    chosen = names(value)
    if not chosen:
        raise ValueError("search query needs `sources`: names such as journals or kb@altair, "
                         "from `search sources`. A bare name searches this node.")
    local: list[str] = []
    remote: dict[str, list[str]] = {}
    for entry in chosen:
        name, _, node = entry.partition("@")
        name, node = name.strip(), node.strip()
        if not name:
            raise ValueError(f"sources: {entry!r} names no source")
        if is_here(node):
            local += [name] if name not in local else []
        else:
            remote.setdefault(node, [])
            remote[node] += [name] if name not in remote[node] else []
    return local, remote


_TAG = re.compile(r"^[a-z0-9][a-z0-9_.:+/-]{0,63}$")


def parse_tags(expr: Any) -> list[tuple[bool, str]]:
    """``[(negated, name), ...]`` from ``"a and !b"`` or a list of such terms, as the journal reads its filter."""
    if expr in (None, "", []):
        return []
    parts = expr if isinstance(expr, (list, tuple)) else re.split(r"\s+and\s+", str(expr).strip(), flags=re.I)
    terms = []
    for raw in parts:
        term, negated = str(raw).strip(), False
        while True:
            if term.startswith("!"):
                negated, term = not negated, term[1:].strip()
            elif term.lower().startswith("not "):
                negated, term = not negated, term[4:].strip()
            else:
                break
        term = term.lower()
        if not _TAG.match(term):
            raise ValueError(f"cannot read the tag filter {expr!r}: join tags with 'and', "
                             "negate one with '!', such as '!closed and precedent'")
        terms.append((negated, term))
    return terms


def parse_hop(value: Any) -> Hop | None:
    """A :class:`Hop` from a JSON object or its text; ``None`` for an empty or missing one."""
    if value in (None, "", {}):
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise ValueError(f"hop: {exc}") from None
    if not isinstance(value, dict):
        raise ValueError('hop is a JSON object, such as {"via": "citations", "from": ["<id>"]}')
    unknown = set(value) - {"via", "from", "limit"}
    if unknown:
        raise ValueError(f"hop: unknown keys {sorted(unknown)}; it takes via, from and limit")
    via = str(value.get("via") or "").strip().lower()
    if via not in VIA:
        raise ValueError(f"hop: via {via!r} is not one of {', '.join(VIA)}")
    start = names(value.get("from")) or []
    if len(start) > MAX_HOP_FROM:
        raise ValueError(f"hop: from names at most {MAX_HOP_FROM} ids")
    limit = max(1, min(int(value.get("limit") or 20), MAX_LIMIT))
    return Hop(via, start, limit)


def parse(args: dict[str, Any], as_: str | None = None) -> Query:
    text = str(args.get("query") or "").strip()
    hop = parse_hop(args.get("hop"))
    if not text and not (hop and hop.start):
        raise ValueError("search query needs a `query`, or a `hop` with `from`")
    local, remote = split_sources(args.get("sources"))
    relayed = privacy.from_peer(as_)
    elsewhere: list[str] = []
    if relayed:  # a peer's call answers from this node alone, never asking another
        elsewhere = [f"{n}@{node}" for node, ns in remote.items() for n in ns]
        remote = {}
    raw = {k: str(args[k]) for k in ("project", "scope", "after", "before", "author") if args.get(k)}
    return Query(
        text=text, sources=local, remote=remote,
        project=str(args.get("project") or ""), scope=str(args.get("scope") or ""),
        tags=parse_tags(args.get("tags")), after=_time(args, "after"), before=_time(args, "before"),
        limit=max(1, min(int(args.get("limit") or DEFAULT_LIMIT), MAX_LIMIT)), hop=hop,
        relayed=relayed, hidden=hidden_for(as_), context=str(args.get("context") or "").strip(),
        elsewhere=elsewhere, raw=raw)
