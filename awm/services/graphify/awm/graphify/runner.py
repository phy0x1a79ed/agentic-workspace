"""Subprocess wrapper around the ``graphify`` CLI for the awm graphify service.

This module owns everything that touches the on-disk world: resolving which
source tree to index ("the active worktree" by default), where the generated
graph lives (under ``$AWM_DIR/services/graphify/`` — never inside the worktree),
and shelling out to the ``graphify`` binary for build / query / path / status /
explain / affected; plus in-memory graph parses for find / refs.

Design notes (from the spike, graphify 0.9.1):
  - **AST-only, no LLM, no key.** ``graphify extract`` requires an API key only
    when the corpus contains doc/paper/image files needing *semantic*
    extraction. The committed ``awm/.graphifyignore`` excludes all doc-class
    extensions, so the corpus is code-only and the build is pure-local tree-
    sitter AST. As a belt-and-braces guarantee we also strip known LLM API-key
    env vars from the build subprocess, so a build can never make a paid call.
  - **Redirected output.** ``extract --out <dir>`` writes ``<dir>/graphify-out/``
    (graph.json + manifest.json + a ``cache/``), leaving the scanned worktree
    untouched. Each build is a *full* build — ``_do_build`` wipes the prior
    ``graphify-out/`` first, because ``extract``'s incremental mode is
    destructive (see ``_do_build``). Read commands (``query``, ``path``,
    ``explain``, ``affected``) take ``--graph <dir>/graphify-out/graph.json``
    and print plain text.
  - **Single graph lock.** All operations that read or write graph.json hold
    ``_GRAPH_LOCK``, so a build subprocess rewriting the file can never race
    against a query subprocess reading it.
  - **Freshness on read.** Read verbs call ``_ensure_fresh`` under the lock,
    rebuilding automatically if the graph is missing or stale. ``status`` is
    the exception — it never rebuilds, just reports staleness.

The handlers in ``hub_adapter`` are thin lambdas over the functions here; the
ServiceAdapter runs each sync handler in a worker thread, so the blocking
``subprocess.run`` calls below never stall the control loop.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from awm.config import SERVICES_DIR

# LLM backend API-key env vars graphify recognises. Stripped from the build
# subprocess env so an AST-only build is guaranteed free/local even if the
# gateway process happens to carry one of these.
_LLM_KEY_VARS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "MOONSHOT_API_KEY",
    "DEEPSEEK_API_KEY",
)

# Shared lock for all graph operations: builds hold it for the full extract
# subprocess; reads hold it for _ensure_fresh + the CLI subprocess.
# Concurrent reads therefore serialize — acceptable for a dev tool; a
# readers-writer lock is a future optimisation.
_GRAPH_LOCK = threading.Lock()

# Spurious "super-hub" labels to strip from the graph after each build, when
# anchored to a shell script (see _prune_noise). ``graphify``'s AST pass turns a
# shell env-var assignment like ``PATH=...`` in run.sh into a node, and every
# Python file that references the name gets an edge into it — ``PATH`` alone
# accrued 113 inbound edges, so BFS (query/path) routes shortest-paths through
# it (``serve → … → PATH → create_session``). Deliberately narrow: only ``PATH``
# today, leaving the smaller sibling env-var hubs (AWM_PORT, GIT_CONFIG_*, …) in
# place. Widen by adding labels here.
_NOISE_HUB_LABELS = frozenset({"PATH"})

# (str(graph_json_path), mtime_float) → parsed graph data.
# Keyed by mtime so a post-build re-read sees the new file automatically.
_GRAPH_CACHE: dict[tuple[str, float], dict[str, Any]] = {}


# -- binary + target + output resolution ------------------------------------


def graphify_bin() -> str:
    """Absolute path to the ``graphify`` executable.

    Prefers ``GRAPHIFY_BIN`` (baked into ``.runtime-env`` by install.sh, which
    keeps graphify's heavy tree-sitter deps in their own env), falling back to
    ``graphify`` on PATH.
    """
    found = os.environ.get("GRAPHIFY_BIN") or shutil.which("graphify")
    if not found:
        raise RuntimeError(
            "graphify binary not found — set GRAPHIFY_BIN or install graphifyy "
            "(see awm/services/graphify/INSTALL.md)"
        )
    return found


def _find_awm_root(start: Path) -> Path:
    """Walk up from ``start`` to the awm source root.

    The root is the ancestor directory named ``awm`` that holds both
    ``services/`` and ``gateway/`` — the top-level package tree this service
    lives in. Under the editable install that resolves to the release worktree;
    under a dev sandbox (``DEV_PYTHONPATH``) it resolves to the active worktree.
    That is exactly "track the tree this instance runs in".
    """
    for d in (start, *start.parents):
        if d.name == "awm" and (d / "services").is_dir() and (d / "gateway").is_dir():
            return d
    raise RuntimeError(f"could not locate the awm source root above {start}")


def resolve_target(target: str | None = None) -> Path:
    """Absolute path of the source tree to index.

    Precedence: explicit ``target`` arg → ``GRAPHIFY_TARGET`` env → the awm
    source root of the worktree this code runs in.
    """
    if target:
        path = Path(target).expanduser().resolve()
    elif os.environ.get("GRAPHIFY_TARGET"):
        path = Path(os.environ["GRAPHIFY_TARGET"]).expanduser().resolve()
    else:
        path = _find_awm_root(Path(__file__).resolve().parent)
    if not path.is_dir():
        raise RuntimeError(f"target is not a directory: {path}")
    return path


def out_dir_for(target: Path) -> Path:
    """Per-target output directory under ``$AWM_DIR/services/graphify/``.

    Keyed by a short hash of the absolute target path so the release tree and a
    dev sandbox's tree never collide in one data dir.
    """
    key = hashlib.sha1(str(target).encode()).hexdigest()[:12]
    return SERVICES_DIR / "graphify" / key


def graph_json(target: Path) -> Path:
    return out_dir_for(target) / "graphify-out" / "graph.json"


def manifest_json(target: Path) -> Path:
    return out_dir_for(target) / "graphify-out" / "manifest.json"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _ast_only_env() -> dict[str, str]:
    env = dict(os.environ)
    for var in _LLM_KEY_VARS:
        env.pop(var, None)
    return env


# -- staleness + freshness --------------------------------------------------


def is_stale(target: str | None = None) -> tuple[bool, int]:
    """Check whether the graph is out of date relative to the source tree.

    Compares each file recorded in ``manifest.json`` (graphify's own incremental
    cache) against its current mtime. Returns ``(stale, changed)`` where
    ``stale`` is True when any tracked file has changed and ``changed`` is the
    count. Returns ``(True, 0)`` when the graph or manifest does not exist yet.

    Never triggers a rebuild — that is ``_ensure_fresh``'s job.
    """
    tgt = resolve_target(target)
    gj = graph_json(tgt)
    mj = manifest_json(tgt)
    if not gj.exists() or not mj.exists():
        return True, 0
    try:
        manifest = json.loads(mj.read_text())
    except (json.JSONDecodeError, OSError):
        return True, 0
    changed = 0
    for relpath, info in manifest.items():
        src = tgt / relpath
        if not src.exists():
            changed += 1
            continue
        if abs(src.stat().st_mtime - info.get("mtime", 0)) > 0.01:
            changed += 1
    return changed > 0, changed


def _prune_noise(out: Path) -> None:
    """Strip spurious super-hub nodes from a freshly-built ``graph.json``.

    Removes every node whose label is in :data:`_NOISE_HUB_LABELS` **and** whose
    source file is a shell script (``.sh``), plus every edge incident to a
    removed node. Anchoring on ``.sh`` keeps a legitimate Python ``PATH`` symbol
    untouched — only the shell-env-var misattribution is dropped. See
    :data:`_NOISE_HUB_LABELS` for why these nodes pollute traversals.

    Operates on the on-disk ``graph.json`` that *every* verb consumes — the CLI
    read-commands (query/path/affected) via ``--graph`` and the in-memory
    find/refs via :func:`_load_graph` — so the fix covers all of them. Edges are
    matched by node **id** (labels are ambiguous). ``hyperedges`` is empty under
    ``--no-cluster`` so it needs no filtering; enabling clustering would require
    extending this. Caller must hold ``_GRAPH_LOCK``.
    """
    gj = out / "graphify-out" / "graph.json"
    if not gj.exists():
        return
    data = json.loads(gj.read_text())
    drop_ids = {
        n["id"]
        for n in data.get("nodes", [])
        if "id" in n
        and n.get("label") in _NOISE_HUB_LABELS
        and str(n.get("source_file", "")).endswith(".sh")
    }
    if not drop_ids:
        return
    data["nodes"] = [n for n in data.get("nodes", []) if n.get("id") not in drop_ids]
    data["edges"] = [
        e
        for e in data.get("edges", [])
        if e.get("source") not in drop_ids and e.get("target") not in drop_ids
    ]
    gj.write_text(json.dumps(data))


def _do_build(tgt: Path) -> subprocess.CompletedProcess:
    """Raw full extract — caller must hold ``_GRAPH_LOCK``.

    ``graphify extract`` is *incremental and destructive*: against an existing
    ``graphify-out/`` (its ``manifest.json`` tracking per-file ast-hashes) it
    re-processes only changed files and writes a ``graph.json`` containing **only
    those** — silently dropping every unchanged file's nodes (a one-file edit
    collapses a 4946-node graph to ~3). It does not merge into the prior graph.
    So a rebuild-in-place would gut the graph rather than refresh it, defeating
    the whole staleness/auto-rebuild trust layer. We force a full build by
    removing the prior output dir first; a clean extract of the awm tree is only
    ~2-3s, so the incremental cache buys nothing worth this correctness hole.

    After a successful extract we :func:`_prune_noise` the graph in place to drop
    spurious shell-env-var super-hubs (see that helper). The ``build()`` summary
    echoes the raw CLI node count, which therefore reads one higher than the
    pruned ``status()`` count — harmless, since ``status`` re-reads the file.
    """
    out = out_dir_for(tgt)
    shutil.rmtree(out / "graphify-out", ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)
    cmd = [graphify_bin(), "extract", str(tgt), "--no-cluster", "--out", str(out)]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, env=_ast_only_env(), timeout=900
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
        raise RuntimeError(f"graphify extract failed (rc={proc.returncode}): {detail}")
    _prune_noise(out)
    return proc


def _ensure_fresh(tgt: Path) -> Path:
    """Rebuild if the graph is missing or stale; return the graph.json path.

    Caller must hold ``_GRAPH_LOCK``.
    """
    gj = graph_json(tgt)
    if not gj.exists():
        _do_build(tgt)
    else:
        stale, _ = is_stale(str(tgt))
        if stale:
            _do_build(tgt)
    return graph_json(tgt)


# -- graph.json in-memory loader (for find / refs) --------------------------


def _load_graph(gj: Path) -> dict[str, Any]:
    """Parse graph.json and return a ``{nodes, by_id, labels, edges}`` dict.

    Results are mtime-keyed in ``_GRAPH_CACHE``; a post-build re-read always
    sees the fresh file. Caller must hold ``_GRAPH_LOCK``.
    """
    mtime = gj.stat().st_mtime
    cache_key = (str(gj), mtime)
    if cache_key not in _GRAPH_CACHE:
        # Evict any earlier entry for this path.
        for k in list(_GRAPH_CACHE):
            if k[0] == str(gj):
                del _GRAPH_CACHE[k]
        data = json.loads(gj.read_text())
        nodes: list[dict] = data.get("nodes", [])
        by_id: dict[str, dict] = {n["id"]: n for n in nodes if "id" in n}
        # Label index: lower-cased label → [node, ...] (ambiguity is common).
        labels: dict[str, list[dict]] = {}
        for n in nodes:
            lbl = n.get("label", "")
            labels.setdefault(lbl.lower(), []).append(n)
        _GRAPH_CACHE[cache_key] = {
            "nodes": nodes,
            "by_id": by_id,
            "labels": labels,
            "edges": data.get("edges", []),
        }
    return _GRAPH_CACHE[cache_key]


# -- CLI output parsers -----------------------------------------------------

_NODE_RE = re.compile(
    r"^NODE\s+(.+?)\s+\[src=(\S+)\s+loc=(L\d+)"
)
_EDGE_RE = re.compile(
    r"^EDGE\s+(.+?)\s+--(\S+).*?-->\s+(.+?)(?:\s*\[|\s*$)"
)
_TRUNCATED_RE = re.compile(r"\(truncated\s+[—-]\s+(\d+)\s+more\s+nodes")


def _parse_traversal(text: str) -> dict[str, Any]:
    """Parse ``graphify query`` / ``graphify affected`` output into structured JSON.

    Extracts NODE and EDGE lines; always retains ``text`` as a fallback. Treats
    unrecognised lines as opaque so a minor CLI format drift degrades gracefully.
    """
    nodes: list[dict] = []
    edges: list[dict] = []
    truncated = False
    extra = 0

    for line in text.splitlines():
        m = _NODE_RE.match(line)
        if m:
            nodes.append({"label": m.group(1).strip(), "file": m.group(2), "line": m.group(3)})
            continue
        m = _EDGE_RE.match(line)
        if m:
            edges.append({
                "source": m.group(1).strip(),
                "relation": m.group(2).strip(),
                "target": m.group(3).strip(),
            })
            continue
        m = _TRUNCATED_RE.search(line)
        if m:
            truncated = True
            extra = int(m.group(1))

    result: dict[str, Any] = {"nodes": nodes, "edges": edges, "text": text}
    if truncated:
        result["truncated"] = True
        result["extra"] = extra
    return result


def _parse_path(text: str) -> dict[str, Any]:
    """Parse ``graphify path`` output into a steps list."""
    steps: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if "-->" in stripped:
            steps = [s.strip() for s in stripped.split("-->")]
    return {"steps": steps, "text": text}


def _parse_explain(text: str) -> dict[str, Any]:
    """Parse ``graphify explain`` output into structured fields."""
    result: dict[str, Any] = {"text": text}
    connections: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("Node:"):
            result["label"] = s.removeprefix("Node:").strip()
        elif s.startswith("ID:"):
            result["id"] = s.removeprefix("ID:").strip()
        elif s.startswith("Source:"):
            parts = s.removeprefix("Source:").strip().split()
            if parts:
                result["file"] = parts[0]
            if len(parts) > 1:
                result["line"] = parts[1]
        elif s.startswith("Type:"):
            result["type"] = s.removeprefix("Type:").strip()
        elif s.startswith("Community:"):
            result["community"] = s.removeprefix("Community:").strip()
        elif s.startswith("Degree:"):
            try:
                result["degree"] = int(s.removeprefix("Degree:").strip())
            except ValueError:
                pass
        elif s.startswith("-->") or s.startswith("<--"):
            connections.append(s)
    if connections:
        result["connections"] = connections
    return result


# -- CLI read helper --------------------------------------------------------


def _run_read_raw(args: list[str], gj: Path) -> str:
    """Run a read-only graphify CLI command; caller must hold ``_GRAPH_LOCK``."""
    proc = subprocess.run(
        [graphify_bin(), *args, "--graph", str(gj)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
        raise RuntimeError(f"graphify {args[0]} failed (rc={proc.returncode}): {detail}")
    return proc.stdout.rstrip()


# -- public operations ------------------------------------------------------


def build(target: str | None = None) -> dict:
    """Build or refresh the AST graph for ``target`` (default: the active tree).

    Runs ``graphify extract <target> --no-cluster --out <data>`` with LLM keys
    stripped. Incremental across runs via graphify's own cache. Returns the
    post-build :func:`status`.
    """
    tgt = resolve_target(target)
    with _GRAPH_LOCK:
        proc = _do_build(tgt)
    result = status(str(tgt))
    tail = (proc.stdout.strip().splitlines() or [""])[-1]
    result["summary"] = tail
    return result


def query(
    question: str,
    context: list[str] | None = None,
    budget: int | None = None,
    target: str | None = None,
) -> dict:
    """BFS traversal of the graph for a natural-language question.

    ``context`` filters the traversal to specific edge types (e.g. ``['call']``),
    reducing result size and avoiding token-budget truncation. ``budget`` sets
    the output token cap (default: graphify's internal default, ~2000).
    """
    tgt = resolve_target(target)
    args = ["query", question]
    if context:
        for c in context:
            args += ["--context", c]
    if budget is not None:
        args += ["--budget", str(budget)]
    with _GRAPH_LOCK:
        gj = _ensure_fresh(tgt)
        text = _run_read_raw(args, gj)
    return {**_parse_traversal(text), "graph": str(gj)}


def path(a: str, b: str, target: str | None = None) -> dict:
    """Shortest path between two node labels."""
    tgt = resolve_target(target)
    with _GRAPH_LOCK:
        gj = _ensure_fresh(tgt)
        text = _run_read_raw(["path", a, b], gj)
    return {**_parse_path(text), "graph": str(gj)}


def explain(node: str, target: str | None = None) -> dict:
    """Plain-language explanation of a node and its immediate connections."""
    tgt = resolve_target(target)
    with _GRAPH_LOCK:
        gj = _ensure_fresh(tgt)
        text = _run_read_raw(["explain", node], gj)
    return {**_parse_explain(text), "graph": str(gj)}


def affected(
    node: str,
    depth: int | None = None,
    relations: list[str] | None = None,
    target: str | None = None,
) -> dict:
    """Transitive impact of changing ``node`` — finds all nodes that would break.

    ``depth`` limits traversal depth (default: 2). ``relations`` restricts which
    edge types to follow (default: calls, imports, references, and friends).
    """
    tgt = resolve_target(target)
    args = ["affected", node]
    if depth is not None:
        args += ["--depth", str(depth)]
    if relations:
        for r in relations:
            args += ["--relation", r]
    with _GRAPH_LOCK:
        gj = _ensure_fresh(tgt)
        text = _run_read_raw(args, gj)
    return {**_parse_traversal(text), "graph": str(gj)}


def find(
    pattern: str,
    limit: int = 50,
    target: str | None = None,
) -> dict:
    """Search for nodes whose label matches ``pattern`` (case-insensitive substring).

    Returns matches ranked exact-first, then substring, up to ``limit``. Always
    returns all matches; use ``limit`` to cap the response size. A ``count``
    field reports the total matches found before truncation.
    """
    tgt = resolve_target(target)
    with _GRAPH_LOCK:
        gj = _ensure_fresh(tgt)
        graph = _load_graph(gj)

    pat = pattern.lower()
    exact: list[dict] = []
    partial: list[dict] = []
    for node in graph["nodes"]:
        label_lower = node.get("label", "").lower()
        if label_lower == pat:
            exact.append(node)
        elif pat in label_lower:
            partial.append(node)

    total = len(exact) + len(partial)
    matches = (exact + partial)[:limit]
    return {
        "pattern": pattern,
        "count": total,
        "truncated": total > limit,
        "matches": [
            {
                "label": n.get("label"),
                "id": n.get("id"),
                "file": n.get("source_file"),
                "line": n.get("source_location"),
                "type": n.get("file_type"),
            }
            for n in matches
        ],
        "graph": str(gj),
    }


def refs(
    node: str,
    direction: str = "both",
    target: str | None = None,
) -> dict:
    """Find callers, callees, and importers of a node (by label substring).

    ``direction`` is ``'in'`` (what calls/imports this node), ``'out'``
    (what this node calls/uses), or ``'both'`` (default).

    Returns ``matched_nodes`` listing every node the pattern resolved to
    (ambiguity is surfaced, not silently collapsed), plus ``in`` / ``out``
    edge lists with the remote node's ``file:line`` and ``relation``.
    """
    if direction not in ("in", "out", "both"):
        raise ValueError(f"direction must be 'in', 'out', or 'both', got {direction!r}")

    tgt = resolve_target(target)
    with _GRAPH_LOCK:
        gj = _ensure_fresh(tgt)
        graph = _load_graph(gj)

    pat = node.lower()
    matched_ids: set[str] = set()
    matched_nodes: list[dict] = []
    for n in graph["nodes"]:
        if pat in n.get("label", "").lower():
            matched_ids.add(n["id"])
            matched_nodes.append({
                "label": n.get("label"),
                "id": n.get("id"),
                "file": n.get("source_file"),
                "line": n.get("source_location"),
            })

    by_id = graph["by_id"]

    def _edge_record(e: dict, role: str) -> dict:
        other_id = e["target"] if role == "out" else e["source"]
        other = by_id.get(other_id, {})
        return {
            "relation": e.get("relation"),
            "context": e.get("context"),
            "other_label": other.get("label"),
            "other_id": other_id,
            "other_file": other.get("source_file"),
            "other_line": other.get("source_location"),
            "source_file": e.get("source_file"),
            "source_line": e.get("source_location"),
        }

    result: dict[str, Any] = {
        "node": node,
        "matched_nodes": matched_nodes,
        "graph": str(gj),
    }
    if direction in ("in", "both"):
        result["in"] = [
            _edge_record(e, "in")
            for e in graph["edges"]
            if e.get("target") in matched_ids
        ]
    if direction in ("out", "both"):
        result["out"] = [
            _edge_record(e, "out")
            for e in graph["edges"]
            if e.get("source") in matched_ids
        ]
    return result


def status(target: str | None = None) -> dict:
    """Whether a graph exists for ``target``, with node/edge counts, build time,
    and a staleness signal.

    Never triggers a rebuild — callers that want freshness should use the read
    verbs (``query``, ``find``, etc.) which call ``_ensure_fresh`` automatically.
    """
    tgt = resolve_target(target)
    out = out_dir_for(tgt)
    gj = graph_json(tgt)
    if not gj.exists():
        return {"target": str(tgt), "exists": False, "out_dir": str(out)}
    stale, changed = is_stale(str(tgt))
    data = json.loads(gj.read_text())
    return {
        "target": str(tgt),
        "exists": True,
        "nodes": len(data.get("nodes", [])),
        "edges": len(data.get("edges", [])),
        "built_at": _iso(gj.stat().st_mtime),
        "stale": stale,
        "changed": changed,
        "graph": str(gj),
        "out_dir": str(out),
    }
