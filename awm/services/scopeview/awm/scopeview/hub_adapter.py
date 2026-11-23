"""Hub adapter for scopeview: view a scope's XGDB raw tables and the chain built on them, at any git ref.

Run via run.sh: python -m awm.scopeview.hub_adapter
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Any

from awm.config import canonical_workspace
from awm.gatewayclient import ServiceAdapter

from awm.scopeview import chain
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
        {"name": "history", "tool": "scopeview_history",
         "params": [P_PROJECT, P_SCOPE, {**P_ID, "required": False, "description": "Node id. Omit for the scope's versions (first-parent commits)."}],
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


def tree(args: dict) -> Tree:
    return Tree(scope_root(args["project"], args["scope"]), args.get("ref"))


def handler(fn):
    async def run(args: dict):
        return await asyncio.to_thread(fn, args)
    return run


def check(a):
    g = chain.build(tree(a), gate=bool(a.get("gate")))
    return {"ok": g["ok"], "commit": g["commit"], "problems": g["problems"],
            "stale": [n["id"] for n in g["nodes"] if n.get("stale")]}


HANDLERS = {
    "scopes": handler(lambda a: {"scopes": find_scopes()}),
    "graph": handler(lambda a: chain.build(tree(a))),
    "node": handler(lambda a: chain.node(tree(a), a["id"])),
    "history": handler(lambda a: {"commits": chain.history(tree(a), a.get("id"))}),
    "new": handler(lambda a: chain.new(scope_root(a["project"], a["scope"]), a["stage"], a["from"], a["title"], a.get("id"))),
    "accept": handler(lambda a: chain.accept(scope_root(a["project"], a["scope"]), a["id"], a["note"])),
    "check": handler(check),
}


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    await ServiceAdapter("scopeview", API_MANIFEST, HANDLERS).run()


if __name__ == "__main__":
    asyncio.run(main())
