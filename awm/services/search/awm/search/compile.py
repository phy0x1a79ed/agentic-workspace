"""Compile a validated query filter and text tree into SQL over the ``documents`` table.

Every value travels as a bound parameter. Single-valued fields test a column; multi-valued
fields test ``doc_values``. The text tree needs only ``.text`` on a literal and ``.op`` and
``.items`` on an operator, so this module does not import the query parser.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Sequence

from awm.search.index import PROJECT_CHANNEL, veil

SOURCE_OF = {"journals": "journals", "transcripts": "transcripts", "kb": "kb"}
SINGLE = ("project", "scope", "ts")
MULTI = ("tag", "replies_to", "author", "cites", "cited_by")
TRIGRAM = 3
_COMPARE = {"$gt": ">", "$gte": ">=", "$lt": "<", "$lte": "<="}
_LOGIC = ("$and", "$or", "$nor")

SQL = tuple[str, list[Any]]


def glob_pattern(value: str) -> str:
    """``value`` as a GLOB pattern in which only ``*`` is a wildcard."""
    return value.replace("[", "[[]").replace("?", "[?]")


def _is_glob(value: Any) -> bool:
    return isinstance(value, str) and "*" in value


def _join(op: str, parts: list[SQL], empty: str) -> SQL:
    if not parts:
        return empty, []
    if len(parts) == 1:
        return parts[0]
    return "(" + f" {op} ".join(p[0] for p in parts) + ")", [x for p in parts for x in p[1]]


def _not(part: SQL) -> SQL:
    return f"NOT ({part[0]})", part[1]


def _single_equals(field: str, value: Any) -> SQL:
    v = str(value)
    glob = _is_glob(v)
    pat = glob_pattern(v) if glob else v
    cmp = "GLOB" if glob else "="
    if field == "project":
        # A journal's project channel is stored with no project and scope 'project:<name>'.
        return (f"(project {cmp} ? OR (project = '' AND scope {cmp} ?))",
                [pat, PROJECT_CHANNEL.format(pat)])
    if field == "scope":
        if not glob:
            return "(scope = ? OR substr(scope, 1, ?) = ?)", [v, len(v) + 1, v + "/"]
        if v.endswith("*"):
            return "scope GLOB ?", [pat]  # the trailing * already spans '/', so nested scopes match
        return "(scope GLOB ? OR scope GLOB ?)", [pat, pat + "/*"]
    return f"ts {cmp} ?", [pat]


def _multi_exists(field: str, comparison: str, params: list[Any]) -> SQL:
    return ("EXISTS (SELECT 1 FROM doc_values v WHERE v.rid = documents.rid AND v.field = ? AND "
            + comparison + ")", [field, *params])


def _multi_value(field: str, value: Any) -> str:
    text = str(value)
    return " ".join(text.split()).lower() if field == "tag" else text


def _multi_equals(field: str, value: Any) -> SQL:
    v = _multi_value(field, value)
    if _is_glob(v):
        return _multi_exists(field, "v.value GLOB ?", [glob_pattern(v)])
    return _multi_exists(field, "v.value = ?", [v])


def _any_of(field: str, values: Sequence[Any]) -> SQL:
    equals = _single_equals if field in SINGLE else _multi_equals
    return _join("OR", [equals(field, v) for v in values], "0")


def _field(field: str, spec: Any) -> SQL:
    if field not in SINGLE and field not in MULTI:
        raise ValueError(f"unknown field {field!r}; allowed: {', '.join(SINGLE + MULTI)}")
    single = field in SINGLE
    ops = spec if isinstance(spec, dict) else {"$eq": spec}
    equals = _single_equals if single else _multi_equals
    parts: list[SQL] = []
    for op, value in ops.items():
        if op == "$eq":
            parts.append(equals(field, value))
        elif op == "$ne":
            parts.append(_not(equals(field, value)))
        elif op == "$in":
            parts.append(_any_of(field, value))
        elif op == "$nin":
            parts.append(_not(_any_of(field, value)))
        elif op == "$all":
            parts.append(_join("AND", [equals(field, v) for v in value], "1"))
        elif op in _COMPARE:
            sign = _COMPARE[op]
            if not single:
                parts.append(_multi_exists(field, f"v.value {sign} ?", [_multi_value(field, value)]))
            elif field == "ts" and op in ("$lt", "$lte"):
                parts.append(("(ts <> '' AND ts " + sign + " ?)", [str(value)]))  # no date is not "before"
            else:
                parts.append((f"{field} {sign} ?", [str(value)]))
        else:
            raise ValueError(f"unknown operator {op!r} on {field!r}")
    return _join("AND", parts, "1")


def _filter(f: dict) -> SQL:
    parts: list[SQL] = []
    for key, value in f.items():
        if key == "$and":
            parts.append(_join("AND", [_filter(x) for x in value], "1"))
        elif key == "$or":
            parts.append(_join("OR", [_filter(x) for x in value], "0"))
        elif key == "$nor":
            parts.append(_not(_join("OR", [_filter(x) for x in value], "0")))
        else:
            parts.append(_field(key, value))
    return _join("AND", parts, "1")


def filter_sql(kind: str, f: dict) -> SQL:
    """A boolean SQL expression over ``documents`` columns for the filter ``f``; ``{}`` gives ``1``."""
    return _filter(f)


def quote_phrase(literal: str) -> str:
    return '"' + literal.replace('"', '""') + '"'


def contains_sql(node: Any) -> SQL:
    """A boolean SQL expression that holds when the title or text contains the literal tree ``node``."""
    if hasattr(node, "text"):
        if len(node.text) >= TRIGRAM:
            return ("rid IN (SELECT rowid FROM documents_exact WHERE documents_exact MATCH ?)",
                    [quote_phrase(node.text)])
        return "instr(lower(title || ' ' || text), lower(?)) > 0", [node.text]
    parts = [contains_sql(x) for x in node.items]
    if node.op == "and":
        return _join("AND", parts, "1")
    if node.op == "or":
        return _join("OR", parts, "0")
    return _not(_join("OR", parts, "0"))


def where(kind: str, f: dict, contains: Any, negate: bool, hidden: Sequence[dict] | None) -> SQL:
    """The condition for one source: its kind, the filter, the text literal (or its negation) and the veil."""
    fsql, fparams = filter_sql(kind, f)
    parts: list[SQL] = [("source = ?", [SOURCE_OF.get(kind, kind)]), (f"({fsql})", fparams)]
    if contains is not None:
        c = contains_sql(contains)
        parts.append(_not(c) if negate else c)
    elif negate:
        parts.append(("0", []))
    hidden_sql, hidden_params = veil(hidden)
    if hidden_sql:
        parts.append((hidden_sql, hidden_params))
    return " AND ".join(p[0] for p in parts), [x for p in parts for x in p[1]]


def allowed(kind: str, f: dict, contains: Any, negate: bool, hidden: Sequence[dict] | None) -> SQL:
    """``SELECT key FROM documents WHERE <where>``, the shape the engine takes as ``allowed``."""
    sql, params = where(kind, f, contains, negate, hidden)
    return "SELECT key FROM documents WHERE " + sql, params


def newest(conn: sqlite3.Connection, kind: str, f: dict, contains: Any,
           hidden: Sequence[dict] | None, limit: int) -> list[dict[str, Any]]:
    """The ``limit`` newest documents passing the filter and the text, as ``{doc_id, title, ts}``."""
    sql, params = where(kind, f, contains, False, hidden)
    rows = conn.execute(f"SELECT doc_id, title, ts FROM documents WHERE {sql} ORDER BY ts DESC LIMIT ?",
                        [*params, limit]).fetchall()
    return [{"doc_id": r[0], "title": r[1], "ts": r[2]} for r in rows]
