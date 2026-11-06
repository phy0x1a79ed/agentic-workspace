"""A search request: the arguments of ``search query`` read once, validated, with the caller's reach applied."""

from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass, field
from typing import Any, Sequence, Union

from awm import config
from awm.config import edges
from awm.config import privacy

from awm.search import registry, store
from awm.search.registry import Field

DEFAULT_MAX = 10
MAX_MAX = 100

LOGIC = ("$and", "$or", "$nor")
SCALAR_OPS = ("$eq", "$ne", "$gt", "$gte", "$lt", "$lte")
LIST_OPS = ("$in", "$nin", "$all")
_NODE = re.compile(r"^[A-Za-z0-9_.-]+$")
_SORT = re.compile(r"^([+-]?)([a-z][a-z0-9_]*)$")


@functools.cache
def kinds() -> dict[str, dict[str, Field]]:
    """``{kind: {name: Field}}`` for every kind a manifest on disk declares fields for."""
    return {k: fields for k, (fields, _) in registry.declared().items() if fields}


@functools.cache
def hit_fields(kind: str) -> tuple[str, ...]:
    """The fields a hit of ``kind`` shows beside its id; ``title`` and ``ts`` for an undeclared kind."""
    declared = registry.declared().get(kind)
    return declared[1] if declared and declared[1] else ("title", "ts")


class QueryError(ValueError):
    """A malformed request: ``<json path>: <problem>; allowed: <values>``."""


def _fail(path: str, problem: str, allowed: Sequence[str] | str = "") -> QueryError:
    allowed = ", ".join(allowed) if not isinstance(allowed, str) else allowed
    return QueryError(f"{path}: {problem}" + (f"; allowed: {allowed}" if allowed else ""))


@dataclass(frozen=True)
class Lit:
    text: str


@dataclass(frozen=True)
class Op:
    op: str
    items: tuple["Node", ...]


Node = Union[Lit, Op]


@dataclass
class Text:
    about: str | None
    under_or: bool
    contains: Node | None
    raw: Any


@dataclass
class Source:
    name: str
    kind: str
    node: str
    filter: dict
    #: ``(field, descending)`` from ``$sort``, or ``None``.
    sort: tuple[str, bool] | None = None

    def asked(self) -> dict:
        """The filter as a caller writes it, ``$sort`` included."""
        if self.sort is None:
            return self.filter
        return {**self.filter, "$sort": ("-" if self.sort[1] else "+") + self.sort[0]}


@dataclass
class Request:
    text: Text | None
    sources: list[Source]
    remote: dict[str, list[Source]]
    max: int
    relayed: bool
    hidden: Sequence[dict] | None
    elsewhere: list[str] = field(default_factory=list)

    def forwarded(self, node: str) -> dict[str, Any]:
        """The arguments a peer is asked with: the same question over its own sources, by bare kind."""
        body: dict[str, Any] = {}
        if self.text is not None:
            body["text"] = self.text.raw
        for source in self.remote.get(node, []):
            body[source.kind] = source.asked()
        return {"query": body, "max": self.max}


def hidden_for(as_: str | None) -> list[dict] | None:
    """The private marks a caller may not see past: none for a local caller, ``None`` when unreadable."""
    if not privacy.from_peer(as_):
        return []
    try:
        return privacy.marks()
    except privacy.MarksUnreadable:
        return None


def is_here(node: str) -> bool:
    return not node or edges.label(node) == edges.label(config.node_name())


class _Dup(dict):
    dups: list[str]


def _pairs(items: list[tuple[str, Any]]) -> dict:
    d = _Dup(items)
    d.dups = sorted({k for k, _ in items if sum(1 for q, _ in items if q == k) > 1})
    return d


def _no_dups(value: Any, path: str) -> None:
    if isinstance(value, dict):
        for key in getattr(value, "dups", []):
            raise _fail(f"{path}.{key}", "duplicate key")
        for key, sub in value.items():
            _no_dups(sub, f"{path}.{key}")
    elif isinstance(value, list):
        for i, sub in enumerate(value):
            _no_dups(sub, f"{path}[{i}]")


def _max(value: Any) -> int:
    if isinstance(value, str) and re.fullmatch(r"\s*\d+\s*", value):
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_MAX:
        raise _fail("max", f"{value!r} is not an integer", f"1-{MAX_MAX}")
    return value


def _contains_tree(node: Any, path: str) -> Node:
    if not isinstance(node, dict) or len(node) != 1:
        raise _fail(path, "a text term is an object with exactly one key", ("$contains", *LOGIC))
    (key, value), = node.items()
    if key == "$contains":
        if not isinstance(value, str) or not re.search(r"[^\W_]", value):
            raise _fail(f"{path}.$contains", "needs a string with a letter or digit")
        return Lit(value)
    if key in LOGIC:
        if not isinstance(value, list) or not value:
            raise _fail(f"{path}.{key}", "needs a non-empty list")
        return Op(key[1:], tuple(_contains_tree(sub, f"{path}.{key}[{i}]") for i, sub in enumerate(value)))
    if key == "$about":
        raise _fail(path, "$about sits only directly under the top $and or $or")
    raise _fail(f"{path}.{key}", "not allowed in text", ("$contains", *LOGIC))


def _about_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise _fail(path, "$about needs a non-empty string")
    return value


def _text(value: Any) -> Text:
    path = "query.text"
    if isinstance(value, str):
        if not value:
            raise _fail(path, "text is empty")
        return Text(value, True, Lit(value), value)
    if not isinstance(value, dict) or len(value) != 1:
        raise _fail(path, "text is a string or an object with exactly one key", ("$contains", "$about", *LOGIC))
    (key, body), = value.items()
    if key == "$about":
        return Text(_about_string(body, f"{path}.$about"), False, None, value)
    if key in ("$and", "$or"):
        if not isinstance(body, list) or not body:
            raise _fail(f"{path}.{key}", "needs a non-empty list")
        about: str | None = None
        rest: list[Node] = []
        for i, item in enumerate(body):
            at = f"{path}.{key}[{i}]"
            if isinstance(item, dict) and list(item) == ["$about"]:
                if about is not None:
                    raise _fail(f"{path}.{key}", "more than one $about")
                about = _about_string(item["$about"], f"{at}.$about")
            else:
                rest.append(_contains_tree(item, at))
        contains = None if not rest else rest[0] if len(rest) == 1 else Op(key[1:], tuple(rest))
        return Text(about, key == "$or" and about is not None and contains is not None, contains, value)
    return Text(None, False, _contains_tree(value, path), value)


def _scalar(value: Any, f: Field, path: str) -> Any:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise _fail(path, f"{type(value).__name__} is not a scalar", "string, number")
    if f.cls == "datetime":
        stamp = store.normalize_ts(value) if isinstance(value, str) else ""
        if not stamp:
            raise _fail(path, f"{value!r} is not an ISO 8601 date or time", "2026-03-01, 2026-03-01T12:00:00Z")
        return stamp
    if f.cls == "numeric" and not isinstance(value, (int, float)):
        raise _fail(path, f"{value!r} is not a number", "a number")
    return value if f.cls == "numeric" else str(value)


def _field_value(value: Any, name: Field, path: str) -> Any:
    if isinstance(value, list):
        raise _fail(path, "a bare list is not a value", "a scalar, or an object with $in, $nin or $all")
    if not isinstance(value, dict):
        return _scalar(value, name, path)
    if not value:
        raise _fail(path, "empty operator object", (*SCALAR_OPS, *LIST_OPS))
    out: dict[str, Any] = {}
    for op, operand in value.items():
        at = f"{path}.{op}"
        if op in SCALAR_OPS:
            out[op] = _scalar(operand, name, at)
        elif op in LIST_OPS:
            if not isinstance(operand, list) or not operand:
                raise _fail(at, "needs a non-empty list")
            out[op] = [_scalar(x, name, f"{at}[{i}]") for i, x in enumerate(operand)]
        else:
            raise _fail(at, "unknown operator", (*SCALAR_OPS, *LIST_OPS))
    return out


def _filter(f: Any, fields: dict[str, Field], kind: str, path: str) -> dict:
    if not isinstance(f, dict):
        raise _fail(path, "a source filter is an object")
    filterable = tuple(n for n, x in fields.items() if x.cls != "content")
    out: dict[str, Any] = {}
    for key, value in f.items():
        at = f"{path}.{key}"
        if key in LOGIC:
            if not isinstance(value, list) or not value:
                raise _fail(at, "needs a non-empty list")
            out[key] = [_filter(sub, fields, kind, f"{at}[{i}]") for i, sub in enumerate(value)]
        elif key in ("$about", "$contains"):
            raise _fail(at, f"{key} belongs in text, not in a source filter")
        elif key == "$sort":
            raise _fail(at, "$sort sits only at the top of a source filter")
        elif key.startswith("$"):
            raise _fail(at, "operator not allowed at a field position", (*LOGIC, "$sort", *filterable))
        elif key not in fields:
            raise _fail(at, f"{kind} has no field {key!r}", filterable)
        elif fields[key].cls == "content":
            raise _fail(at, f"{key} is content: search it with text", filterable)
        else:
            out[key] = _field_value(value, fields[key], at)
    return out


def _sort(value: Any, fields: dict[str, Field], kind: str, path: str) -> tuple[str, bool]:
    m = _SORT.match(value) if isinstance(value, str) else None
    if m is None or m.group(2) not in fields:
        raise _fail(path, f"{value!r} is not one field of {kind} with + or -",
                    tuple(f"-{n}" for n in fields))
    return m.group(2), m.group(1) == "-"


def _source(key: str, value: Any) -> Source:
    path = f"query.{key}"
    kind, at, node = key.partition("@")
    known = kinds()
    if kind not in known:
        raise _fail(path, "not a source", sorted(known))
    if at and not _NODE.match(node):
        raise _fail(path, "malformed node after @", "kind@node, such as journals@altair")
    fields = known[kind]
    sort = None
    if isinstance(value, dict) and "$sort" in value:
        sort = _sort(value["$sort"], fields, kind, f"{path}.$sort")
        value = {k: v for k, v in value.items() if k != "$sort"}
    return Source(key, kind, "" if is_here(node) else node, _filter(value, fields, kind, path), sort)


def _query(query: Any) -> tuple[Text | None, list[Source]]:
    if isinstance(query, str) and query.lstrip().startswith("{"):
        try:
            query = json.loads(query, object_pairs_hook=_pairs)
        except ValueError as exc:
            raise _fail("query", f"not valid JSON ({exc})") from None
        _no_dups(query, "query")
    if isinstance(query, str):
        if not query.strip():
            raise _fail("query", "the query is empty")
        return _text(query), [_source("journals", {})]
    if not isinstance(query, dict) or not query:
        raise _fail("query", "a query is a string or a non-empty object", ("text", *kinds()))
    text = _text(query["text"]) if "text" in query else None
    sources = [_source(k, v) for k, v in query.items() if k != "text"]
    if not sources:
        sources = [_source("journals", {})]
    return text, sources


def parse(args: dict[str, Any], as_: str | None = None) -> Request:
    """The validated :class:`Request` for ``search query`` arguments, as the caller ``as_`` may see it."""
    for key in args:
        if key not in ("query", "max") and not key.startswith("_caller_"):
            raise _fail(key, "unknown argument", ("query", "max"))
    if "query" not in args:
        raise _fail("query", "missing", "a string, or an object of text and sources")
    maximum = _max(args["max"]) if args.get("max") is not None else DEFAULT_MAX
    text, sources = _query(args["query"])

    relayed = privacy.from_peer(as_)
    here: list[Source] = []
    remote: dict[str, list[Source]] = {}
    elsewhere: list[str] = []
    seen: set[tuple[str, str]] = set()
    for source in sources:
        ident = (source.kind, edges.label(source.node))
        if ident in seen:
            raise _fail(f"query.{source.name}", "this source is named twice")
        seen.add(ident)
        if not source.node:
            here.append(source)
        elif relayed:
            elsewhere.append(source.name)
        else:
            remote.setdefault(edges.label(source.node), []).append(source)
    return Request(text, here, remote, maximum, relayed, hidden_for(as_), elsewhere)
