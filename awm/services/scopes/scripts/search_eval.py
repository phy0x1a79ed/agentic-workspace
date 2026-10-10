"""Measure how well awm search finds known documents.

Runs a labelled query set against a node's live search verbs, or against any
``search(query, service, filter) -> [id, ...]`` callable, and reports
recall@k, MRR@k and nDCG@k per subset plus the queries that missed.

A query set is JSONL, one query per line::

    {"id": "q1", "subset": "journal-needle", "service": "post",
     "query": "...", "filter": {"project": "awm", "kind": "journal"},
     "relevant": ["<doc id>", ...]}

Query sets name real document ids and paraphrase private content, so they live
in the node's runtime dir (``<AWM_DIR>/search-eval/``), never in the repo.
Stdlib only, so ``--remote`` can pipe this file into a peer's system python.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shlex
import subprocess
import sys
import time
import urllib.request
from collections import defaultdict
from typing import Callable, Iterable

SearchFn = Callable[[str, str, dict], list[str]]

# service -> (tool name, argument builder, result list key)
_LIVE = {
    "post": ("scope_fetch", lambda q, f, k: {"query": q, "limit": k, **f}, "posts"),
    "scope": ("scope_search", lambda q, f, k: {"query": q, "limit": k, **f}, "scopes"),
    "project": ("project_search", lambda q, f, k: {"query": q, "limit": k, **f}, "projects"),
    "note": ("notes_search", lambda q, f, k: {"semantic": q, "k": k, **f}, "results"),
    "writing": ("writing_search", lambda q, f, k: {"semantic": q, "k": k, **f}, "results"),
    "precedence": ("precedence_search", lambda q, f, k: {"context": q, "k": k, **f}, "results"),
}


def _doc_id(service: str, row: dict) -> str:
    if service == "scope":
        return f"{row['project']}/{row['scope']}"
    if service == "project":
        return row.get("name") or row.get("id")
    return str(row["id"])


def live_search(base_url: str = "http://127.0.0.1:7819", k: int = 10) -> SearchFn:
    """A SearchFn that calls the node's gateway ``/invoke``."""

    def search(query: str, service: str, flt: dict) -> list[str]:
        tool, build, key = _LIVE[service]
        body = json.dumps({"name": tool, "args": build(query, flt, k)}).encode()
        req = urllib.request.Request(f"{base_url}/invoke", body,
                                     {"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            env = json.load(r)
        res = env.get("result", env)
        if isinstance(res, str):
            res = json.loads(res)
        rows = res.get(key, []) if isinstance(res, dict) else res
        return [_doc_id(service, row) for row in rows]

    return search


def load(path: str) -> list[dict]:
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _metrics(ranked: list[str], relevant: set[str], k: int) -> dict[str, float]:
    top = ranked[:k]
    hits = [i for i, d in enumerate(top) if d in relevant]
    dcg = sum(1 / math.log2(i + 2) for i in hits)
    idcg = sum(1 / math.log2(i + 2) for i in range(min(len(relevant), k)))
    return {
        "recall": len(hits) / len(relevant),
        "mrr": 1 / (hits[0] + 1) if hits else 0.0,
        "ndcg": dcg / idcg if idcg else 0.0,
    }


def evaluate(queries: Iterable[dict], search: SearchFn, k: int = 10) -> dict:
    """Run every query through ``search``; return per-subset means and misses."""
    per: dict[str, list[dict]] = defaultdict(list)
    misses, errors, latencies = [], [], []
    for q in queries:
        rel = set(q["relevant"])
        t0 = time.perf_counter()
        try:
            ranked = search(q["query"], q["service"], q.get("filter") or {})
        except Exception as exc:  # a failing query scores zero, visibly
            errors.append({"id": q["id"], "error": repr(exc)[:200]})
            ranked = []
        latencies.append(time.perf_counter() - t0)
        m = _metrics(ranked, rel, k)
        for s in (q["subset"], "all"):
            per[s].append(m)
        if m["recall"] < 1:
            rank = next((i + 1 for i, d in enumerate(ranked) if d in rel), None)
            misses.append({"id": q["id"], "subset": q["subset"], "query": q["query"],
                           "rank": rank, "returned": len(ranked)})
    summary = {
        s: {"n": len(ms), **{f"{key}@{k}": round(sum(m[key] for m in ms) / len(ms), 3)
                             for key in ("recall", "mrr", "ndcg")}}
        for s, ms in sorted(per.items())
    }
    lat = sorted(latencies)
    p95 = lat[max(0, math.ceil(0.95 * len(lat)) - 1)] if lat else 0.0
    return {"summary": summary, "p95_s": round(p95, 3), "misses": misses, "errors": errors}


def report(result: dict, show_misses: bool = True) -> str:
    lines = [f"{'subset':<22}{'n':>4}  recall   mrr    ndcg"]
    for s, v in result["summary"].items():
        vals = {k.split("@")[0]: v[k] for k in v if k != "n"}
        lines.append(f"{s:<22}{v['n']:>4}  {vals['recall']:.3f}  {vals['mrr']:.3f}  {vals['ndcg']:.3f}")
    lines.append(f"p95 latency {result['p95_s']}s")
    for e in result["errors"]:
        lines.append(f"ERROR {e['id']}: {e['error']}")
    if show_misses:
        for m in result["misses"]:
            lines.append(f"miss {m['id']:<14} rank={m['rank']} [{m['subset']}] {m['query']}")
    return "\n".join(lines)


def _default_set() -> str:
    root = os.environ.get("AWM_DIR") or os.path.expanduser("~/agentic_workspace/.awm")
    return os.path.join(root, "search-eval", "queries.jsonl")


def _run_remote(host: str, argv: list[str]) -> int:
    """Pipe this script to ``host``'s python3 and run it there against its own set."""
    src = open(__file__).read()
    cmd = f"python3 - {' '.join(shlex.quote(a) for a in argv)}"
    return subprocess.run(["ssh", host, cmd], input=src, text=True).returncode


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--set", default=None, help="query set JSONL (default <AWM_DIR>/search-eval/queries.jsonl)")
    ap.add_argument("--url", default="http://127.0.0.1:7819", help="gateway to query")
    ap.add_argument("--remote", help="ssh host: run there, against that node's own query set")
    ap.add_argument("--subset", action="append", help="only these subsets (repeatable)")
    ap.add_argument("-k", type=int, default=10)
    ap.add_argument("--json", action="store_true", help="print the raw result as JSON")
    ap.add_argument("--no-misses", action="store_true")
    a = ap.parse_args(argv)
    if a.remote:
        rest = [x for i, x in enumerate(argv)
                if x != "--remote" and (i == 0 or argv[i - 1] != "--remote")]
        return _run_remote(a.remote, rest)
    queries = load(a.set or _default_set())
    if a.subset:
        queries = [q for q in queries if q["subset"] in a.subset]
    result = evaluate(queries, live_search(a.url, a.k), a.k)
    print(json.dumps(result, indent=1) if a.json else report(result, not a.no_misses))
    return 0


if __name__ == "__main__":
    sys.exit(main())
