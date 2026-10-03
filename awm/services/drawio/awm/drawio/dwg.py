"""dwg — place and edit cells in a drawio file from one call.

A small library + CLI over the drawio XML model, so an agent can drop or tweak
a single node or edge without writing a 300-line ElementTree script — yet still
compose the same operations into a complex build. Every op is in place, keyed
on a **stable cell id** (idempotent: re-running updates instead of
duplicating), and never touches cells it was not asked about, so a person's
hand-drawn work and any embedded payloads survive an agent's edits untouched.

This is the operation layer behind the service's ``edit`` verb. Operations are
safe *here* because a checkout has exactly one writer — see
:mod:`awm.drawio.checkout` for why they are deliberately not used at the merge
boundary.

**Id discipline.** Agents own an id namespace prefix (``axes/…``, ``mol/…``)
and never renumber. Ids the editor mints for cells a person draws are stable
once created; agent ids must be *deterministic* or a re-run reads as
delete-plus-create and the diff stops being reviewable.

--------------------------------------------------------------------------
CLI (``--file`` is always required, for safety):

    python -m awm.drawio.dwg ls   --file map.drawio
    python -m awm.drawio.dwg get  --file map.drawio pax_hdr_protein
    python -m awm.drawio.dwg find --file map.drawio --label Protein

    # place (idempotent by id; relative anchors resolve the target's geometry)
    python -m awm.drawio.dwg add-node --file map.drawio --page "Primary Axes" \\
        --id axes/glyc --label "Glycolysis" --right-of pax_hdr_protein --gap 40
    python -m awm.drawio.dwg add-edge --file map.drawio --page "Primary Axes" \\
        --id axes/e1 --from axes/glyc --to pax_hdr_protein

    # mutate / remove / auto-layout
    python -m awm.drawio.dwg set --file map.drawio --id axes/glyc --move 300,200
    python -m awm.drawio.dwg rm  --file map.drawio --id axes/glyc
    python -m awm.drawio.dwg layout --file map.drawio --page "Primary Axes"

--------------------------------------------------------------------------
Library:

    from awm.drawio import dwg
    with dwg.Doc.open(path) as d:                  # auto-saves on clean exit
        p = d.page("Primary Axes")
        p.add_node("axes/glyc", "Glycolysis", right_of="pax_hdr_protein")
        p.add_edge("axes/e1", "axes/glyc", "pax_hdr_protein")
        d.set("axes/glyc", style="fillColor=#f8cecc")

Point it at a **checkout's** file (``drawio path <handle>``), not at a live
diagram: writing the live file directly is the race this service exists to
eliminate.
"""
from __future__ import annotations

import argparse
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from . import xmlmodel

# --- defaults for quick placement ------------------------------------------
DEFAULT_NODE_STYLE = (
    "rounded=1;whiteSpace=wrap;html=1;"
    "fillColor=#dae8fc;strokeColor=#6c8ebf;"
)
DEFAULT_EDGE_STYLE = "rounded=0;orthogonalLoop=1;jettySize=auto;html=1;"
DEFAULT_NODE_SIZE = (160.0, 40.0)
DEFAULT_GAP = 40.0


# ==========================================================================
# style helpers — preserve leading bare shape tokens, merge key=value by key
# ==========================================================================
def parse_style(style: str) -> list[tuple[str, str | None]]:
    """Split a drawio style string into ordered (key, value|None) segments.

    Bare tokens (no `=`, e.g. `group`, `text`, `rounded`) come back as
    (token, None); `key=value` pairs as (key, value). Order is preserved.
    """
    out: list[tuple[str, str | None]] = []
    for seg in (style or "").split(";"):
        if not seg:
            continue
        if "=" in seg:
            k, v = seg.split("=", 1)
            out.append((k, v))
        else:
            out.append((seg, None))
    return out


def serialize_style(segments: list[tuple[str, str | None]]) -> str:
    parts = [k if v is None else f"{k}={v}" for k, v in segments]
    # drawio styles conventionally end with a trailing ';'.
    return ";".join(parts) + (";" if parts else "")


def merge_style(existing: str, incoming: str) -> str:
    """Merge `incoming` into `existing`, replacing keys and appending new ones.

    Existing order is kept; new keys/flags are appended at the end. The leading
    bare shape token of `existing` (e.g. `group`, `shape=image`) is never lost.
    """
    segs = parse_style(existing)
    index = {k: i for i, (k, _) in enumerate(segs)}
    for k, v in parse_style(incoming):
        if k in index:
            segs[index[k]] = (k, v)
        else:
            index[k] = len(segs)
            segs.append((k, v))
    return serialize_style(segs)


# ==========================================================================
# geometry helpers
# ==========================================================================
def _geom_el(cell: ET.Element) -> ET.Element | None:
    for g in cell.findall("mxGeometry"):
        if g.get("as", "geometry") == "geometry":
            return g
    return None


def _read_geom(cell: ET.Element) -> dict | None:
    g = _geom_el(cell)
    if g is None:
        return None
    try:
        return {
            "x": float(g.get("x", "0")),
            "y": float(g.get("y", "0")),
            "width": float(g.get("width", "0")),
            "height": float(g.get("height", "0")),
            "relative": g.get("relative"),
        }
    except (TypeError, ValueError):
        return None


def _set_geom(cell: ET.Element, x=None, y=None, w=None, h=None) -> None:
    g = _geom_el(cell)
    if g is None:
        g = ET.SubElement(cell, "mxGeometry", {"as": "geometry"})
    if x is not None:
        g.set("x", f"{float(x):.0f}")
    if y is not None:
        g.set("y", f"{float(y):.0f}")
    if w is not None:
        g.set("width", f"{float(w):.0f}")
    if h is not None:
        g.set("height", f"{float(h):.0f}")


def cell_kind(cell: ET.Element) -> str:
    if cell.get("edge") == "1":
        return "edge"
    if cell.get("vertex") == "1":
        return "vertex"
    return "other"


# ==========================================================================
# Page — a handle to one <diagram>'s <root>
# ==========================================================================
class Page:
    """One drawio page: its name plus a handle to its <root> element."""

    def __init__(self, doc: "Doc", diagram: ET.Element):
        self.doc = doc
        self.diagram = diagram
        self.name = diagram.get("name", "")
        model = diagram.find("mxGraphModel")
        if model is None:
            raise ValueError(f"page {self.name!r} has no mxGraphModel")
        root = model.find("root")
        if root is None:
            raise ValueError(f"page {self.name!r} has no <root>")
        self.root = root

    # -- lookups ----------------------------------------------------------
    def cell(self, cell_id: str) -> ET.Element | None:
        for c in self.root.findall("mxCell"):
            if c.get("id") == cell_id:
                return c
        return None

    def cells(self) -> list[ET.Element]:
        return [c for c in self.root.findall("mxCell")
                if c.get("id") not in ("0", "1")]

    def order_parents_first(self) -> int:
        """Reorder cells so every parent precedes its children. Returns #moved.

        WHY THIS IS NOT COSMETIC. mxGraph's codec resolves a cell's `parent`
        by id AS IT DECODES, in document order. A child that appears BEFORE
        its parent does not get attached -- drawio silently drops it, so a
        group whose children are all forward references renders as nothing.
        The file stays valid XML and ElementTree parses it fine, which is why
        this failure looks like "the page is blank" rather than a parse error.

        It bit the N Case Study page on 2026-07-20: `mk()`/`add_node` append
        new cells with ET.SubElement, so a script that wraps EXISTING cells in
        a NEW group emits the group after the children it just adopted. Seven
        images pointed forward at their groups; the page came up blank, and
        the editor then autosaved its empty reading back over the file.

        The sort is stable, so a page that already satisfies the invariant is
        left byte-identical -- this is a no-op for every well-formed page.
        """
        cells = self.root.findall("mxCell")
        by_id = {c.get("id"): c for c in cells}
        original = list(cells)

        ordered: list[ET.Element] = []
        placed: set[str] = set()

        def place(c: ET.Element, stack: frozenset[str] = frozenset()) -> None:
            cid = c.get("id")
            if cid in placed:
                return
            parent = c.get("parent")
            # Guard against a parent cycle rather than blowing the stack.
            if parent in by_id and parent not in placed and parent not in stack:
                place(by_id[parent], stack | {cid})
            placed.add(cid)
            ordered.append(c)

        for c in original:
            place(c)

        if ordered == original:
            return 0
        for c in original:
            self.root.remove(c)
        for c in ordered:
            self.root.append(c)
        return sum(1 for a, b in zip(original, ordered) if a is not b)

    def _anchor_geom(self, cell_id: str) -> dict:
        c = self.cell(cell_id)
        if c is None:                       # allow cross-page anchors
            hit = self.doc.locate(cell_id)
            c = hit[1] if hit else None
        if c is None:
            raise KeyError(f"anchor cell {cell_id!r} not found")
        g = _read_geom(c)
        if g is None or g.get("relative"):
            raise ValueError(
                f"anchor {cell_id!r} has no absolute geometry to anchor to")
        return g

    # -- mutations --------------------------------------------------------
    def add_node(
        self,
        cell_id: str,
        label: str = "",
        at: tuple[float, float] | None = None,
        size: tuple[float, float] = DEFAULT_NODE_SIZE,
        style: str = DEFAULT_NODE_STYLE,
        parent: str = "1",
        right_of: str | None = None,
        below: str | None = None,
        in_: str | None = None,
        gap: float = DEFAULT_GAP,
    ) -> ET.Element:
        """Upsert a vertex. Existing id → update; else create.

        Placement precedence: explicit `at` wins; else `right_of`/`below`
        resolve the anchor's current geometry; `in_` re-parents into a
        container (geometry then relative to it). Defaults to (40, 40).
        """
        w, h = size
        if at is not None:
            x, y = at
        elif right_of is not None:
            a = self._anchor_geom(right_of)
            x, y = a["x"] + a["width"] + gap, a["y"]
        elif below is not None:
            a = self._anchor_geom(below)
            x, y = a["x"], a["y"] + a["height"] + gap
        else:
            x, y = DEFAULT_GAP, DEFAULT_GAP
        if in_ is not None:
            parent = in_
            if at is None and right_of is None and below is None:
                x, y = 20.0, 20.0           # sane default inside a container

        cell = self.cell(cell_id)
        if cell is None:
            cell = ET.SubElement(self.root, "mxCell", {"id": cell_id})
        cell.set("value", label)
        cell.set("style", style)
        cell.set("vertex", "1")
        cell.set("parent", parent)
        cell.attrib.pop("edge", None)
        cell.attrib.pop("source", None)
        cell.attrib.pop("target", None)
        _set_geom(cell, x=x, y=y, w=w, h=h)
        return cell

    def add_edge(
        self,
        cell_id: str,
        src: str,
        dst: str,
        style: str = DEFAULT_EDGE_STYLE,
        label: str = "",
        parent: str = "1",
    ) -> ET.Element:
        """Upsert an edge between two cell ids (idempotent by id)."""
        cell = self.cell(cell_id)
        if cell is None:
            cell = ET.SubElement(self.root, "mxCell", {"id": cell_id})
        cell.set("value", label)
        cell.set("style", style)
        cell.set("edge", "1")
        cell.set("parent", parent)
        cell.set("source", src)
        cell.set("target", dst)
        cell.attrib.pop("vertex", None)
        g = _geom_el(cell)
        if g is None:
            ET.SubElement(cell, "mxGeometry",
                          {"relative": "1", "as": "geometry"})
        else:
            g.set("relative", "1")
        return cell

    def remove(self, cell_id: str) -> bool:
        c = self.cell(cell_id)
        if c is None or cell_id in ("0", "1"):
            return False
        self.root.remove(c)
        return True

    def remove_prefix(self, prefix: str) -> int:
        n = 0
        for c in list(self.root.findall("mxCell")):
            cid = c.get("id", "")
            if cid in ("0", "1") or not cid.startswith(prefix):
                continue
            self.root.remove(c)
            n += 1
        return n

    # -- auto-layout ------------------------------------------------------
    def layout(self, prog: str = "dot", rankdir: str = "LR") -> int:
        """Reposition this page's top-level vertices with graphviz.

        Only moves absolute-geometry vertices parented to the layer (`1`);
        edges between them define the graph. Styles, labels and ids are
        untouched. Returns the number of cells moved.
        """
        import os

        # pygraphviz shells out to `dot`/`neato` via $PATH; the conda-installed
        # graphviz lives in this env's bin, which isn't on PATH under
        # `mamba run`. Prepend it (same shim as build_layout.py:46).
        env_bin = str(Path(sys.executable).parent)
        if env_bin not in os.environ.get("PATH", "").split(os.pathsep):
            os.environ["PATH"] = f"{env_bin}{os.pathsep}{os.environ.get('PATH', '')}"
        import pygraphviz as pgv

        nodes: dict[str, dict] = {}
        for c in self.cells():
            if cell_kind(c) != "vertex" or c.get("parent") != "1":
                continue
            g = _read_geom(c)
            if g is None or g.get("relative"):
                continue
            nodes[c.get("id")] = {"width": g["width"], "height": g["height"]}
        if not nodes:
            return 0

        ag = pgv.AGraph(directed=True, rankdir=rankdir)
        for cid, info in nodes.items():
            ag.add_node(
                cid,
                width=f"{info['width'] / 72.0:.3f}",
                height=f"{info['height'] / 72.0:.3f}",
                fixedsize="true",
                label="",
            )
        for c in self.cells():
            if cell_kind(c) != "edge":
                continue
            s, t = c.get("source"), c.get("target")
            if s in nodes and t in nodes:
                ag.add_edge(s, t)

        ag.layout(prog=prog)
        positions = _graphviz_positions(ag, nodes)
        for cid, (x, y) in positions.items():
            _set_geom(self.cell(cid), x=x, y=y)
        return len(positions)


def _graphviz_positions(ag, nodes: dict[str, dict]) -> dict[str, tuple]:
    """graphviz centroids (points, y-up) → drawio top-left, bbox at (40,40).

    Coordinate math mirrors build_layout.py:256 `_read_positions`.
    """
    raw: dict[str, tuple[float, float]] = {}
    for cid in nodes:
        pos = ag.get_node(cid).attr.get("pos", "").rstrip("!")
        gx, gy = (float(v) for v in pos.split(","))
        raw[cid] = (gx, gy)
    min_gx = min(p[0] for p in raw.values())
    max_gy = max(p[1] for p in raw.values())

    pre: dict[str, tuple[float, float]] = {}
    for cid, info in nodes.items():
        gx, gy = raw[cid]
        pre[cid] = (gx - min_gx - info["width"] / 2.0,
                    max_gy - gy - info["height"] / 2.0)
    shift_x = 40.0 - min(p[0] for p in pre.values())
    shift_y = 40.0 - min(p[1] for p in pre.values())
    return {cid: (x + shift_x, y + shift_y) for cid, (x, y) in pre.items()}


# ==========================================================================
# Doc — a handle to the whole <mxfile>
# ==========================================================================
class Doc:
    def __init__(self, path: Path, mxfile: ET.Element):
        self.path = Path(path)
        self.mxfile = mxfile

    # -- open / save ------------------------------------------------------
    @classmethod
    def open(cls, path) -> "Doc":
        path = Path(path)
        return cls(path, xmlmodel.parse(path.read_text(encoding="utf-8")))

    def save(self) -> list[str]:
        """Serialize canonically and write. Returns any warnings raised.

        Writing through :func:`xmlmodel.serialize` rather than
        ``ET.tostring`` is load-bearing: a checkout is line-merged against
        the browser's output, so the two writers have to agree on spelling. A
        raw ElementTree dump would re-order attributes and reflow whitespace,
        turning every agent edit into a whole-file diff and every ``update``
        into a wall of phantom conflicts.

        No backups and no size guard: history lives in the store now, so a bad
        write is one ``restore`` away rather than one ``.bak`` file away.
        """
        warnings: list[str] = []
        # Enforce parent-before-child on every page: a forward parent ref makes
        # drawio drop the cell and render the page blank. Stable, so pages that
        # are already correct are untouched. See Page.order_parents_first.
        for page in self.pages():
            moved = page.order_parents_first()
            if moved:
                warnings.append(
                    f"{page.name}: reordered {moved} cell(s) so parents precede "
                    "children (would have rendered blank)")
        xmlmodel.normalize_tree(self.mxfile)
        self.path.write_text(xmlmodel.serialize(self.mxfile), encoding="utf-8")
        return warnings

    # context-manager: save on clean exit
    def __enter__(self) -> "Doc":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.save()
        return False

    # -- pages ------------------------------------------------------------
    def pages(self) -> list[Page]:
        return [Page(self, d) for d in self.mxfile.findall("diagram")]

    def page_names(self) -> list[str]:
        return [d.get("name", "") for d in self.mxfile.findall("diagram")]

    def page(self, name: str) -> Page:
        for d in self.mxfile.findall("diagram"):
            if d.get("name") == name:
                return Page(self, d)
        raise KeyError(
            f"page {name!r} not found (have: {self.page_names()})")

    def add_page(self, name: str, width: int = 6000, height: int = 900) -> Page:
        """Create (or replace a same-named) page with the standard skeleton."""
        for old in list(self.mxfile.findall("diagram")):
            if old.get("name") == name:
                self.mxfile.remove(old)
                break
        diag = ET.SubElement(self.mxfile, "diagram",
                             {"name": name, "id": uuid.uuid4().hex[:20]})
        model = ET.SubElement(diag, "mxGraphModel", {
            "dx": "1422", "dy": "750", "grid": "1", "gridSize": "10",
            "guides": "1", "tooltips": "1", "connect": "1", "arrows": "1",
            "fold": "1", "page": "1", "pageScale": "1",
            "pageWidth": str(width), "pageHeight": str(height),
            "math": "0", "shadow": "0",
        })
        root = ET.SubElement(model, "root")
        ET.SubElement(root, "mxCell", {"id": "0"})
        ET.SubElement(root, "mxCell", {"id": "1", "parent": "0"})
        return Page(self, diag)

    # -- cross-page cell ops ---------------------------------------------
    def locate(self, cell_id: str, page: str | None = None):
        """Return (Page, cell) for a cell id. Raises if ambiguous across pages."""
        hits = []
        for p in (self.pages() if page is None else [self.page(page)]):
            c = p.cell(cell_id)
            if c is not None:
                hits.append((p, c))
        if not hits:
            return None
        if len(hits) > 1:
            pages = ", ".join(repr(p.name) for p, _ in hits)
            raise ValueError(
                f"id {cell_id!r} exists on multiple pages ({pages}); "
                f"pass page= to disambiguate")
        return hits[0]

    def get(self, cell_id: str, page: str | None = None) -> dict | None:
        hit = self.locate(cell_id, page)
        if hit is None:
            return None
        p, c = hit
        return {
            "id": cell_id,
            "page": p.name,
            "kind": cell_kind(c),
            "value": c.get("value", ""),
            "style": c.get("style", ""),
            "parent": c.get("parent"),
            "source": c.get("source"),
            "target": c.get("target"),
            "geom": _read_geom(c),
        }

    def set(
        self,
        cell_id: str,
        page: str | None = None,
        label: str | None = None,
        move: tuple[float, float] | None = None,
        size: tuple[float, float] | None = None,
        style: str | None = None,
    ) -> bool:
        """Mutate an existing cell. `style` is merged by key (not replaced)."""
        hit = self.locate(cell_id, page)
        if hit is None:
            raise KeyError(f"cell {cell_id!r} not found")
        _, c = hit
        if label is not None:
            c.set("value", label)
        if style is not None:
            c.set("style", merge_style(c.get("style", ""), style))
        if move is not None:
            _set_geom(c, x=move[0], y=move[1])
        if size is not None:
            _set_geom(c, w=size[0], h=size[1])
        return True

    def remove(self, cell_id: str, page: str | None = None) -> bool:
        hit = self.locate(cell_id, page)
        if hit is None:
            return False
        p, _ = hit
        return p.remove(cell_id)

    def find(self, label: str | None = None,
             id_contains: str | None = None) -> list[dict]:
        out: list[dict] = []
        for p in self.pages():
            for c in p.cells():
                cid = c.get("id", "")
                val = c.get("value", "")
                if label is not None and label.lower() not in val.lower():
                    continue
                if id_contains is not None and id_contains not in cid:
                    continue
                out.append({"id": cid, "page": p.name,
                            "kind": cell_kind(c), "value": val})
        return out


# ==========================================================================
# CLI — thin adapter; all behaviour lives in Doc/Page above
# ==========================================================================
def _xy(s: str) -> tuple[float, float]:
    a, b = s.split(",")
    return float(a), float(b)


def _print_get(info: dict | None, cell_id: str) -> int:
    if info is None:
        print(f"not found: {cell_id}", file=sys.stderr)
        return 1
    g = info["geom"]
    print(f"id     {info['id']}")
    print(f"page   {info['page']}")
    print(f"kind   {info['kind']}")
    print(f"value  {info['value']!r}")
    if info["kind"] == "edge":
        print(f"source {info['source']}  target {info['target']}")
    if g:
        print(f"geom   x={g['x']:.0f} y={g['y']:.0f} "
              f"w={g['width']:.0f} h={g['height']:.0f}")
    print(f"style  {info['style']}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="dwg", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_file(p):
        p.add_argument("--file", type=Path, required=True,
                       help="target .drawio file (always explicit)")

    pl = sub.add_parser("ls", help="list pages, or cells in a page")
    add_file(pl)
    pl.add_argument("--page")

    pg = sub.add_parser("get", help="dump one cell")
    add_file(pg)
    pg.add_argument("id")
    pg.add_argument("--page")

    pf = sub.add_parser("find", help="search cells by label / id substring")
    add_file(pf)
    pf.add_argument("--label")
    pf.add_argument("--id-contains", dest="id_contains")

    pn = sub.add_parser("add-node", help="upsert a vertex (idempotent by id)")
    add_file(pn)
    pn.add_argument("--page", required=True)
    pn.add_argument("--id", required=True)
    pn.add_argument("--label", default="")
    pn.add_argument("--at", type=_xy, help="x,y")
    pn.add_argument("--size", type=_xy, default=DEFAULT_NODE_SIZE, help="w,h")
    pn.add_argument("--style", default=DEFAULT_NODE_STYLE)
    pn.add_argument("--parent", default="1")
    pn.add_argument("--right-of", dest="right_of")
    pn.add_argument("--below")
    pn.add_argument("--in", dest="in_")
    pn.add_argument("--gap", type=float, default=DEFAULT_GAP)

    pe = sub.add_parser("add-edge", help="upsert an edge (idempotent by id)")
    add_file(pe)
    pe.add_argument("--page", required=True)
    pe.add_argument("--id", required=True)
    pe.add_argument("--from", dest="src", required=True)
    pe.add_argument("--to", dest="dst", required=True)
    pe.add_argument("--style", default=DEFAULT_EDGE_STYLE)
    pe.add_argument("--label", default="")

    ps = sub.add_parser("set", help="mutate an existing cell")
    add_file(ps)
    ps.add_argument("--id", required=True)
    ps.add_argument("--page")
    ps.add_argument("--label")
    ps.add_argument("--move", type=_xy, help="x,y")
    ps.add_argument("--size", type=_xy, help="w,h")
    ps.add_argument("--style", help="key=value;... merged into existing style")

    pr = sub.add_parser("rm", help="remove a cell (or --prefix)")
    add_file(pr)
    pr.add_argument("--id")
    pr.add_argument("--prefix", help="remove all cells whose id starts with this")
    pr.add_argument("--page")

    pp = sub.add_parser("add-page", help="create/replace a page")
    add_file(pp)
    pp.add_argument("--name", required=True)
    pp.add_argument("--width", type=int, default=6000)
    pp.add_argument("--height", type=int, default=900)

    py = sub.add_parser("layout", help="auto-position a page with graphviz")
    add_file(py)
    py.add_argument("--page", required=True)
    py.add_argument("--algo", default="dot")
    py.add_argument("--rankdir", default="LR")

    args = ap.parse_args(argv)

    # ---- read-only verbs -------------------------------------------------
    if args.cmd == "ls":
        d = Doc.open(args.file)
        if args.page:
            for c in d.page(args.page).cells():
                g = _read_geom(c)
                loc = (f"({g['x']:.0f},{g['y']:.0f})" if g and not g.get("relative")
                       else "-")
                print(f"{cell_kind(c):6s} {c.get('id'):40s} {loc:14s} "
                      f"{c.get('value', '')!r}")
        else:
            for p in d.pages():
                print(f"{p.name:24s} {len(p.cells())} cells")
        return 0

    if args.cmd == "get":
        return _print_get(Doc.open(args.file).get(args.id, args.page), args.id)

    if args.cmd == "find":
        hits = Doc.open(args.file).find(args.label, args.id_contains)
        for h in hits:
            print(f"{h['kind']:6s} {h['id']:40s} [{h['page']}] {h['value']!r}")
        print(f"({len(hits)} hits)", file=sys.stderr)
        return 0

    # ---- mutating verbs --------------------------------------------------
    d = Doc.open(args.file)
    if args.cmd == "add-node":
        d.page(args.page).add_node(
            args.id, args.label, at=args.at, size=tuple(args.size),
            style=args.style, parent=args.parent, right_of=args.right_of,
            below=args.below, in_=args.in_, gap=args.gap)
        print(f"add-node {args.id} on {args.page!r}")
    elif args.cmd == "add-edge":
        d.page(args.page).add_edge(args.id, args.src, args.dst,
                                   style=args.style, label=args.label)
        print(f"add-edge {args.id}: {args.src} -> {args.dst}")
    elif args.cmd == "set":
        d.set(args.id, page=args.page, label=args.label, move=args.move,
              size=args.size, style=args.style)
        print(f"set {args.id}")
    elif args.cmd == "rm":
        if args.prefix:
            page = d.page(args.page) if args.page else None
            n = (page.remove_prefix(args.prefix) if page
                 else sum(p.remove_prefix(args.prefix) for p in d.pages()))
            print(f"rm prefix {args.prefix!r}: {n} cells")
        elif args.id:
            ok = d.remove(args.id, args.page)
            print(f"rm {args.id}: {'removed' if ok else 'not found'}")
        else:
            ap.error("rm needs --id or --prefix")
    elif args.cmd == "add-page":
        d.add_page(args.name, args.width, args.height)
        print(f"add-page {args.name!r}")
    elif args.cmd == "layout":
        n = d.page(args.page).layout(prog=args.algo, rankdir=args.rankdir)
        print(f"layout {args.page!r}: moved {n} cells")

    for warning in d.save():
        print(f"[dwg] {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # Output piped into `head`/`less` and closed early — not an error.
        try:
            sys.stdout.close()
        finally:
            sys.exit(0)
