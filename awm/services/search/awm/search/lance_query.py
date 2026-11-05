"""One index source's answer from its LanceDB tables.

A validated source filter compiles to a Lance SQL prefilter that both tables share, because
the chunk table carries every non-content field. The ``$contains`` tree compiles to a full-text
query of phrases. A tree that only excludes cannot run as a full-text query on its own, so it
compiles to its positive opposite and the caller keeps the entries it does not match.
``$about`` ranks by a weighted sum of a vector search over chunks and any-word BM25 over entries,
both under the same prefilter, each min-max normalised over its own top hits, as the released engine
ranked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator, Sequence

from lancedb.query import BooleanQuery, MultiMatchQuery, Occur, PhraseQuery

from awm.persistence import embeddings as engine

from awm.search.lance_store import Tables, id_list, quote
from awm.search.registry import Field

#: Probes and refine factor of a vector search over an IVF index.
NPROBES = 20
REFINE = 0
#: How deep each ranked list reaches before fusion.
FUSION_DEPTH = engine.FUSION_DEPTH
#: How many phrase matches an ``$about`` ranks, or a sort orders, at most.
CANDIDATES = 1000
BATCH = 500
_SEP = r"\x1f"
_META = re.compile(r"([\\.+*?()|\[\]{}^$])")
_WORD = re.compile(r"[^\W_]+")
_COMPARE = {"$gt": ">", "$gte": ">=", "$lt": "<", "$lte": "<="}


class Unserved(ValueError):
    """A query this source's store cannot answer, with the reason."""


# -- filter ---------------------------------------------------------------


def _regex_value(value: str) -> str:
    return "[^" + _SEP + "]*" if value == "*" else _META.sub(r"\\\1", value)


def _pattern(value: str, f: Field) -> str:
    """A regex over a categorical's values joined by U+001F: ``value`` with ``*`` as a glob, and with the
    values nested below it when the field nests."""
    body = "".join(_regex_value(part) for part in re.split(r"(\*)", value) if part)
    below = ""
    if f.nested and not value.endswith("*"):
        below = "(" + _regex_value(f.nested) + "[^" + _SEP + "]*)?"
    return "(^|" + _SEP + ")" + body + below + "(" + _SEP + "|$)"


def _joined(name: str) -> str:
    return f"array_to_string({name}, chr(31))"


def _equals(name: str, f: Field, value: Any) -> str:
    if f.cls == "categorical":
        text = str(value)
        if "*" not in text and not f.nested:
            return f"array_has({name}, {quote(text)})"
        return f"regexp_like({_joined(name)}, {quote(_pattern(text, f))})"
    return f"{name} = {_literal(f, value)}"


def _literal(f: Field, value: Any) -> str:
    if f.cls == "datetime":
        return f"timestamp {quote(str(value))}"
    if f.cls == "numeric":
        return repr(float(value))
    return quote(str(value))


def _join(op: str, parts: list[str], empty: str) -> str:
    if not parts:
        return empty
    return parts[0] if len(parts) == 1 else "(" + f" {op} ".join(parts) + ")"


def _not(name: str, f: Field, expr: str) -> str:
    """``NOT expr`` that also holds for an entry without the field, as MongoDB's ``$ne`` does."""
    return f"NOT ({expr})" if f.cls == "categorical" else f"({name} IS NULL OR NOT ({expr}))"


def _field(name: str, f: Field, spec: Any) -> str:
    ops = spec if isinstance(spec, dict) else {"$eq": spec}
    parts = []
    for op, value in ops.items():
        if op == "$eq":
            parts.append(_equals(name, f, value))
        elif op == "$ne":
            parts.append(_not(name, f, _equals(name, f, value)))
        elif op == "$in":
            parts.append(_join("OR", [_equals(name, f, v) for v in value], "false"))
        elif op == "$nin":
            parts.append(_not(name, f, _join("OR", [_equals(name, f, v) for v in value], "false")))
        elif op == "$all":
            parts.append(_join("AND", [_equals(name, f, v) for v in value], "true"))
        elif op in _COMPARE:
            if f.cls == "categorical":
                raise Unserved(f"{op} on the categorical field {name!r}; use $eq, $ne, $in, $nin or $all")
            parts.append(f"{name} {_COMPARE[op]} {_literal(f, value)}")
        else:
            raise Unserved(f"unknown operator {op!r} on {name!r}")
    return _join("AND", parts, "true")


def filter_sql(fields: dict[str, Field], f: dict) -> str:
    """A Lance SQL condition for the validated filter ``f``; ``{}`` gives ``true``."""
    parts = []
    for key, value in f.items():
        if key == "$and":
            parts.append(_join("AND", [filter_sql(fields, x) for x in value], "true"))
        elif key == "$or":
            parts.append(_join("OR", [filter_sql(fields, x) for x in value], "false"))
        elif key == "$nor":
            parts.append(f"NOT {_join('OR', [filter_sql(fields, x) for x in value], 'false')}")
        else:
            parts.append(_field(key, fields[key], value))
    return _join("AND", parts, "true")


def veil_sql(fields: dict[str, Field], hidden: Sequence[dict] | None) -> str:
    """A condition that keeps only entries no private mark covers; ``hidden=None`` keeps only unmarked ones.

    A mark covers an entry by its project and scope, and by a tag equal to the marked project's name,
    ignoring case. A source without those fields has nothing a mark can cover.
    """
    has = {n for n in ("project", "scope", "tag") if fields.get(n) and fields[n].cls == "categorical"}
    if hidden is None:
        return _join("AND", [f"cardinality({n}) = 0" for n in sorted(has)], "true")
    clauses = []
    for mark in hidden:
        if "project" not in has:
            break
        covered = f"array_has(project, {quote(mark['project'])})"
        if mark["scope"] is not None and "scope" in has:
            covered += " AND " + _equals("scope", Field("scope", "categorical", "/"), mark["scope"])
        clauses.append(f"({covered})")
    if "tag" in has:
        for project in sorted({m["project"] for m in hidden}):
            clauses.append(f"regexp_like({_joined('tag')}, {quote('(?i)' + _pattern(project, fields['tag']))})")
    return f"NOT {_join('OR', clauses, 'false')}" if clauses else "true"


def where(fields: dict[str, Field], f: dict, hidden: Sequence[dict] | None) -> str:
    return _join("AND", [p for p in (filter_sql(fields, f), veil_sql(fields, hidden)) if p != "true"], "true")


# -- text -----------------------------------------------------------------


def words(text: str) -> list[str]:
    """``text`` as the full-text index tokenizes it: lowercase runs of letters and digits."""
    return [w.lower() for w in _WORD.findall(text)]


@dataclass(frozen=True)
class Tree:
    """A compiled ``$contains`` tree: matches ``query``, or with ``negated`` everything ``query`` does not."""

    query: Any
    negated: bool


def _should(queries: list[Any]) -> Any:
    return queries[0] if len(queries) == 1 else BooleanQuery([(Occur.SHOULD, q) for q in queries])


def compile_tree(node: Any, content: Sequence[str]) -> Tree:
    """The full-text query for a ``$contains`` tree over the ``content`` columns."""
    if hasattr(node, "text"):
        phrase = " ".join(words(node.text))
        return Tree(_should([PhraseQuery(phrase, c) for c in content]), False)
    items = [compile_tree(x, content) for x in node.items]
    pos = [t.query for t in items if not t.negated]
    neg = [t.query for t in items if t.negated]
    if node.op == "nor":
        if neg:  # NOT (p OR NOT n) = n AND NOT p
            must = [(Occur.MUST, q) for q in neg] + [(Occur.MUST_NOT, q) for q in pos]
            return Tree(BooleanQuery(must), False)
        return Tree(_should(pos), True)
    if node.op == "and":
        if pos:
            return Tree(BooleanQuery([(Occur.MUST, q) for q in pos] + [(Occur.MUST_NOT, q) for q in neg]), False)
        return Tree(_should(neg), True)  # NOT a AND NOT b = NOT (a OR b)
    if not neg:
        return Tree(_should(pos), False)
    # p OR NOT n = NOT (n AND NOT p)
    inner = [(Occur.MUST, q) for q in neg] if len(neg) == 1 else [(Occur.MUST, BooleanQuery(
        [(Occur.MUST, q) for q in neg]))]
    return Tree(BooleanQuery(inner + ([(Occur.MUST_NOT, _should(pos))] if pos else [])), True)


# -- reads ----------------------------------------------------------------


def _and(*parts: str) -> str:
    return _join("AND", [p for p in parts if p and p != "true"], "true")


def _fts_rows(tables: Tables, query: Any, cond: str, limit: int | None, columns: Sequence[str] = ()
              ) -> list[dict[str, Any]]:
    b = tables.entries.search(query, query_type="fts").select(["id", *columns]).limit(limit)
    if cond != "true":
        b = b.where(cond, prefilter=True)
    return b.to_arrow().to_pylist()


def _fts(tables: Tables, query: Any, cond: str, limit: int | None) -> list[tuple[str, float]]:
    return [(r["id"], r["_score"]) for r in _fts_rows(tables, query, cond, limit)]


def matching(tables: Tables, tree: Tree, ids: Sequence[str]) -> set[str]:
    """The ``ids`` that the tree matches."""
    found: set[str] = set()
    for i in range(0, len(ids), BATCH):
        batch = list(ids[i:i + BATCH])
        hit = {k for k, _ in _fts(tables, tree.query, id_list(batch), len(batch))}
        found |= set(batch) - hit if tree.negated else hit
    return found


def _ids(tables: Tables, cond: str, limit: int | None) -> list[str]:
    b = tables.entries.search().select(["id"]).limit(limit)
    if cond != "true":
        b = b.where(cond)
    return b.to_arrow()["id"].to_pylist()


def _by_field(tables: Tables, rows: list[dict[str, Any]], field: str, descending: bool) -> list[str]:
    """The ids of ``rows`` ordered by ``field``; a row without it goes last."""
    key = (lambda v: v[0] if v else None) if tables.fields[field].cls == "categorical" else (lambda v: v)
    present = [r for r in rows if key(r[field]) is not None]
    present.sort(key=lambda r: key(r[field]), reverse=descending)
    return [r["id"] for r in present] + [r["id"] for r in rows if key(r[field]) is None]


def _sorted_ids(tables: Tables, cond: str, field: str, descending: bool, within: Sequence[str] | None = None
                ) -> list[str]:
    """Every id passing ``cond`` (and in ``within``), ordered by ``field``."""
    b = tables.entries.search().select(["id", field]).limit(None)
    cond = _and(cond, id_list(within) if within is not None else "")
    if cond != "true":
        b = b.where(cond)
    return _by_field(tables, b.to_arrow().to_pylist(), field, descending)


def _first(tables: Tables, tree: Tree | None, cond: str, order: Iterator[str] | None, limit: int) -> list[str]:
    """The first ``limit`` ids that pass ``cond`` and the tree, in ``order`` when given, else as stored."""
    if tree is not None and not tree.negated and order is None:
        return [k for k, _ in _fts(tables, tree.query, cond, limit)]
    if tree is None and order is None:
        return _ids(tables, cond, limit)
    source = order if order is not None else iter(_ids(tables, cond, None))
    out: list[str] = []
    while len(out) < limit:
        batch = [i for _, i in zip(range(BATCH), source)]
        if not batch:
            break
        kept = matching(tables, tree, batch) if tree is not None else set(batch)
        out += [i for i in batch if i in kept]
    return out[:limit]


def _vector(tables: Tables, embedder: engine.Embedder, about: str, cond: str, depth: int) -> dict[str, float]:
    """Each entry's best chunk similarity to ``about``, for the nearest ``depth`` entries."""
    if tables.chunks.count_rows() == 0:
        return {}
    qv = embedder.encode([about], query=True)[0]
    b = tables.chunks.search(qv).distance_type("cosine").nprobes(NPROBES).select(["id"]).limit(depth * 3)
    if REFINE:
        b = b.refine_factor(REFINE)
    if cond != "true":
        b = b.where(cond, prefilter=True)
    best: dict[str, float] = {}
    for r in b.to_arrow().to_pylist():
        best.setdefault(r["id"], 1.0 - r["_distance"])
    return dict(list(best.items())[:depth])


def _bm25(tables: Tables, about: str, cond: str, depth: int) -> dict[str, float]:
    terms = [w for w in words(about) if w not in engine._STOPWORDS] or words(about)
    if not terms:
        return {}
    query = MultiMatchQuery(" ".join(dict.fromkeys(terms)), list(tables.content))
    return dict(_fts(tables, query, cond, depth))


def ranked(tables: Tables, embedder: engine.Embedder | None, about: str, cond: str, depth: int,
           within: Sequence[str] | None = None) -> list[str]:
    """Ids passing ``cond`` (and in ``within``), best first by ``about``."""
    if within is not None:
        if not within:
            return []
        cond = _and(cond, id_list(within))
    reach = max(depth, FUSION_DEPTH)
    dense = _vector(tables, embedder, about, cond, reach) if embedder is not None else {}
    fused = engine.fuse(dense, _bm25(tables, about, cond, reach), depth=reach)
    return sorted(fused, key=lambda k: (-fused[k], k))[:depth]


def _cell(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    return value


def hits(tables: Tables, ids: Sequence[str]) -> list[dict[str, Any]]:
    """``{id, <hit fields>}`` for ``ids``, in their order."""
    if not ids:
        return []
    got = tables.entries.search().where(id_list(ids)).select(["id", *tables.hit]).limit(None).to_arrow().to_pylist()
    by_id = {r["id"]: {k: _cell(v) for k, v in r.items()} for r in got}
    return [by_id[i] for i in ids if i in by_id]


def document(tables: Tables, doc_id: str, cond: str = "true") -> dict[str, Any] | None:
    """One entry whole, or ``None`` when it is absent or ``cond`` excludes it."""
    got = tables.entries.search().where(_and(f"id = {quote(doc_id)}", cond)).limit(1).to_arrow().to_pylist()
    return {k: _cell(v) for k, v in got[0].items()} if got else None


# -- sections -------------------------------------------------------------


@dataclass
class Ask:
    """What one index source is asked: the prefilter, the compiled text, the sort and the cap."""

    cond: str
    tree: Tree | None
    about: str | None
    under_or: bool
    sort: tuple[str, bool] | None
    limit: int


def _candidates(tables: Tables, ask: Ask) -> list[str]:
    """The ids passing the filter and the tree, at most :data:`CANDIDATES`, for a ranking or a sort."""
    return _first(tables, ask.tree, ask.cond, None, CANDIDATES) if ask.tree is not None else []


def _ordered(tables: Tables, ask: Ask) -> list[str]:
    """Without ``$about``: the matches, ordered by ``$sort`` when given."""
    if ask.sort is None:
        return _first(tables, ask.tree, ask.cond, None, ask.limit)
    field, descending = ask.sort
    if tables.fields[field].cls == "content":
        if ask.tree is None or ask.tree.negated:
            raise Unserved(f"$sort on the content field {field!r} needs a $contains or $about to score by")
        scored = _fts(tables, ask.tree.query, ask.cond, None if not descending else ask.limit)
        ids = [k for k, _ in scored]
        return ids[:ask.limit] if descending else ids[::-1][:ask.limit]
    if ask.tree is not None and not ask.tree.negated:
        rows = _fts_rows(tables, ask.tree.query, ask.cond, None, [field])
        return _by_field(tables, rows, field, descending)[:ask.limit]
    return _first(tables, ask.tree, ask.cond, iter(_sorted_ids(tables, ask.cond, field, descending)), ask.limit)


def _resorted(tables: Tables, ids: list[str], sort: tuple[str, bool] | None) -> list[str]:
    """With ``$about``, a ``$sort`` on a non-content field reorders the top hits; on content it keeps relevance."""
    if sort is None or tables.fields[sort[0]].cls == "content" or not ids:
        return ids if sort is None or sort[1] else ids[::-1]
    return _sorted_ids(tables, "true", sort[0], sort[1], ids)


def answer(tables: Tables, embedder: engine.Embedder | None, ask: Ask) -> dict[str, list[dict[str, Any]]]:
    """``{exact, related?}`` for one source, each a list of hits."""
    if ask.about is None:
        return {"exact": hits(tables, _ordered(tables, ask))}
    if ask.tree is None:
        exact = ranked(tables, embedder, ask.about, ask.cond, ask.limit)
    else:
        exact = ranked(tables, embedder, ask.about, ask.cond, ask.limit, _candidates(tables, ask))
    out = {"exact": hits(tables, _resorted(tables, exact, ask.sort))}
    if ask.under_or:
        near = ranked(tables, embedder, ask.about, ask.cond, ask.limit * 3)
        inside = matching(tables, ask.tree, near) if ask.tree is not None else set(near)
        related = [i for i in near if i not in inside][:ask.limit]
        out["related"] = hits(tables, _resorted(tables, related, ask.sort))
    return out
