"""A node's provenance as metasmith DAG geometry: its ancestors down to the raw tables, and its direct children.

The geometry is metasmith's own (the `/dag/layout` wire shape), so the page draws it as metasmith's GUI does.
Only metasmith.models is imported: metasmith.ops pulls in the agent and caching stack.
"""
from __future__ import annotations

from metasmith.models.dag_draw import Label
from metasmith.models.dag_renderer import DagRenderer, LabelMode, NodeKind

KIND = {"table": NodeKind.GIVEN, "figure": NodeKind.TRANSFORM}


def provenance(g: dict, node_id: str, max_label_chars: int = 26) -> dict:
    by_id = {n["id"]: n for n in g["nodes"]}
    if node_id not in by_id:
        raise KeyError(f"no node {node_id} at {g['commit'][:10]}")
    parents: dict[str, list[str]] = {}
    for e in g["edges"]:
        parents.setdefault(e["to"], []).append(e["from"])
    keep, todo = set(), [node_id]
    while todo:
        v = todo.pop()
        if v not in keep:
            keep.add(v)
            todo.extend(parents.get(v, []))
    keep |= {e["to"] for e in g["edges"] if e["from"] == node_id}

    r = DagRenderer(label_mode=LabelMode.COLUMN, colour="none")
    for v in sorted(keep, key=lambda v: (by_id[v]["stage"] != "table", v)):
        n = by_id[v]
        r.add_node(KIND.get(n["stage"], NodeKind.DATA), v, label=Label(name=n.get("title") or v, full=v))
    for e in g["edges"]:
        if e["from"] in keep and e["to"] in keep:
            r.add_edge(e["from"], e["to"])

    lay = r.layout(None)
    geo = r.geometry(lay, font_size=13.0, max_label_chars=max_label_chars)
    return {
        "width": geo.width, "height": geo.height, "marker_d": geo.marker_d,
        "row_pitch": geo.row_pitch, "lane_pitch": geo.lane_pitch, "font_size": geo.font_size,
        "nodes": [{"id": n.name, "stage": by_id[n.name]["stage"], "kind": n.kind.name.lower(),
                   "cx": n.cx, "cy": n.cy, "label_x": n.label_x, "marker_w": n.marker_w, "marker_h": n.marker_h,
                   "label": n.label, "truncated": n.truncated,
                   "stale": bool(by_id[n.name].get("stale")), "focus": n.name == node_id}
                  for n in geo.nodes],
        "edges": [{"from": e.src, "to": e.dst, "back": e.back, "d": e.d} for e in geo.edges],
    }
