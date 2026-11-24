"""Hub adapter for scopeview: view a scope's XGDB raw tables and the chain built on them, at any git ref.

Run via run.sh: python -m awm.scopeview.hub_adapter
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
from pathlib import Path
from typing import Any

from awm.config import canonical_workspace
from awm.gatewayclient import ServiceAdapter

from awm.scopeview import chain, chat, layout, trace
from awm.scopeview.tree import Tree, git

log = logging.getLogger("awm.scopeview")

P_PROJECT = {"name": "project", "required": True, "description": "Project, the directory under projects/."}
P_SCOPE = {"name": "scope", "required": True, "description": "Scope, the worktree path under the project (may nest)."}
P_REF = {"name": "ref", "required": False, "description": "Git branch, tag or commit to read. Default: the scope's worktree as it stands."}
P_ID = {"name": "id", "required": True, "description": "Node id: a chain node id, or table:<path under data/>."}

API_MANIFEST: dict[str, Any] = {
    "functions": [
        {"name": "scopes", "tool": "scopeview_scopes", "params": [],
         "description": "Scopes on this node with a data/ or chain/ folder, with their XGDB datasets."},
        {"name": "graph", "tool": "scopeview_graph", "params": [P_PROJECT, P_SCOPE, P_REF],
         "description": "The scope's graph: raw tables from data/<dataset>/ (XGDB), chain nodes from chain/, edges, stale flags and check problems."},
        {"name": "node", "tool": "scopeview_node", "params": [P_PROJECT, P_SCOPE, P_ID, P_REF],
         "description": "One node's content: a table's columns and first rows, a figure's Plotly JSON, a text node's markdown."},
        {"name": "layout", "tool": "scopeview_layout", "params": [P_PROJECT, P_SCOPE, P_ID, P_REF],
         "description": "A node's provenance as metasmith DAG geometry: its ancestors down to the raw tables, and its direct children."},
        {"name": "trace", "tool": "scopeview_trace",
         "params": [P_PROJECT, P_SCOPE,
                    {"name": "cites", "type": "array", "required": True,
                     "description": "Citations to follow: id, id#n (nth list item), or id?col=v1,v2 (table rows, or a figure's marks by its row key)."},
                    P_REF],
         "description": "Follow citations down the chain to the table rows behind them: the nodes passed, the cited items' text, and the rows (capped)."},
        {"name": "history", "tool": "scopeview_history",
         "params": [P_PROJECT, P_SCOPE, {**P_ID, "required": False, "description": "Node id. Omit for the scope's versions (first-parent commits)."}, P_REF],
         "description": "Commits that touched a node, or the scope's versions when no id is given."},
        {"name": "new", "tool": "scopeview_new",
         "params": [P_PROJECT, P_SCOPE,
                    {"name": "stage", "required": True, "description": "question, figure, interpretation, point or prose."},
                    {"name": "from", "type": "array", "required": True, "description": "Input node ids or table:<path under data/>."},
                    {"name": "title", "required": True, "description": "One line: the question, the figure's subject, the claim."},
                    {"name": "id", "required": False, "description": "Node id. Default: minted from the stage and title."}],
         "description": "Create a chain node file in the scope's worktree with its front matter. Write the body with ordinary edits."},
        {"name": "accept", "tool": "scopeview_accept",
         "params": [P_PROJECT, P_SCOPE, P_ID,
                    {"name": "note", "required": True, "description": "What you re-read, and why the node still holds."}],
         "description": "Mark a chain node reviewed against its current inputs: restamp `seen` and store the note. Re-read the node first."},
        {"name": "check", "tool": "scopeview_check",
         "params": [P_PROJECT, P_SCOPE, P_REF,
                    {"name": "gate", "type": "boolean", "required": False, "description": "Treat stale nodes as errors, as a merge into the study branch does."}],
         "description": "Validate the chain: ids, inputs, stage order, cycles, artifact pins, and with gate, staleness."},
        {"name": "chat", "tool": "scopeview_chat",
         "params": [P_PROJECT, P_SCOPE,
                    {"name": "after", "type": "number", "required": False, "description": "Return messages after this seq. Default 0."},
                    {"name": "wait", "type": "number", "required": False, "description": "Seconds to wait for a new message (at most 25)."}],
         "description": "The conversation with the scope's room seat: its channel room's messages after a seq, and the next seq. The first call declares the seat on this node."},
        {"name": "say", "tool": "scopeview_say",
         "params": [P_PROJECT, P_SCOPE,
                    {"name": "text", "required": True, "description": "The message to the scope's room seat."},
                    {"name": "context", "type": "object", "required": False,
                     "description": "What the message is about: ref, node, cite. Sent as its first line."}],
         "description": "Write to the scope's room seat from the page. Its agent starts on the message if it is not running."},
    ],
    "emitters": [],
    "sessions": [],
}


def scope_root(project: str, scope: str) -> Path:
    projects = (canonical_workspace() / "projects").resolve()
    root = (projects / project / scope).resolve()
    if projects not in root.parents or not (root / ".git").exists():
        raise ValueError(f"{project}/{scope} is not a scope worktree on this node")
    return root


def find_scopes() -> list[dict]:
    projects = (canonical_workspace() / "projects").resolve()
    out = []
    for bare in sorted(projects.glob("*/.bare")):
        try:
            listing = git(bare, "worktree", "list", "--porcelain")
        except subprocess.CalledProcessError:
            continue
        for ln in listing.splitlines():
            if not ln.startswith("worktree "):
                continue
            root = Path(ln[9:])
            if projects not in root.parents or not root.is_dir():
                continue
            rel = root.relative_to(projects).parts
            datasets = sorted(p.parent.name for p in (root / "data").glob("*/samples.tsv"))
            if datasets or (root / "chain").is_dir():
                out.append({"project": rel[0], "scope": "/".join(rel[1:]), "datasets": datasets, "chain": (root / "chain").is_dir()})
    return out


_read: dict[tuple[Path, str], tuple[Tree, dict]] = {}
_read_lock = threading.Lock()


def read(args: dict) -> tuple[Tree, dict]:
    """The scope's tree and graph at the ref, built once per commit (or worktree state) and shared by every read verb."""
    t = Tree(scope_root(args["project"], args["scope"]), str(args["ref"]) if args.get("ref") else None)
    key = (t.root, t.stamp)
    if hit := _read.get(key):
        return hit
    built = (t, chain.build(t))
    with _read_lock:
        if len(_read) >= 16:
            del _read[next(iter(_read))]
        _read[key] = built
    return built


def handler(fn):
    async def run(args: dict):
        return await asyncio.to_thread(fn, args)
    return run


def check(a):
    g = read(a)[1]
    gate = bool(a.get("gate"))
    return {"ok": not g["problems"] if gate else g["ok"], "commit": g["commit"],
            "problems": [p for p in g["problems"] if gate or p["code"] != "stale"],
            "stale": [n["id"] for n in g["nodes"] if n.get("stale")]}


async def chat_read(a):
    scope_root(a["project"], a["scope"])
    return await chat.read(a["project"], a["scope"], int(a.get("after") or 0), float(a.get("wait") or 0))


async def chat_say(a):
    scope_root(a["project"], a["scope"])
    return await chat.say(a["project"], a["scope"], a["text"], a.get("context") or {})


HANDLERS = {
    "chat": chat_read,
    "say": chat_say,
    "scopes": handler(lambda a: {"scopes": find_scopes()}),
    "graph": handler(lambda a: read(a)[1]),
    "node": handler(lambda a: chain.node(*read(a), a["id"])),
    "layout": handler(lambda a: layout.provenance(read(a)[1], a["id"])),
    "trace": handler(lambda a: trace.trace(*read(a), a["cites"])),
    "history": handler(lambda a: {"commits": chain.history(*read(a), a.get("id"))}),
    "new": handler(lambda a: chain.new(scope_root(a["project"], a["scope"]), a["stage"], a["from"], a["title"], a.get("id"))),
    "accept": handler(lambda a: chain.accept(scope_root(a["project"], a["scope"]), a["id"], a["note"])),
    "check": handler(check),
}


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    await ServiceAdapter("scopeview", API_MANIFEST, HANDLERS).run()


if __name__ == "__main__":
    asyncio.run(main())
