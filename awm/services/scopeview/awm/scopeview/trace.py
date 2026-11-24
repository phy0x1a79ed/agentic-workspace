"""Citations: how a sentence, a list item or a figure mark names what it was built from, down to table rows.

A chain body cites inline as [[id]], [[id#n]] (the nth list item of a text node) or [[id?<conditions>]]:
rows of a table, or the rows behind a figure, joined by `&`, each `col=v1,v2`, `col!=v` or `col>n` (also <, >=, <=).
A figure names its rows with front matter `rows: {table: table:<path>, key: <column>}`, and its script puts
each mark's key values in customdata.
"""
from __future__ import annotations

import re

from awm.scopeview.tree import Tree

CITE = re.compile(r"\[\[([^\[\]\s]+)\]\]")
ITEM = re.compile(r"^(?:[-*+]|\d+[.)])\s+(.*)$")
MAX_ROWS = 50


COND = re.compile(r"([^=!<>&]+)(=|!=|>=|<=|>|<)([^&]*)")
COMPARE = {">": "greater", "<": "less", ">=": "greater_equal", "<=": "less_equal"}


def parse(cite: str) -> tuple[str, int | None, list[tuple[str, str, list[str]]] | None]:
    """id, list item (1-based), and the row conditions (None when there is no selection; [] when it is malformed)."""
    nid, sep, rest = re.fullmatch(r"([^#?]+)([#?]?)(.*)", cite).groups()
    if sep == "#":
        return nid, int(rest) if rest.isdigit() else 0, None
    if sep == "?":
        conds = [COND.fullmatch(c) for c in rest.split("&")]
        return nid, None, [(m[1], m[2], m[3].split(",")) for m in conds] if all(conds) else []
    return nid, None, None


def cites(text: str) -> list[str]:
    return CITE.findall(text)


def items(body: str) -> list[str]:
    """Top-level list items, one line each."""
    return [m.group(1) for ln in body.splitlines() if (m := ITEM.match(ln))]


def table_file(tree: Tree, nid: str) -> str | None:
    path = "data/" + nid[len("table:"):]
    return path if path in tree.pins and path.endswith((".parquet", ".tsv", ".csv")) else None


def read(tree: Tree, path: str, schema_only: bool = False):
    import pyarrow.csv as pcsv
    import pyarrow.parquet as pq

    if path.endswith(".parquet"):
        return pq.read_schema(tree.local(path)) if schema_only else pq.read_table(tree.local(path))
    opts = pcsv.ParseOptions(delimiter="\t" if path.endswith(".tsv") else ",")
    t = pcsv.read_csv(tree.local(path), parse_options=opts)
    return t.schema if schema_only else t


def select(tree: Tree, nid: str, conds: list[tuple[str, str, list[str]]]) -> dict:
    import pyarrow as pa
    import pyarrow.compute as pc

    from awm.scopeview.chain import clip

    out = {"table": nid, "where": "&".join(f"{c}{op}{','.join(v)}" for c, op, v in conds)}
    t = read(tree, table_file(tree, nid))
    mask = None
    try:
        for col, op, vals in conds:
            if op in ("=", "!="):
                m = pc.is_in(pc.cast(t[col], pa.string()), value_set=pa.array(vals, pa.string()))
                m = pc.invert(m) if op == "!=" else m
            else:
                m = getattr(pc, COMPARE[op])(t[col], pa.scalar(float(vals[0])))
            mask = m if mask is None else pc.and_(mask, m)
    except (pa.ArrowException, ValueError, KeyError) as e:
        return {**out, "error": str(e)}
    t = t.filter(mask)
    return {**out, "total": t.num_rows, "columns": t.column_names,
            "rows": [{k: clip(v) for k, v in r.items()} for r in t.slice(0, MAX_ROWS).to_pylist()]}


def trace(tree: Tree, g: dict, start: list[str]) -> dict:
    """Follow citations from `start` down to table rows. Uncited text falls back to the node's `from`."""
    from awm.scopeview.chain import split

    by_id = {n["id"]: n for n in g["nodes"]}
    lit, steps, rows, done = set(), [], [], set()

    def visit(cite: str, depth: int):
        if cite in done or depth > 12:
            return
        done.add(cite)
        nid, n, conds = parse(cite)
        node = by_id.get(nid)
        if node is None:
            return
        lit.add(nid)
        if node["stage"] == "table":
            if conds and table_file(tree, nid):
                rows.append(select(tree, nid, conds))
            return
        if node["stage"] == "figure":
            spec = node.get("rows")
            if conds and spec and table_file(tree, spec["table"]):
                rows.append({**select(tree, spec["table"], conds), "via": nid})
                lit.add(spec["table"])
            for ref in node["from"]:
                visit(ref, depth + 1)
            return
        body = split(tree.text(node["path"]))[1]
        text = body if n is None else (items(body)[n - 1:n] or [""])[0]
        steps.append({"cite": cite, "node": nid, "stage": node["stage"], "title": node["title"],
                      **({"text": text} if n is not None else {})})
        below = cites(text)
        for ref in below or node["from"]:
            visit(ref, depth + 1)

    for c in start:
        visit(c, 0)
    return {"lit": sorted(lit), "steps": steps, "rows": rows}


def problems(tree: Tree, nodes: list[dict], by_id: dict, ancestors) -> list[dict]:
    """`bad_cite` for each citation that does not resolve, or names a node outside the citing node's provenance."""
    out = []
    for node in nodes:
        for c in cites(node["body"]):
            nid, n, conds = parse(c)
            target = by_id.get(nid)
            why = None
            if target is None:
                why = "names no node"
            elif nid not in ancestors(node["id"]):
                why = "is not upstream of this node; add it to `from` or cite through an input"
            elif n is not None and (target["stage"] in ("table", "figure") or not 0 < n <= len(items(target["body"]))):
                why = f"has no list item {n}"
            elif conds == []:
                why = "has a malformed selection; write col=v1,v2, col!=v or col>n joined by &"
            elif conds and target["stage"] not in ("table", "figure"):
                why = "selects rows of a text node"
            elif conds:
                table = target["id"] if target["stage"] == "table" else (target.get("rows") or {}).get("table")
                path = table and table_file(tree, table)
                if not path:
                    why = "selects rows, but names no pinned table file (a figure needs `rows` front matter)"
                else:
                    missing = {c for c, _, _ in conds} - set(read(tree, path, schema_only=True).names)
                    if missing:
                        why = f"selects on {', '.join(sorted(missing))}, not columns of {table}"
            if why:
                out.append({"node": node["id"], "code": "bad_cite", "message": f"[[{c}]] {why}"})
    return out
