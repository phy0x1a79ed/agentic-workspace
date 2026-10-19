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
import os
import re
import sqlite3
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

SCHEMA = 3
#: How many matching passages a literal or keyword query weighs before it ranks papers.
CANDIDATES = 2000
TRIGRAM = 3
#: A literal longer than this, or holding a LIKE wildcard, is looked up by trigrams and checked whole on each
#: passage found, not by LIKE. LIKE asks for every trigram of its pattern, and the index keeps no positions,
#: so each costs its whole posting list: cheap for a word, most of a second for a sentence. The lookup reads
#: the :data:`PROBE_GRAMS` rarest, and twice as many while the passages found fill :data:`CANDIDATES`.
LONG_LITERAL = 12
PROBE_GRAMS = 6
#: A BM25 lookup that scores more passages than this is split by passage id into up to :data:`SHARDS`
#: ranges, one thread each.
SHARD_PASSAGES = 40_000
SHARDS = 4
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


#: A term in at least this many passages is counted once per version of its store, not once per query: the
#: vocabulary reads the term's whole posting list to count it, 30 ms for a common word.
DF_CACHED = 2000
_df_cache: dict[tuple[str, int, str], int] = {}


def _version(conn: sqlite3.Connection) -> tuple[str, int]:
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    try:
        return path, os.stat(path).st_mtime_ns
    except OSError:
        return path, 0


def document_frequency(conn: sqlite3.Connection, words: list[str]) -> dict[str, int]:
    """How many passages hold each word's stem; 0 for a word no passage holds."""
    out, version = {}, _version(conn)
    for word, stem in stems(words).items():
        n = _df_cache.get((*version, stem))
        if n is None:
            row = conn.execute("SELECT doc FROM passage_vocab WHERE term = ?", (stem,)).fetchone()
            n = row[0] if row else 0
            if n >= DF_CACHED:
                if len(_df_cache) >= STEM_CACHE:
                    _df_cache.clear()
                _df_cache[(*version, stem)] = n
        out[word] = n
    return out


def keyword_plan(df: dict[str, int]) -> tuple[tuple[str, int], tuple[str, int]]:
    """From each query word's passage count: an AND of every word some passage holds, and an OR of the rarest
    of them within :data:`OR_BUDGET`; the OR is empty when it would match no more than the AND. Each comes with
    the number of passages it can match, which is what scoring it costs."""
    held = sorted((n, w) for w, n in df.items() if n)
    if not held:
        return ("", 0), ("", 0)
    kept, spent = [], 0
    for n, w in held:
        if kept and spent + n > OR_BUDGET:
            break
        kept.append(w)
        spent += n
    every = " AND ".join(f'"{w}"' for _, w in held)
    return (every, held[0][0]), ((" OR ".join(f'"{w}"' for w in kept), spent) if len(held) > 1 else ("", 0))


def keyword_exprs(df: dict[str, int]) -> tuple[str, str]:
    (every, _), (either, _) = keyword_plan(df)
    return every, either


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

    def _stores(self) -> list[Path]:
        stores = []
        base = _open_ro(self.base_path)
        if base is not None:
            row = base.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            base.close()
            if row and row[0] == str(SCHEMA):
                stores.append(self.base_path)
        if self.delta_path.exists():
            stores.append(self.delta_path)
        return stores

    def search(self, query: str, filters: Filters, limit: int, per_hit: int) -> dict[str, list[dict[str, Any]]]:
        """``exact``: papers holding the query literally, then papers holding every one of its words.
        ``related``: the other papers holding its rarest words. Each hit quotes its passages.

        The three index lookups (the literal's trigrams, the AND of the words, the OR of the rarest) run
        at once, each on its own connection; the answer is then assembled in order."""
        if filters.blocked or not query.strip():
            return {"exact": [], "related": []}
        paths = self._stores()
        if not paths:
            return {"exact": [], "related": []}
        needle, words = literal(query), terms(query)
        looked = _Lookups(paths, needle, words)
        stores = [_open_ro(p) for p in paths]
        try:
            exact = _literal_hits(stores, needle, filters, limit, per_hit, looked.literal())
            seen = {h["id"] for h in exact}
            if len(exact) < limit:
                exact += _keyword_hits(stores, looked.every(), filters, limit - len(exact), per_hit, seen, None)
            related = _keyword_hits(stores, looked.related(), filters, limit, per_hit, seen, looked.weighed)
            return {"exact": exact, "related": related}
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


def _passages_by_id(conn: sqlite3.Connection, ids: list[int]) -> list[sqlite3.Row]:
    return conn.execute(f"SELECT * FROM passage WHERE id IN ({','.join('?' * len(ids))}) ORDER BY id", ids).fetchall()


def _trigram_ids(conn: sqlite3.Connection, grams: list[str]) -> list[int]:
    expr = " AND ".join('"' + g.replace('"', '""') + '"' for g in grams)
    return [r[0] for r in conn.execute("SELECT rowid FROM passage_tri WHERE passage_tri MATCH ? LIMIT ?",
                                       (expr, CANDIDATES))]


def _bounded_phrase(needle: str) -> str:
    """The longest run of whole words inside ``needle``, as a phrase the word index can look up; empty when the
    needle holds fewer than two. A word counts only when ASCII punctuation or space bounds it in the needle on
    both sides, so the text around any hit of the needle tokenizes it the same way, and the word index, which
    keeps positions, holds the run in order: every passage with the needle is a hit, among others."""
    best: list[tuple[int, int]] = []
    run: list[tuple[int, int]] = []
    for m in re.finditer(r"[A-Za-z0-9]+", needle):
        a, b = m.span()
        if not (0 < a and b < len(needle) and needle[a - 1].isascii() and needle[b].isascii()):
            run = []
            continue
        run = run + [(a, b)] if run and needle[run[-1][1]:a].isascii() else [(a, b)]
        best = max(best, run, key=len)
    if len(best) < 2:
        return ""
    return '"' + needle[best[0][0]:best[-1][1]].replace('"', '""') + '"'


def _literal_rows(conn: sqlite3.Connection, needle: str) -> list[sqlite3.Row]:
    """Passages that may hold ``needle``; the caller checks the literal on each.

    A short literal is a LIKE, which the trigram index narrows to the passages holding all its trigrams and
    SQLite then checks on the text. A longer one with two whole words inside is looked up as their phrase in
    the word index. Any other, and any with a LIKE wildcard, is looked up by trigrams alone: its
    :data:`PROBE_GRAMS` rarest, then twice as many. A lookup that leaves fewer passages than
    :data:`CANDIDATES` has every passage that can hold the literal. One that fills the cap may have cut the
    true ones off, as with a phrase made of common words, and the LIKE, which checks, decides.
    """
    wild = re.search(r"[%_]", needle)
    like = ("SELECT p.* FROM passage_tri JOIN passage p ON p.id = passage_tri.rowid"
            " WHERE passage_tri.text LIKE ? LIMIT ?", (f"%{needle}%", CANDIDATES))
    if len(needle) <= LONG_LITERAL and not wild:
        return conn.execute(*like).fetchall()
    phrase = _bounded_phrase(needle)
    if phrase:
        ids = [r[0] for r in conn.execute("SELECT rowid FROM passage_bm25 WHERE passage_bm25 MATCH ? LIMIT ?",
                                          (phrase, CANDIDATES))]
        if len(ids) < CANDIDATES:
            return _passages_by_id(conn, ids) if ids else []
    grams = sorted({needle[i:i + TRIGRAM].lower() for i in range(len(needle) - TRIGRAM + 1)})
    df = _gram_counts(conn, grams)
    if len(df) < len(grams):
        return [] if wild else conn.execute(*like).fetchall()
    ordered = sorted(grams, key=df.__getitem__)
    for count in (PROBE_GRAMS, 2 * PROBE_GRAMS):
        ids = _trigram_ids(conn, ordered[:count])
        if len(ids) < CANDIDATES or count >= len(ordered):
            return _passages_by_id(conn, ids) if ids else []
    if wild:
        return _passages_by_id(conn, ids)
    return conn.execute(*like).fetchall()


def _literal_hits(stores: list[sqlite3.Connection], needle: str, filters: Filters, limit: int,
                  per_hit: int, found: list[list[sqlite3.Row]]) -> list[dict[str, Any]]:
    """Papers holding ``needle``, from each store's candidate passages ``found`` (see :func:`_literal_rows`)."""
    if len(needle) < TRIGRAM:
        return []
    pattern = re.compile(re.escape(needle), re.IGNORECASE)
    ranked: list[tuple[tuple, sqlite3.Row, list[dict[str, Any]]]] = []
    seen: set[str] = set()
    for conn, rows in zip(stores, found):
        by_paper: dict[str, list[tuple[sqlite3.Row, list[list[int]]]]] = {}
        for p in rows:
            marks = [[p["start"] + m.start(), p["start"] + m.end()] for m in pattern.finditer(p["text"])]
            if marks:
                by_paper.setdefault(p["paper_id"], []).append((p, marks))
        papers = _papers(conn, [k for k in by_paper if k not in seen])
        for pid, hold in by_paper.items():
            paper = papers.get(pid)
            if paper is None or not filters.keeps(paper["ts"]):
                continue
            seen.add(pid)
            hold.sort(key=lambda f: (_ZONE_RANK.get(f[0]["zone"], 2), f[0]["ord"]))
            cased = any(needle in p["text"] for p, _ in hold)
            count = sum(len(m) for _, m in hold)
            rank = (cased, -_ZONE_RANK.get(hold[0][0]["zone"], 2), count, paper["ts"])
            ranked.append((rank, paper, [_quote(p, m) for p, m in hold[:per_hit]]))
    ranked.sort(key=lambda r: r[0], reverse=True)
    return [_hit(paper, "literal", float(rank[2]), quotes) for rank, paper, quotes in ranked[:limit]]


#: The passages of a keyword match are read this many at a time, best first, until enough papers are found.
FIRST_CHUNK = 40


def _ranked(conn: sqlite3.Connection, expr: str, lo: int = 0, hi: int = 1 << 62) -> list[tuple[int, float]]:
    """The passages ``expr`` matches among ids ``lo`` to ``hi``, best BM25 first, as (passage id, rank). The
    passage rows join afterwards, on the few the answer needs: joined before the sort they are all read, a
    hundred thousand for an OR."""
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT rowid, rank FROM passage_bm25 WHERE passage_bm25 MATCH ? AND rowid BETWEEN ? AND ?"
        " ORDER BY rank LIMIT ?", (expr, lo, hi, CANDIDATES))]


def _walk(conn: sqlite3.Connection, ranked: list[tuple[int, float]], filters: Filters, want: int | None,
          skip: set[str], weighed: tuple[dict[str, int], int] | None) -> list[tuple[float, sqlite3.Row, list[sqlite3.Row]]]:
    """Each paper in the ranked passages that the filters keep and, when ``weighed`` (the query words' passage
    counts and the passage total) is given, that holds :data:`MIN_COVERAGE` of the query, with its passages in
    rank order. Passages are read in chunks, and the walk stops at ``want`` papers when a caller needs no more."""
    accepted: dict[str, tuple[float, sqlite3.Row, list[sqlite3.Row]]] = {}
    refused: set[str] = set()
    start, size = 0, FIRST_CHUNK if want is None else max(FIRST_CHUNK, want * 3)
    while start < len(ranked) and (want is None or len(accepted) < want):
        chunk = ranked[start:start + size]
        start, size = start + size, size * 4
        rows = {r["id"]: r for r in conn.execute(
            f"SELECT * FROM passage WHERE id IN ({','.join('?' * len(chunk))})", [i for i, _ in chunk])}
        known = accepted.keys() | refused | skip
        new = list(dict.fromkeys(rows[i]["paper_id"] for i, _ in chunk
                                 if i in rows and rows[i]["paper_id"] not in known))
        papers = _papers(conn, new)
        for i, rank in chunk:
            p = rows.get(i)
            if p is None or p["paper_id"] in skip or p["paper_id"] in refused:
                continue
            pid = p["paper_id"]
            if pid in accepted:
                accepted[pid][2].append(p)
                continue
            if want is not None and len(accepted) >= want:
                break
            paper = papers.get(pid)
            if paper is None or not filters.keeps(paper["ts"]) or \
                    (weighed and coverage(weighed[0], weighed[1], p["text"]) < MIN_COVERAGE):
                refused.add(pid)
                continue
            accepted[pid] = (-rank, paper, [p])
    return list(accepted.values())


def _keyword_hits(stores: list[sqlite3.Connection], ranked: list[list[tuple[int, float]]], filters: Filters,
                  limit: int, per_hit: int, skip: set[str],
                  weighed: tuple[dict[str, int], int] | None) -> list[dict[str, Any]]:
    """Papers whose passages are among the ``ranked`` ones, best BM25 first; ``weighed`` asks, besides, that
    a paper hold at least :data:`MIN_COVERAGE` of the query's information. The papers returned join ``skip``."""
    if limit <= 0:
        return []
    scored: list[tuple[float, sqlite3.Row, list[sqlite3.Row]]] = []
    for conn, ids in zip(stores, ranked):
        scored += _walk(conn, ids, filters, limit if per_hit == 1 else None, skip, weighed)
    scored.sort(key=lambda s: -s[0])
    hits = []
    for score, paper, found in scored:
        if len(hits) == limit:
            break
        if paper["paper_id"] in skip:
            continue
        skip.add(paper["paper_id"])
        hits.append(_hit(paper, "keyword", score, [_quote(p) for p in found[:per_hit]]))
    return hits


_pool = ThreadPoolExecutor(max_workers=12, thread_name_prefix="kb-fast")


def _on(path: Path, job, *args):
    conn = _open_ro(path)
    try:
        return job(conn, *args)
    finally:
        conn.close()


def _weigh(conn: sqlite3.Connection, words: list[str]) -> tuple[dict[str, int], int]:
    return document_frequency(conn, words), conn.execute("SELECT max(rowid) FROM passage").fetchone()[0] or 0


def _split(path: Path, expr: str, cost: int, total: int) -> list[Future]:
    """Score ``expr`` in as many id ranges as its cost warrants. BM25 weighs a passage by the whole index's
    statistics, so the ranges rank the same as one scan would, and their best merge to the same top."""
    if not expr:
        return []
    parts = max(1, min(SHARDS, cost // SHARD_PASSAGES))
    step = -(-total // parts)
    return [_pool.submit(_on, path, _ranked, expr, i * step + 1, (i + 1) * step) for i in range(parts)]


class _Lookups:
    """The index lookups of one query, started on every store at once, each on a connection of its own: SQLite
    holds no lock a reader needs, and releases the interpreter while it scans, so the slowest one sets the time.
    An AND that is dear to score waits until the literal hits show whether the answer needs it."""

    def __init__(self, paths: list[Path], needle: str, words: list[str]) -> None:
        self.paths = paths
        self._literal = [_pool.submit(_on, p, _literal_rows, needle) if len(needle) >= TRIGRAM else None
                         for p in paths]
        weights = [_pool.submit(_on, p, _weigh, words) if words else None for p in paths]
        self.weighed: tuple[dict[str, int], int] = ({}, 0)
        self._every: list[Any] = []
        self._related: list[list[Future]] = []
        for path, weight in zip(paths, weights):
            if weight is None:
                self._every.append([])
                self._related.append([])
                continue
            df, total = weight.result()
            if path == paths[0]:
                self.weighed = (df, total)
            (and_expr, and_cost), (or_expr, or_cost) = keyword_plan(df)
            self._every.append(_split(path, and_expr, and_cost, total) if and_cost <= SHARD_PASSAGES
                               else (path, and_expr, and_cost, total))
            self._related.append(_split(path, or_expr, or_cost, total))

    def literal(self) -> list[list[sqlite3.Row]]:
        return [f.result() if f else [] for f in self._literal]

    def every(self) -> list[list[tuple[int, float]]]:
        started = [_split(*e) if isinstance(e, tuple) else e for e in self._every]
        return [_best(fs) for fs in started]

    def related(self) -> list[list[tuple[int, float]]]:
        return [_best(fs) for fs in self._related]


def _best(parts: list[Future]) -> list[tuple[int, float]]:
    got = [r for f in parts for r in f.result()]
    return got if len(parts) < 2 else sorted(got, key=lambda r: r[1])[:CANDIDATES]
