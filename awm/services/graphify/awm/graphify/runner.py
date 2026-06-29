"""Subprocess wrapper around the ``graphify`` CLI for the awm graphify service.

This module owns everything that touches the on-disk world: resolving which
source tree to index ("the active worktree" by default), where the generated
graph lives (under ``$AWM_DIR/services/graphify/`` — never inside the worktree),
and shelling out to the ``graphify`` binary for build / query / path / status.

Design notes (from the spike, graphify 0.9.1):
  - **AST-only, no LLM, no key.** ``graphify extract`` requires an API key only
    when the corpus contains doc/paper/image files needing *semantic*
    extraction. The committed ``awm/.graphifyignore`` excludes all doc-class
    extensions, so the corpus is code-only and the build is pure-local tree-
    sitter AST. As a belt-and-braces guarantee we also strip known LLM API-key
    env vars from the build subprocess, so a build can never make a paid call.
  - **Redirected output.** ``extract --out <dir>`` writes ``<dir>/graphify-out/``
    (graph.json + manifest.json + an incremental ``cache/``), leaving the
    scanned worktree untouched. Re-running a build is incremental via that
    cache. ``query`` / ``path`` read ``--graph <dir>/graphify-out/graph.json``
    and print plain text.

The handlers in ``hub_adapter`` are thin lambdas over the functions here; the
ServiceAdapter runs each sync handler in a worker thread, so the blocking
``subprocess.run`` calls below never stall the control loop.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

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

# One build at a time per process: graphify writes a single graphify-out/ tree
# per target and two concurrent extracts against the same --out would race.
_BUILD_LOCK = threading.Lock()


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


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _ast_only_env() -> dict[str, str]:
    env = dict(os.environ)
    for var in _LLM_KEY_VARS:
        env.pop(var, None)
    return env


# -- operations -------------------------------------------------------------


def build(target: str | None = None) -> dict:
    """Build/refresh the AST graph for ``target`` (default: the active tree).

    Runs ``graphify extract <target> --no-cluster --out <data>`` with LLM keys
    stripped. Incremental across runs via graphify's own cache. Returns the
    post-build :func:`status`.
    """
    tgt = resolve_target(target)
    out = out_dir_for(tgt)
    out.mkdir(parents=True, exist_ok=True)
    cmd = [graphify_bin(), "extract", str(tgt), "--no-cluster", "--out", str(out)]
    with _BUILD_LOCK:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, env=_ast_only_env(), timeout=900
        )
    if proc.returncode != 0:
        detail = (proc.stderr.strip() or proc.stdout.strip() or "unknown error")
        raise RuntimeError(f"graphify extract failed (rc={proc.returncode}): {detail}")
    result = status(str(tgt))
    # graphify's last stdout line is the "wrote … N nodes, M edges" summary.
    tail = (proc.stdout.strip().splitlines() or [""])[-1]
    result["summary"] = tail
    return result


def _require_graph(tgt: Path) -> Path:
    gj = graph_json(tgt)
    if not gj.exists():
        raise RuntimeError(
            f"no graph for {tgt} yet — run graphify_build first "
            f"(expected {gj})"
        )
    return gj


def _run_read(args: list[str], gj: Path) -> str:
    proc = subprocess.run(
        [graphify_bin(), *args, "--graph", str(gj)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        detail = (proc.stderr.strip() or proc.stdout.strip() or "unknown error")
        raise RuntimeError(f"graphify {args[0]} failed (rc={proc.returncode}): {detail}")
    return proc.stdout.rstrip()


def query(question: str, target: str | None = None) -> dict:
    """BFS traversal of the graph for a natural-language question."""
    tgt = resolve_target(target)
    gj = _require_graph(tgt)
    return {"result": _run_read(["query", question], gj), "graph": str(gj)}


def path(a: str, b: str, target: str | None = None) -> dict:
    """Shortest path between two node labels."""
    tgt = resolve_target(target)
    gj = _require_graph(tgt)
    return {"result": _run_read(["path", a, b], gj), "graph": str(gj)}


def status(target: str | None = None) -> dict:
    """Whether a graph exists for ``target``, with node/edge counts + build time.

    Stat-only — never triggers a rebuild.
    """
    tgt = resolve_target(target)
    out = out_dir_for(tgt)
    gj = graph_json(tgt)
    if not gj.exists():
        return {"target": str(tgt), "exists": False, "out_dir": str(out)}
    data = json.loads(gj.read_text())
    return {
        "target": str(tgt),
        "exists": True,
        "nodes": len(data.get("nodes", [])),
        "edges": len(data.get("edges", [])),
        "built_at": _iso(gj.stat().st_mtime),
        "graph": str(gj),
        "out_dir": str(out),
    }
