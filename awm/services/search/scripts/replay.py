"""Replay the judgement set through two search services, and check grep parity, for the regression gate.

    replay.py seed gold PARQUET            semdb's quote queries, each judged against its gold papers
    replay.py seed kbeval QUERIES --search-db DB
                                           kb's single-hop and paraphrase sets, targets resolved to journal ids
    replay.py seed sessions DIR...         a search hit that a session fetched with get, after that query
    replay.py seed jsonl FILE              one judgement per line: query, source, id, verdict, section, note
    replay.py run --old URL --new URL      recall of every judged query on both, with the gate's verdict
    replay.py run --judgements FILE...     the same, judged by seed-shaped files instead of --new's set
    replay.py grep --new URL --search-db DB [--papers-db DB]
                                           literal substrings sampled from the indexed text, found in `exact`

URLs are gateway bases such as http://127.0.0.1:7819 (prod) or a dev sandbox's. Judgements are read
from and seeded into --new (default: AWM_HUB_URL, else prod). A paper matches across engines by its
DOI, because the old engine keys papers by Zotero key and the new one by DOI.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import random
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import httpx

PROD = "http://127.0.0.1:7819"
SECTIONS = ("exact", "related", "graph")
#: semdb's own max-pooling baseline on its gold set (criterion 5).
GOLD_FLOOR = 0.389
DOI = re.compile(r"(?:doi\.org/|^doi:)(10\.\S+)", re.I)


def call(hub: str, fn: str, args: dict[str, Any], timeout: float = 120.0, tries: int = 4) -> Any:
    """One search verb; a locked database, which a sync pass can hold for a moment, is retried."""
    for attempt in range(tries):
        resp = httpx.post(f"{hub.rstrip('/')}/svc/search/fn/{fn}", json=args, timeout=timeout)
        if resp.status_code < 300:
            return resp.json()
        if "database is locked" not in resp.text or attempt == tries - 1:
            raise RuntimeError(f"search {fn}: HTTP {resp.status_code}: {resp.text[:300]}")
        time.sleep(2 ** attempt)


def identity(source: str, doc_id: str, url: str = "") -> str:
    """One name for a document on both engines: ``doi:<doi>`` for a paper, else ``source:id``."""
    for text in (doc_id, url):
        found = DOI.search(text or "")
        if found:
            return "doi:" + found.group(1).lower().rstrip(".")
    return f"{source}:{doc_id}"


def ranked(answer: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """``(identity, section, hit)`` in reading order: the sections in turn, or an old answer's ``hits``."""
    out = []
    for section in SECTIONS if any(k in answer for k in SECTIONS) else ("hits",):
        for hit in answer.get(section) or []:
            out.append((identity(hit["source"], str(hit["id"]), hit.get("url", "")),
                        "related" if section == "hits" else section, hit))
    return out


def ask(hub: str, query: str, limit: int) -> dict[str, Any]:
    try:
        return call(hub, "query", {"query": query, "limit": limit, "passages": 1, "peers": False})
    except RuntimeError as exc:
        if "passages" not in str(exc) and "Unknown" not in str(exc):
            raise
        return call(hub, "query", {"query": query, "limit": limit})


# -- seeding ---------------------------------------------------------------


def seed_gold(path: str) -> Iterable[dict[str, Any]]:
    import pandas as pd
    for row in pd.read_parquet(path).to_dict("records"):
        papers = row["gold_papers"]
        for doi in (ast.literal_eval(papers) if isinstance(papers, str) else papers):
            yield {"query": row["text"], "source": "kb", "id": doi, "verdict": "relevant", "set": "gold",
                   "note": f"{row['qid']} {row['variant']}"}


def seed_kbeval(path: str, search_db: str) -> Iterable[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{search_db}?mode=ro", uri=True)
    sets = json.loads(Path(path).read_text())
    for name in ("single_hop", "paraphrase"):
        for item in sets[name]:
            for prefix in item["targets"]:
                rows = conn.execute("SELECT 'journals', doc_id FROM documents WHERE source='journals'"
                                    " AND doc_id LIKE ?", (prefix + "%",)).fetchall()
                paper = conn.execute("SELECT url FROM documents WHERE source='kb' AND doc_id=?", (prefix,)).fetchone()
                if paper and DOI.search(paper[0]):
                    rows = [("kb", identity("kb", "", paper[0]))]
                if len(rows) != 1:
                    print(f"skip {prefix}: {len(rows)} journal or paper ids match", file=sys.stderr)
                    continue
                yield {"query": item["q"], "source": rows[0][0], "id": rows[0][1], "verdict": "relevant",
                       "set": f"kb-{name.replace('_', '-')}"}


def _tool_uses(path: Path) -> Iterable[tuple[str, Any]]:
    """``("use", (id, verb, args))`` and ``("result", (id, text))`` in transcript order."""
    with path.open(errors="replace") as fh:
        for line in fh:
            if "mcp__awm__search" not in line and "tool_result" not in line:
                continue
            try:
                content = json.loads(line).get("message", {}).get("content")
            except json.JSONDecodeError:
                continue
            for part in content if isinstance(content, list) else []:
                if part.get("type") == "tool_use" and part.get("name") == "mcp__awm__search":
                    args = part.get("input") or {}
                    yield "use", (part.get("id"), args.get("verb"), args.get("args") or {})
                elif part.get("type") == "tool_result":
                    body = part.get("content")
                    text = body if isinstance(body, str) else json.dumps(body)
                    yield "result", (part.get("tool_use_id"), text)


def seed_sessions(dirs: list[str]) -> Iterable[dict[str, Any]]:
    """A hit fetched with ``get`` right after the query that returned it, before the next query, counts as relevant."""
    seen: set[tuple[str, str, str]] = set()
    for path in (p for d in dirs for p in sorted(Path(d).expanduser().glob("**/*.jsonl"))):
        pending: dict[str, str] = {}
        last: tuple[str, str] | None = None
        for kind, item in _tool_uses(path):
            if kind == "use":
                use_id, verb, args = item
                if verb == "query" and args.get("query"):
                    pending[use_id] = str(args["query"])
                    last = None
                elif verb == "get" and last and str(args.get("id", "")) in last[1]:
                    key = (last[0], str(args.get("source", "")), str(args["id"]))
                    if key not in seen and key[1]:
                        seen.add(key)
                        yield {"query": key[0], "source": key[1], "id": key[2], "verdict": "relevant",
                               "set": "sessions", "note": path.stem}
            elif item[0] in pending:
                last = (pending.pop(item[0]), item[1])


def seed_jsonl(path: str) -> Iterable[dict[str, Any]]:
    for line in Path(path).read_text().splitlines():
        if line.strip():
            yield json.loads(line)


def seed(args: argparse.Namespace) -> None:
    rows = {"gold": lambda: seed_gold(args.path[0]),
            "kbeval": lambda: seed_kbeval(args.path[0], args.search_db),
            "sessions": lambda: seed_sessions(args.path),
            "jsonl": lambda: seed_jsonl(args.path[0])}[args.kind]()
    count = 0
    for row in rows:
        if args.dry_run:
            print(json.dumps(row))
        else:
            call(args.new, "judge", row)
        count += 1
    print(f"{'would seed' if args.dry_run else 'seeded'} {count} judgements", file=sys.stderr)


# -- replay ----------------------------------------------------------------


def judged(hub: str, only: str, files: list[str]) -> dict[str, list[dict[str, Any]]]:
    """The judgements by query, from the files when given, else from the hub's judgement set."""
    if files:
        rows = [{"doc_id": r["id"], "collection": r.get("set", ""), "section": r.get("section", ""), **r}
                for f in files for r in seed_jsonl(f)]
    else:
        rows = call(hub, "judgements", {})["judgements"]
    by_query: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if not only or row["collection"] == only:
            by_query[row["query"]].append(row)
    return by_query


def score(rows: list[dict[str, Any]], answer: dict[str, Any]) -> dict[str, Any]:
    order = ranked(answer)
    place = {}
    for rank, (ident, section, _) in enumerate(order, 1):
        place.setdefault(ident, (rank, section))
    relevant = [r for r in rows if r["verdict"] == "relevant"]
    found = {identity(r["source"], r["doc_id"]): place.get(identity(r["source"], r["doc_id"])) for r in relevant}
    wrong_section = [k for r in relevant if r["section"]
                     for k in [identity(r["source"], r["doc_id"])] if found[k] and found[k][1] != r["section"]]
    noise = sum(1 for r in rows if r["verdict"] == "irrelevant"
                and (place.get(identity(r["source"], r["doc_id"])) or (99,))[0] <= 10)
    sectioned = any(k in answer for k in SECTIONS)
    bare = sum(1 for _, _, hit in order if sectioned
               and not all(isinstance(p.get("start"), int) and isinstance(p.get("end"), int)
                           for p in hit.get("passages") or [{}]))
    return {"found": found, "wrong_section": wrong_section, "noise": noise, "unquoted": bare}


def recall(found: dict[str, Any], cut: int) -> float:
    return sum(1 for v in found.values() if v and v[0] <= cut) / len(found) if found else 1.0


def run(args: argparse.Namespace) -> int:
    by_query = judged(args.new, args.set, args.judgements)
    per_set: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    lost, misplaced, unquoted, rows_out = [], [], 0, []
    started = time.time()
    for query, rows in sorted(by_query.items()):
        if not any(r["verdict"] == "relevant" for r in rows):
            continue
        new = score(rows, ask(args.new, query, 20))
        old = score(rows, ask(args.old, query, 20)) if args.old else None
        name = rows[0]["collection"] or "marks"
        per_set[name]["new@10"].append(recall(new["found"], 10))
        per_set[name]["new@20"].append(recall(new["found"], 20))
        if old:
            per_set[name]["old@10"].append(recall(old["found"], 10))
            per_set[name]["old@20"].append(recall(old["found"], 20))
            lost += [(query, k) for k, v in old["found"].items() if v and v[0] <= 20 and not
                     (new["found"][k] and new["found"][k][0] <= 20)]
        misplaced += [(query, k) for k in new["wrong_section"]]
        unquoted += new["unquoted"]
        rows_out.append({"query": query, "set": name, "new": new, "old": old})
    means = {s: {k: round(sum(v) / len(v), 3) for k, v in m.items()} | {"queries": len(m["new@10"])}
             for s, m in sorted(per_set.items())}
    every = [x for m in per_set.values() for x in m["new@10"]]
    every_old = [x for m in per_set.values() for x in m.get("old@10", [])]
    gate = {
        "no lost recall (4)": None if not args.old else bool(
            len(every) >= 100 and sum(every) >= sum(every_old) and not lost),
        "quotes (5)": None if "gold" not in means else bool(
            means["gold"]["new@20"] >= GOLD_FLOOR and not unquoted),
        "sections": not misplaced,
    }
    regressed = sorted(s for s, m in means.items() if "old@10" in m and m["new@10"] < m["old@10"])
    report = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "seconds": round(time.time() - started),
              "old": args.old, "new": args.new, "sets": means, "regressed_sets": regressed,
              "lost": lost, "misplaced": misplaced,
              "unquoted_hits": unquoted, "judged_queries": len(every), "gate": gate, "queries": rows_out}
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps({k: v for k, v in report.items() if k != "queries"}, indent=1, default=str))
    return 0 if all(v is not False for v in gate.values()) else 1


# -- grep parity -----------------------------------------------------------

HASH = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")
PATH = re.compile(r"(?:~|\.{0,2})(?:/[\w.@-]+){2,}")
ERROR = re.compile(r"^.*(?:Error|Exception|Traceback|error:|failed).*$", re.M)


def _sentence(text: str, rng: random.Random) -> str:
    starts = [m.end() for m in re.finditer(r"(?:^|\s)", text)] or [0]
    for _ in range(20):
        start = rng.choice(starts)
        piece = text[start:start + rng.randint(30, 80)].rsplit(" ", 1)[0].strip()
        if len(piece) >= 24 and "\n" not in piece:
            return piece
    return ""


def _pick(kind: str, text: str, rng: random.Random) -> str:
    if kind == "sentence":
        return _sentence(text, rng)
    found = {"hash": HASH, "path": PATH, "error": ERROR}[kind].findall(text)
    found = [f.strip()[:80] for f in found if len(f.strip()) >= 7]
    return rng.choice(found) if found else ""


def samples(args: argparse.Namespace, rng: random.Random) -> list[dict[str, str]]:
    plan = [("hash", 0.15), ("path", 0.15), ("error", 0.15), ("sentence", 0.15)]
    if args.papers_db:
        plan.append(("paper", 0.40))
    total = sum(w for _, w in plan)
    out: list[dict[str, str]] = []
    local = sqlite3.connect(f"file:{args.search_db}?mode=ro", uri=True)
    docs = local.execute("SELECT source, doc_id, text FROM documents WHERE length(text) > 40").fetchall()
    papers = sqlite3.connect(f"file:{args.papers_db}?mode=ro", uri=True) if args.papers_db else None
    top = papers.execute("SELECT max(id) FROM passage").fetchone()[0] if papers else 0
    for kind, weight in plan:
        want = round(args.n * weight / total)
        tries = 0
        while want and tries < want * 200:
            tries += 1
            if kind == "paper":
                row = papers.execute("SELECT paper_id, text FROM passage WHERE id >= ? LIMIT 1",
                                     (rng.randint(1, top),)).fetchone()
                source, doc_id, text = "kb", row[0], row[1]
                literal = _sentence(text, rng)
            else:
                source, doc_id, text = rng.choice(docs)
                literal = _pick(kind, text, rng)
            if literal and literal in text:
                out.append({"kind": kind, "source": source, "id": doc_id, "literal": literal})
                want -= 1
    return out


def grep(args: argparse.Namespace) -> int:
    rng = random.Random(args.seed)
    picked = samples(args, rng)
    tally: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    misses, crowded, latencies = [], [], []
    for s in picked:
        start = time.time()
        answer = call(args.new, "query", {"query": s["literal"], "sources": [s["source"]], "limit": 100,
                                          "passages": 1})
        latencies.append(time.time() - start)
        exact = [identity(h["source"], str(h["id"]), h.get("url", "")) for h in answer.get("exact") or []]
        hit = identity(s["source"], s["id"]) in exact
        tally[s["kind"]][0] += hit
        tally[s["kind"]][1] += 1
        if not hit:
            (crowded if len(exact) >= 100 else misses).append(s)
    latencies.sort()
    report = {"found": sum(v[0] for v in tally.values()), "asked": len(picked),
              "by_kind": {k: f"{v[0]}/{v[1]}" for k, v in sorted(tally.items())},
              "p50_ms": round(1000 * latencies[len(latencies) // 2]) if latencies else None,
              "p95_ms": round(1000 * latencies[int(len(latencies) * 0.95)]) if latencies else None,
              "crowded": crowded, "misses": misses}
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1))
    print(json.dumps({**report, "crowded": len(crowded), "misses": misses[:20]}, indent=1))
    return 0 if not misses and not crowded else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--new", default=os.environ.get("AWM_HUB_URL") or PROD)
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed")
    s.add_argument("kind", choices=["gold", "kbeval", "sessions", "jsonl"])
    s.add_argument("path", nargs="+")
    s.add_argument("--search-db")
    s.add_argument("--dry-run", action="store_true")
    r = sub.add_parser("run")
    r.add_argument("--old", help="The engine to compare against. Omit to score --new alone.")
    r.add_argument("--set", default="", help="Only this judgement set.")
    r.add_argument("--judgements", nargs="*", default=[],
                   help="Judgement files in the seed jsonl shape, read instead of --new's set.")
    r.add_argument("--out")
    g = sub.add_parser("grep")
    g.add_argument("--search-db", required=True)
    g.add_argument("--papers-db")
    g.add_argument("-n", type=int, default=1000)
    g.add_argument("--seed", type=int, default=7)
    g.add_argument("--out")
    args = parser.parse_args()
    if args.cmd == "seed" and args.kind == "kbeval" and not args.search_db:
        parser.error("seed kbeval needs --search-db")
    return {"seed": lambda: seed(args) or 0, "run": lambda: run(args), "grep": lambda: grep(args)}[args.cmd]()


if __name__ == "__main__":
    sys.exit(main())
