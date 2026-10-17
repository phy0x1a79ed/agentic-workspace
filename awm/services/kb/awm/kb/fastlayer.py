"""The paper fast layer: search's exact section for papers, from the shipped base and the live delta.

kb's ``kb.fastlayer`` builds the base offline from semdb, with the schema and
:data:`SCHEMA` below; the two must agree, and a base of another schema is not
read. The delta has the same tables and holds what the base lacks: Zotero items
semdb does not have, as metadata and abstract, and the map from each library
item to the semdb paper it is. A query reads both. A paper in both counts once,
under its semdb id.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

SCHEMA = 3
#: How many matching passages a literal or keyword query weighs before it ranks papers.
CANDIDATES = 2000
TRIGRAM = 3
#: A literal longer than this, or holding a LIKE wildcard, is looked up by its :data:`PROBE_GRAMS` rarest
#: trigrams and checked whole on each passage found. LIKE asks for every trigram of its pattern, and the
#: index keeps no positions, so each costs its whole posting list: cheap for a phrase, seconds for a sentence.
LONG_LITERAL = 40
PROBE_GRAMS = 6
#: A keyword OR stops adding terms, rarest first, once their passages pass this many. BM25 scores every
#: passage an OR matches, so a common term costs seconds and adds almost nothing to the ranking.
OR_BUDGET = 150_000
#: A paper that holds only some of the query's words is related only when its best passage holds this share
#: of the query's information, each word weighted by its IDF; a word no passage holds weighs the most. Below
#: it, a question about a cluster job pulls in papers that merely say "node" and "run". 0.5 kept 98% of
#: semdb's gold papers and dropped 91% of the papers that journal queries pulled in (judgement set, 2026-10).
MIN_COVERAGE = 0.5
_ZONE_RANK = {"title": 0, "abstract": 1}
_STOPWORDS = frozenset(
    "a an the and or of to in on for with by from at as is are was were be been it its this "
    "that what which who how why when where do does did can i my me we you they not".split())

DELTA_DDL = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper(
    rid        INTEGER PRIMARY KEY,
    paper_id   TEXT NOT NULL UNIQUE,
    doi        TEXT NOT NULL DEFAULT '',
    title      TEXT NOT NULL DEFAULT '',
    authors    TEXT NOT NULL DEFAULT '[]',
    year       INTEGER,
    journal    TEXT NOT NULL DEFAULT '',
    domains    TEXT NOT NULL DEFAULT '[]',
    zotero_key TEXT NOT NULL DEFAULT '',
    ts         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS paper_doi ON paper(doi);
CREATE TABLE IF NOT EXISTS passage(
    id       INTEGER PRIMARY KEY,
    paper_id TEXT NOT NULL,
    zone     TEXT NOT NULL,
    ord      INTEGER NOT NULL,
    start    INTEGER NOT NULL,
    end      INTEGER NOT NULL,
    section  TEXT NOT NULL DEFAULT '',
    text     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS passage_paper ON passage(paper_id, zone, ord);
CREATE VIRTUAL TABLE IF NOT EXISTS passage_tri USING fts5(
    text, content='passage', content_rowid='id', tokenize='trigram', detail='none');
CREATE VIRTUAL TABLE IF NOT EXISTS passage_bm25 USING fts5(
    text, content='passage', content_rowid='id', tokenize='porter unicode61');
CREATE VIRTUAL TABLE IF NOT EXISTS passage_vocab USING fts5vocab(passage_bm25, 'row');
CREATE VIRTUAL TABLE IF NOT EXISTS passage_tri_vocab USING fts5vocab(passage_tri, 'row');
CREATE TRIGGER IF NOT EXISTS passage_ai AFTER INSERT ON passage BEGIN
    INSERT INTO passage_tri(rowid, text) VALUES (new.id, new.text);
    INSERT INTO passage_bm25(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS passage_ad AFTER DELETE ON passage BEGIN
    INSERT INTO passage_tri(passage_tri, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO passage_bm25(passage_bm25, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TABLE IF NOT EXISTS library(
    zotero_key TEXT PRIMARY KEY,       -- library/key, as the mirror names it
    paper_id   TEXT NOT NULL,          -- the semdb paper it is, or zotero:<library/key> in this file
    version    INTEGER NOT NULL
);
"""


@dataclass
class Filters:
    """What narrows a paper query; a filter papers cannot meet makes the answer empty."""

    kinds: list[str] = field(default_factory=list)
    after: str = ""
    before: str = ""
    blocked: bool = False

    @classmethod
    def from_args(cls, args: dict[str, Any]) -> "Filters":
        kinds = args.get("kind") or []
        kinds = [k.strip() for k in (kinds.split(",") if isinstance(kinds, str) else kinds) if str(k).strip()]
        return cls(kinds, str(args.get("after") or ""), str(args.get("before") or ""),
                   blocked=bool(args.get("project") or args.get("scope")) or (bool(kinds) and "paper" not in kinds))

    def keeps(self, ts: str) -> bool:
        if self.after and (not ts or ts < self.after):
            return False
        return not (self.before and (not ts or ts >= self.before))


def terms(text: str) -> list[str]:
    """The query's non-stopword words, in order, once each."""
    return list(dict.fromkeys(t for t in re.findall(r"\w+", text.lower()) if t not in _STOPWORDS))


_stem_lock = threading.Lock()
_stemmer: sqlite3.Connection | None = None
_stem_cache: dict[str, str | None] = {}
STEM_CACHE = 200_000


def stems(words: list[str]) -> dict[str, str]:
    """Each word's stem as the BM25 index's own porter tokenizer makes it, so a vocabulary lookup agrees.
    A word the tokenizer keeps nothing of is left out."""
    global _stemmer
    with _stem_lock:
        fresh = [w for w in dict.fromkeys(words) if w not in _stem_cache]
        if fresh:
            if _stemmer is None:
                _stemmer = sqlite3.connect(":memory:", check_same_thread=False)
                _stemmer.executescript("CREATE VIRTUAL TABLE s USING fts5(t, tokenize='porter unicode61');"
                                       "CREATE VIRTUAL TABLE v USING fts5vocab(s, 'instance');")
            _stemmer.execute("DELETE FROM s")
            _stemmer.executemany("INSERT INTO s(rowid, t) VALUES (?, ?)", list(enumerate(fresh, 1)))
            got = {fresh[doc - 1]: term for term, doc in _stemmer.execute("SELECT term, doc FROM v")}
            found = {w: got[w] if w in got else _stem_cache.get(w) for w in words}
            if len(_stem_cache) + len(fresh) > STEM_CACHE:
                _stem_cache.clear()
            _stem_cache.update({w: got.get(w) for w in fresh})
        else:
            found = {w: _stem_cache[w] for w in words}
        return {w: s for w, s in found.items() if s is not None}


def document_frequency(conn: sqlite3.Connection, words: list[str]) -> dict[str, int]:
    """How many passages hold each word's stem; 0 for a word no passage holds."""
    out = {}
    for word, stem in stems(words).items():
        row = conn.execute("SELECT doc FROM passage_vocab WHERE term = ?", (stem,)).fetchone()
        out[word] = row[0] if row else 0
    return out


def keyword_exprs(df: dict[str, int]) -> tuple[str, str]:
    """From each query word's passage count: an AND of every word some passage holds, and an OR of the rarest
    of them within :data:`OR_BUDGET`; the OR is empty when it would match no more than the AND."""
    held = sorted((n, w) for w, n in df.items() if n)
    if not held:
        return "", ""
    kept, spent = [], 0
    for n, w in held:
        if kept and spent + n > OR_BUDGET:
            break
        kept.append(w)
        spent += n
    every = " AND ".join(f'"{w}"' for _, w in held)
    return every, (" OR ".join(f'"{w}"' for w in kept) if len(held) > 1 else "")


def coverage(df: dict[str, int], passages: int, text: str) -> float:
    """The IDF-weighted share of the query's words, given by their passage counts ``df``, that ``text`` holds."""
    weights = {w: math.log((passages + 1) / (n + 1)) for w, n in df.items()}
    total = sum(weights.values())
    if total <= 0:
        return 0.0
    wanted = stems(list(weights))
    held = set(stems(terms(text)).values())
    return sum(v for w, v in weights.items() if wanted.get(w) in held) / total


def literal(query: str) -> str:
    text = query.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'`":
        text = text[1:-1]
    return text


def _open_ro(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


class FastLayer:
    def __init__(self, base: Path, delta: Path) -> None:
        self.base_path, self.delta_path = base, delta

    # -- delta ------------------------------------------------------------

    def _delta(self) -> sqlite3.Connection:
        self.delta_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.delta_path)
        conn.row_factory = sqlite3.Row
        conn.executescript(DELTA_DDL)
        return conn

    def cursor(self) -> dict[str, int]:
        """The per-library Zotero versions the delta has taken in."""
        conn = self._delta()
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='zotero_versions'").fetchone()
            return json.loads(row["value"]) if row else {}
        finally:
            conn.close()

    def take_items(self, items: Iterable[dict[str, Any]], versions: dict[str, int] | None = None) -> dict[str, int]:
        """Map each Zotero item to its semdb paper, or hold it in the delta when the base lacks it."""
        base = _open_ro(self.base_path)
        conn = self._delta()
        mapped = held = 0
        try:
            for item in items:
                ref = f"{item.get('library') or ''}/{item.get('key') or ''}"
                if not item.get("key"):
                    continue
                own = f"zotero:{ref}"
                found = _match(base, item) if base else None
                conn.execute("DELETE FROM passage WHERE paper_id=?", (own,))
                conn.execute("DELETE FROM paper WHERE paper_id=?", (own,))
                if found:
                    mapped += 1
                else:
                    held += 1
                    _hold(conn, own, ref, item)
                conn.execute("INSERT INTO library(zotero_key, paper_id, version) VALUES (?,?,?)"
                             " ON CONFLICT(zotero_key) DO UPDATE SET paper_id=excluded.paper_id,"
                             " version=excluded.version", (ref, found or own, int(item.get("version") or 0)))
            if versions is not None:
                conn.execute("INSERT INTO meta VALUES ('zotero_versions', ?) ON CONFLICT(key)"
                             " DO UPDATE SET value=excluded.value", (json.dumps(versions),))
            conn.commit()
        finally:
            conn.close()
            if base:
                base.close()
        return {"mapped": mapped, "held": held}

    def remap(self) -> dict[str, int]:
        """Drop from the delta every held item the base now has, as after a new base ships."""
        base = _open_ro(self.base_path)
        if base is None:
            return {"remapped": 0}
        conn = self._delta()
        moved = 0
        try:
            held = conn.execute("SELECT * FROM paper WHERE paper_id LIKE 'zotero:%'").fetchall()
            for paper in held:
                found = _match(base, {"doi": paper["doi"], "title": paper["title"], "year": paper["year"]})
                if found:
                    conn.execute("DELETE FROM passage WHERE paper_id=?", (paper["paper_id"],))
                    conn.execute("DELETE FROM paper WHERE paper_id=?", (paper["paper_id"],))
                    conn.execute("UPDATE library SET paper_id=? WHERE paper_id=?", (found, paper["paper_id"]))
                    moved += 1
            conn.commit()
        finally:
            conn.close()
            base.close()
        return {"remapped": moved}

    # -- query ------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"base": None, "delta": None}
        for name, path in (("base", self.base_path), ("delta", self.delta_path)):
            conn = _open_ro(path)
            if conn is None:
                continue
            try:
                meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
                out[name] = {"papers": conn.execute("SELECT MAX(rid) FROM paper").fetchone()[0] or 0,
                             "built": meta.get("built"), "schema": meta.get("schema")}
                if name == "delta":
                    out[name]["library"] = conn.execute("SELECT COUNT(*) FROM library").fetchone()[0]
            finally:
                conn.close()
        return out

    def _stores(self) -> list[sqlite3.Connection]:
        stores = []
        base = _open_ro(self.base_path)
        if base is not None:
            row = base.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            if row and row[0] == str(SCHEMA):
                stores.append(base)
            else:
                base.close()
        delta = _open_ro(self.delta_path)
        if delta is not None:
            stores.append(delta)
        return stores

    def search(self, query: str, filters: Filters, limit: int, per_hit: int) -> dict[str, list[dict[str, Any]]]:
        """``exact``: papers holding the query literally, then papers holding every one of its words.
        ``related``: the other papers holding its rarest words. Each hit quotes its passages."""
        if filters.blocked or not query.strip():
            return {"exact": [], "related": []}
        stores = self._stores()
        try:
            exact = _literal_hits(stores, literal(query), filters, limit, per_hit)
            seen = {h["id"] for h in exact}
            exact += _keyword_hits(stores, query, filters, limit - len(exact), per_hit, seen, every=True)
            return {"exact": exact, "related": _keyword_hits(stores, query, filters, limit, per_hit, seen, every=False)}
        finally:
            for conn in stores:
                conn.close()


def _norm_title(title: str) -> str:
    return re.sub(r"\W+", " ", title.lower()).strip()


def _match(base: sqlite3.Connection, item: dict[str, Any]) -> str | None:
    doi = (item.get("doi") or "").strip().lower()
    if doi:
        row = base.execute("SELECT paper_id FROM paper WHERE doi=?", (doi,)).fetchone()
        if row:
            return row["paper_id"]
    title, year = _norm_title(item.get("title") or ""), item.get("year")
    if len(title) < 20 or not year:
        return None
    for p in _literal_rows(base, (item.get("title") or "").strip()[:200]):
        if p["zone"] != "title":
            continue
        paper = base.execute("SELECT title, year FROM paper WHERE paper_id = ?", (p["paper_id"],)).fetchone()
        if paper and _norm_title(paper["title"]) == title and str(paper["year"]) == str(year)[:4]:
            return p["paper_id"]
    return None


def _hold(conn: sqlite3.Connection, own: str, ref: str, item: dict[str, Any]) -> None:
    year = item.get("year")
    year = int(str(year)[:4]) if year and str(year)[:4].isdigit() else None
    title, abstract = (item.get("title") or "").strip(), (item.get("abstract") or "").strip()
    conn.execute("INSERT INTO paper(paper_id, doi, title, authors, year, journal, zotero_key, ts)"
                 " VALUES (?,?,?,?,?,?,?,?)",
                 (own, (item.get("doi") or "").strip().lower(), title,
                  json.dumps([str(c) for c in item.get("creators") or []]), year,
                  item.get("publication") or "", ref, f"{year}-01-01T00:00:00Z" if year else ""))
    for zone, text in (("title", title), ("abstract", abstract)):
        if text:
            conn.execute("INSERT INTO passage(paper_id, zone, ord, start, end, text) VALUES (?,?,0,0,?,?)",
                         (own, zone, len(text), text))


def _papers(conn: sqlite3.Connection, ids: list[str]) -> dict[str, sqlite3.Row]:
    if not ids:
        return {}
    return {r["paper_id"]: r for r in conn.execute(
        f"SELECT * FROM paper WHERE paper_id IN ({','.join('?' * len(ids))})", ids)}


def _hit(paper: sqlite3.Row, match: str, score: float, passages: list[dict[str, Any]]) -> dict[str, Any]:
    hit: dict[str, Any] = {"id": paper["paper_id"], "title": paper["title"], "ts": paper["ts"],
                           "kind": "paper", "match": match, "score": round(score, 4), "passages": passages}
    if paper["doi"]:
        hit["url"] = f"https://doi.org/{paper['doi']}"
    return hit


def _quote(p: sqlite3.Row, marks: list[list[int]] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"field": p["zone"], "start": p["start"], "end": p["end"], "text": p["text"]}
    if p["section"]:
        out["section"] = p["section"]
    if marks:
        out["marks"] = marks
    return out


def _gram_counts(conn: sqlite3.Connection, grams: list[str]) -> dict[str, int]:
    """How many passages hold each trigram: from the base's precomputed table, else from the vocabulary.

    A vocabulary lookup reads the trigram's whole posting list to count it,
    which the small delta can afford and the base cannot.
    """
    marks = ",".join("?" * len(grams))
    has_table = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'trigram_df'").fetchone()
    source = "trigram_df" if has_table else "passage_tri_vocab"
    return dict(conn.execute(f"SELECT term, doc FROM {source} WHERE term IN ({marks})", grams).fetchall())


def _literal_rows(conn: sqlite3.Connection, needle: str) -> list[sqlite3.Row]:
    """Passages that may hold ``needle``; the caller checks the literal on each."""
    if len(needle) <= LONG_LITERAL and not re.search(r"[%_]", needle):
        return conn.execute("SELECT p.* FROM passage_tri JOIN passage p ON p.id = passage_tri.rowid"
                            " WHERE passage_tri.text LIKE ? LIMIT ?", (f"%{needle}%", CANDIDATES)).fetchall()
    grams = sorted({needle[i:i + TRIGRAM].lower() for i in range(len(needle) - TRIGRAM + 1)})
    df = _gram_counts(conn, grams)
    if len(df) < len(grams):
        return []
    chosen = sorted(grams, key=df.__getitem__)[:PROBE_GRAMS]
    expr = " AND ".join('"' + g.replace('"', '""') + '"' for g in chosen)
    return conn.execute("SELECT p.* FROM passage_tri JOIN passage p ON p.id = passage_tri.rowid"
                        " WHERE passage_tri MATCH ? LIMIT ?", (expr, CANDIDATES)).fetchall()


def _literal_hits(stores: list[sqlite3.Connection], needle: str, filters: Filters, limit: int,
                  per_hit: int) -> list[dict[str, Any]]:
    if len(needle) < TRIGRAM:
        return []
    pattern = re.compile(re.escape(needle), re.IGNORECASE)
    ranked: list[tuple[tuple, sqlite3.Row, list[dict[str, Any]]]] = []
    seen: set[str] = set()
    for conn in stores:
        rows = _literal_rows(conn, needle)
        by_paper: dict[str, list[tuple[sqlite3.Row, list[list[int]]]]] = {}
        for p in rows:
            marks = [[p["start"] + m.start(), p["start"] + m.end()] for m in pattern.finditer(p["text"])]
            if marks:
                by_paper.setdefault(p["paper_id"], []).append((p, marks))
        papers = _papers(conn, [k for k in by_paper if k not in seen])
        for pid, found in by_paper.items():
            paper = papers.get(pid)
            if paper is None or not filters.keeps(paper["ts"]):
                continue
            seen.add(pid)
            found.sort(key=lambda f: (_ZONE_RANK.get(f[0]["zone"], 2), f[0]["ord"]))
            cased = any(needle in p["text"] for p, _ in found)
            count = sum(len(m) for _, m in found)
            rank = (cased, -_ZONE_RANK.get(found[0][0]["zone"], 2), count, paper["ts"])
            ranked.append((rank, paper, [_quote(p, m) for p, m in found[:per_hit]]))
    ranked.sort(key=lambda r: r[0], reverse=True)
    return [_hit(paper, "literal", float(rank[2]), quotes) for rank, paper, quotes in ranked[:limit]]


def _keyword_hits(stores: list[sqlite3.Connection], query: str, filters: Filters, limit: int, per_hit: int,
                  skip: set[str], every: bool) -> list[dict[str, Any]]:
    """Papers whose passages hold every word of the query, or else the rarest of its words and at least
    :data:`MIN_COVERAGE` of its information, best BM25 first. The papers returned join ``skip``."""
    words = terms(query)
    if not words or limit <= 0 or not stores:
        return []
    scored: list[tuple[float, sqlite3.Row, list[sqlite3.Row]]] = []
    weighed = document_frequency(stores[0], words)
    passages = stores[0].execute("SELECT max(rowid) FROM passage").fetchone()[0] or 0
    for conn in stores:
        expr = keyword_exprs(weighed if conn is stores[0] else document_frequency(conn, words))[0 if every else 1]
        if not expr:
            continue
        rows = conn.execute(
            "SELECT p.*, bm25(passage_bm25) AS rank FROM passage_bm25 JOIN passage p ON p.id = passage_bm25.rowid"
            " WHERE passage_bm25 MATCH ? ORDER BY rank LIMIT ?", (expr, CANDIDATES)).fetchall()
        by_paper: dict[str, list[sqlite3.Row]] = {}
        for p in rows:
            if p["paper_id"] not in skip:
                by_paper.setdefault(p["paper_id"], []).append(p)
        papers = _papers(conn, list(by_paper))
        scored += [(-found[0]["rank"], papers[pid], found) for pid, found in by_paper.items()
                   if pid in papers and filters.keeps(papers[pid]["ts"])]
    scored.sort(key=lambda s: -s[0])
    hits = []
    for score, paper, found in scored:
        if len(hits) == limit:
            break
        if paper["paper_id"] in skip or (not every and coverage(weighed, passages, found[0]["text"]) < MIN_COVERAGE):
            continue
        skip.add(paper["paper_id"])
        hits.append(_hit(paper, "keyword", score, [_quote(p) for p in found[:per_hit]]))
    return hits
