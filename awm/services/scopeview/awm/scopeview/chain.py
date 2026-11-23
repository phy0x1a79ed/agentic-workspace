"""A scope's graph: XGDB raw tables under data/, and the chain of nodes under chain/ built on them.

A chain node is chain/<stage dir>/<id>.md: YAML front matter, then a markdown body. Edges live on the
child (`from`). Nothing stores the graph. Each node keeps `seen`, the content hash of each input at its
last review, and a node whose input hash moved is stale.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import date
from pathlib import Path

import yaml

from awm.scopeview.tree import Tree

STAGES = ["table", "question", "figure", "interpretation", "point", "prose"]
DIRS = {"question": "questions", "figure": "figures", "interpretation": "interpretations", "point": "points", "prose": "prose"}
PREFIX = {"question": "q", "figure": "f", "interpretation": "i", "point": "p", "prose": "s"}
REVIEW_KEYS = {"seen", "review", "status", "stale_ok"}
FRONT = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)


def split(text: str) -> tuple[dict, str]:
    m = FRONT.match(text)
    if not m:
        raise ValueError("no front matter")
    return yaml.safe_load(m.group(1)) or {}, m.group(2)


def join(meta: dict, body: str) -> str:
    return f"---\n{yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=120)}---\n{body}"


def tsv(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text), delimiter="\t"))


def digest(*parts) -> str:
    return hashlib.sha1("\0".join(str(p) for p in parts).encode()).hexdigest()


# ---------------------------------------------------------------- raw tables


def raw_nodes(tree: Tree) -> list[dict]:
    """XGDB datasets: a folder data/<d>/ holding samples.tsv. Pooled tables and product folders become nodes."""
    nodes = []
    for sample_file in sorted(p for p in tree.pins if re.fullmatch(r"data/[^/]+/samples\.tsv", p)):
        root = sample_file.rsplit("/", 1)[0]
        ds = root.split("/", 1)[1]
        files = {p[len(root) + 1:]: m for p, m in tree.pins.items() if p.startswith(root + "/")}
        gaps = tsv(tree.text(f"{root}/_logs/gaps.tsv")) if "_logs/gaps.tsv" in files else []
        ledger = {r["path"]: r for r in tsv(tree.text(f"{root}/_logs/ledger.tsv"))} if "_logs/ledger.tsv" in files else {}
        samples = tsv(tree.text(sample_file))
        products: dict[tuple[str, str], list[str]] = {}
        for rel in files:
            parts = rel.split("/")
            if parts[0] == "tables" and len(parts) == 2:
                nodes.append({
                    "id": f"table:{ds}/{rel}", "stage": "table", "kind": "table", "dataset": ds, "subject": "tables",
                    "title": parts[1], "path": f"{root}/{rel}", "hash": files[rel],
                    "source": ledger.get(rel, {}).get("source"),
                })
            elif len(parts) == 3 and not parts[0].startswith("_"):
                products.setdefault((parts[0], parts[1]), []).append(rel)
        for (subject, product), rels in sorted(products.items()):
            nodes.append({
                "id": f"table:{ds}/{subject}/{product}", "stage": "table", "kind": "product", "dataset": ds,
                "subject": subject, "title": product, "path": f"{root}/{subject}/{product}",
                "hash": digest(*sorted(f"{r}:{files[r]}" for r in rels)),
                "files": len(rels), "samples": len(samples),
                "gaps": [g for g in gaps if g.get("product") == product],
                "source": sorted({ledger[r]["source"] for r in rels if r in ledger}),
            })
    return nodes


def table_hash(tree: Tree, ref: str) -> str | None:
    """Hash of a `table:<path under data/>` input: one pinned file, or every pinned file below a folder."""
    path = "data/" + ref[len("table:"):].rstrip("/")
    if path in tree.pins:
        return tree.pins[path]
    below = sorted(f"{p}:{m}" for p, m in tree.pins.items() if p.startswith(path + "/"))
    return digest(*below) if below else None


# ---------------------------------------------------------------- chain


def chain_nodes(tree: Tree) -> tuple[list[dict], list[dict]]:
    nodes, problems = [], []
    for path in tree.files("chain/"):
        if not path.endswith(".md"):
            continue
        try:
            meta, body = split(tree.text(path))
        except (ValueError, yaml.YAMLError) as e:
            problems.append({"node": path, "code": "bad_front_matter", "message": str(e)})
            continue
        stage = meta.get("stage")
        if stage not in DIRS or not meta.get("id"):
            problems.append({"node": path, "code": "bad_front_matter", "message": f"needs an id and a stage in {list(DIRS)}"})
            continue
        if path.split("/")[1] != DIRS[stage]:
            problems.append({"node": meta["id"], "code": "wrong_folder", "message": f"a {stage} lives in chain/{DIRS[stage]}/"})
        frm = meta.get("from") or []
        nodes.append({
            "id": str(meta["id"]), "stage": stage, "title": meta.get("title") or meta["id"], "path": path,
            "from": [str(f) for f in (frm if isinstance(frm, list) else [frm])],
            "artifact": meta.get("artifact"), "made_by": meta.get("made_by"),
            "status": meta.get("status", "draft"), "review": meta.get("review"), "stale_ok": meta.get("stale_ok"),
            "seen": meta.get("seen") or {}, "body": body,
            "hash": digest(meta.get("title"), sorted(map(str, frm if isinstance(frm, list) else [frm])), body,
                           tree.hash(meta["artifact"]) if meta.get("artifact") else "",
                           tree.hash(meta["made_by"]) if meta.get("made_by") else ""),
        })
    return nodes, problems


def build(tree: Tree, gate: bool = False) -> dict:
    """The whole graph with stale flags, and every problem `check` reports."""
    raw = raw_nodes(tree)
    chain, problems = chain_nodes(tree)
    by_id: dict[str, dict] = {n["id"]: n for n in raw}
    for n in chain:
        if n["id"] in by_id:
            problems.append({"node": n["id"], "code": "duplicate_id", "message": f"also at {by_id[n['id']]['path']}"})
        by_id[n["id"]] = n

    def current(ref: str) -> str | None:
        if ref in by_id:
            return by_id[ref]["hash"]
        return table_hash(tree, ref) if ref.startswith("table:") else None

    edges = []
    for n in chain:
        n["stale_inputs"] = []
        for ref in n["from"]:
            h = current(ref)
            if h is None:
                problems.append({"node": n["id"], "code": "unresolved_from", "message": f"{ref} names no node or pinned table"})
                continue
            edges.append({"from": ref, "to": n["id"]})
            if ref.startswith("table:") and ref not in by_id:
                by_id[ref] = {"id": ref, "stage": "table", "kind": "path", "title": ref[6:], "path": "data/" + ref[6:], "hash": h}
                raw.append(by_id[ref])
            up = by_id[ref]["stage"]
            if STAGES.index(up) >= STAGES.index(n["stage"]):
                problems.append({"node": n["id"], "code": "stage_order", "message": f"a {n['stage']} cannot be built from a {up} ({ref})"})
            if n["seen"].get(ref) != h:
                n["stale_inputs"].append(ref)
        for key in ("artifact", "made_by"):
            if n[key] and not tree.exists(n[key]):
                problems.append({"node": n["id"], "code": f"{key}_missing", "message": f"{n[key]} is not tracked (an artifact needs a DVC pin)"})
        if n["artifact"] and n["artifact"] not in tree.pins:
            if tree.exists(n["artifact"]):
                problems.append({"node": n["id"], "code": "artifact_unpinned", "message": f"{n['artifact']} is in git; put it under a DVC pin"})
        n["stale"] = bool(n["stale_inputs"])

    for c in cycles(chain):
        problems.append({"node": c[0], "code": "cycle", "message": " → ".join(c)})

    order = sorted(chain, key=lambda n: STAGES.index(n["stage"]))
    for n in order:
        n["upstream_stale"] = any(by_id.get(r, {}).get("stale") or by_id.get(r, {}).get("upstream_stale") for r in n["from"])
        if gate and n["stale"] and not n["stale_ok"]:
            problems.append({"node": n["id"], "code": "stale", "message": f"inputs changed since review: {', '.join(n['stale_inputs'])}"})

    used_by: dict[str, list[str]] = {}
    for e in edges:
        used_by.setdefault(e["from"], []).append(e["to"])
    for n in by_id.values():
        n["used_by"] = sorted(used_by.get(n["id"], []))
    for n in chain:
        del n["body"]
    return {"commit": tree.commit, "ref": tree.ref, "stages": STAGES, "nodes": raw + chain, "edges": edges,
            "problems": problems, "ok": not problems}


def cycles(chain: list[dict]) -> list[list[str]]:
    graph = {n["id"]: [r for r in n["from"] if not r.startswith("table:")] for n in chain}
    state, found = {}, []

    def visit(v, path):
        state[v] = 1
        for u in graph.get(v, []):
            if state.get(u) == 1:
                found.append(path[path.index(u):] + [u])
            elif u in graph and not state.get(u):
                visit(u, path + [u])
        state[v] = 2

    for v in graph:
        if not state.get(v):
            visit(v, [v])
    return found


# ---------------------------------------------------------------- one node


def preview_table(path: Path, name: str, rows: int = 200) -> dict:
    if name.endswith(".parquet"):
        import pyarrow.parquet as pq

        f = pq.ParquetFile(path)
        batch = next(f.iter_batches(batch_size=rows), None)
        data = batch.to_pylist() if batch is not None else []
        return {"format": "parquet", "rows": f.metadata.num_rows,
                "columns": [{"name": c.name, "type": str(c.type)} for c in f.schema_arrow],
                "head": [{k: clip(v) for k, v in r.items()} for r in data]}
    if name.endswith((".tsv", ".csv")):
        with open(path, newline="") as fh:
            lines = [ln for _, ln in zip(range(rows + 1), fh)]
        reader = list(csv.reader(lines, delimiter="\t" if name.endswith(".tsv") else ","))
        cols = reader[0] if reader else []
        return {"format": "tsv", "columns": [{"name": c} for c in cols],
                "head": [{c: clip(v) for c, v in zip(cols, r)} for r in reader[1:]]}
    return preview_text(path, name)


MAX_TEXT = 256 << 10


def preview_text(path: Path, name: str) -> dict:
    """Any other file as text when it is small and decodes, with JSON pretty-printed."""
    out = {"format": name.rsplit(".", 1)[-1], "bytes": path.stat().st_size}
    if out["bytes"] > MAX_TEXT:
        return out
    try:
        text = path.read_bytes().decode()
    except UnicodeDecodeError:
        return out
    if name.endswith(".json"):
        import json

        try:
            text = json.dumps(json.loads(text), indent=2)
        except ValueError:
            pass
    return {**out, "text": text}


def clip(v):
    if isinstance(v, (bytes, bytearray)):
        return f"<{len(v)} bytes>"
    if isinstance(v, (list, dict)):
        v = str(v)
    if isinstance(v, str) and len(v) > 300:
        return v[:300] + "…"
    if isinstance(v, (date,)):
        return v.isoformat()
    return v


MAX_FIGURE = 8 << 20


def node(tree: Tree, node_id: str) -> dict:
    g = build(tree)
    n = next((n for n in g["nodes"] if n["id"] == node_id), None)
    if n is None:
        raise KeyError(f"no node {node_id} at {tree.commit[:10]}")
    out = dict(n)
    if n["stage"] == "table":
        if n.get("kind") == "product":
            rels = sorted(p for p in tree.pins if p.startswith(n["path"] + "/"))
            out["listing"] = [{"file": p.rsplit("/", 1)[1]} for p in rels[:500]]
            first = next((p for p in rels if p.endswith((".tsv", ".parquet", ".csv"))), rels[0] if rels else None)
            if first:
                out["preview"] = {"file": first.rsplit("/", 1)[1], **preview_table(tree.local(first), first, 50)}
        elif n["path"] in tree.pins:
            out["preview"] = preview_table(tree.local(n["path"]), n["path"])
        return out
    meta, body = split(tree.text(n["path"]))
    out["body"] = body
    if n["artifact"]:
        if n["artifact"] not in tree.pins:
            out["artifact_error"] = f"{n['artifact']} has no DVC pin at this ref"
        else:
            try:
                p = tree.local(n["artifact"])
                if p.stat().st_size > MAX_FIGURE:
                    out["artifact_error"] = f"{n['artifact']} is over {MAX_FIGURE >> 20} MB"
                elif n["artifact"].endswith(".json"):
                    import json

                    out["figure"] = json.loads(p.read_text())
            except FileNotFoundError as e:
                out["artifact_error"] = str(e)
    if n["made_by"] and tree.exists(n["made_by"]):
        out["made_by_source"] = tree.text(n["made_by"])
    return out


def history(tree: Tree, node_id: str | None) -> list[dict]:
    """Commits that touched a node's file, artifact pin or made_by; with no node, the scope's versions."""
    if not node_id:
        return tree.log(limit=100, first_parent=True)
    g = build(tree)
    n = next((n for n in g["nodes"] if n["id"] == node_id), None)
    if n is None:
        raise KeyError(f"no node {node_id}")
    if n["stage"] == "table":
        ds = n["path"].split("/")[1]
        return tree.log(f"data/{ds}.dvc")
    paths = [n["path"]] + [p for p in (n.get("made_by"), pin_of(tree, n.get("artifact"))) if p]
    return tree.log(*paths)


def pin_of(tree: Tree, path: str | None) -> str | None:
    if not path:
        return None
    parts = path.split("/")
    for i in range(len(parts), 1, -1):
        pin = "/".join(parts[:i]) + ".dvc"
        if tree.exists(pin):
            return pin
    return None


# ---------------------------------------------------------------- writes (worktree only)


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return "-".join(s.split("-")[:6]) or "node"


def new(root: Path, stage: str, sources: list[str], title: str, node_id: str | None = None) -> dict:
    if stage not in DIRS:
        raise ValueError(f"stage must be one of {list(DIRS)}")
    tree = Tree(root)
    g = build(tree)
    ids = {n["id"] for n in g["nodes"]}
    base = node_id or f"{PREFIX[stage]}-{slug(title)}"
    nid, k = base, 2
    while nid in ids:
        nid, k = f"{base}-{k}", k + 1
    hashes = {n["id"]: n["hash"] for n in g["nodes"]}
    seen = {}
    for ref in sources:
        h = hashes.get(ref) or (table_hash(tree, ref) if ref.startswith("table:") else None)
        if h is None:
            raise ValueError(f"{ref} names no node or pinned table")
        seen[ref] = h
    meta = {"id": nid, "stage": stage, "title": title, "from": list(sources)}
    if stage == "figure":
        meta["artifact"] = f"data/chain/{nid}.plotly.json"
        meta["made_by"] = f"chain/figures/{nid}.py"
    meta.update({"status": "draft", "seen": seen})
    path = root / "chain" / DIRS[stage] / f"{nid}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(join(meta, "\n"))
    return {"id": nid, "path": str(path.relative_to(root)), **{k: meta[k] for k in ("artifact", "made_by") if k in meta}}


def accept(root: Path, node_id: str, note: str) -> dict:
    if not (note or "").strip():
        raise ValueError("accept needs a review note: say what you re-read and why it still holds")
    tree = Tree(root)
    g = build(tree)
    n = next((n for n in g["nodes"] if n["id"] == node_id and n["stage"] != "table"), None)
    if n is None:
        raise KeyError(f"no chain node {node_id}")
    hashes = {m["id"]: m["hash"] for m in g["nodes"]}
    meta, body = split((root / n["path"]).read_text())
    seen = {}
    for ref in n["from"]:
        h = hashes.get(ref) or (table_hash(tree, ref) if ref.startswith("table:") else None)
        if h is None:
            raise ValueError(f"{ref} does not resolve; fix `from` before accepting")
        seen[ref] = h
    meta["seen"] = seen
    meta["status"] = "accepted"
    meta["review"] = {"note": note.strip(), "on": date.today().isoformat()}
    meta.pop("stale_ok", None)
    (root / n["path"]).write_text(join(meta, body))
    return {"id": node_id, "was_stale": n["stale_inputs"], "seen": seen}
