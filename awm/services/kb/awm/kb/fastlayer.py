"""The paper fast layer: search's exact section for papers, from the shipped base and the live delta.

kb's ``kb.fastlayer`` builds the base offline from semdb, with the schema and
:data:`SCHEMA` below; the two must agree, and a base of another schema is not
read. The delta has the same tables and holds what the base lacks: Zotero items
semdb does not have, as metadata and abstract, and the map from each library
item to the semdb paper it is. A query reads both. A paper in both counts once,
under its semdb id.

A schema-4 base carries champion lists: each word stem's best passages by its
own BM25 rank, stored together. Its keyword legs read those lists and never ask
FTS5 to rank, because ``ORDER BY rank`` scores every passage a word is in, and
from a cold disk that costs minutes. The small delta is still ranked exactly.
"""

from __future__ import annotations

import hashlib
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

SCHEMA = 4
#: The base schemas this reader opens: 3 is ranked by FTS5 as before, 4 by its champion lists.
SCHEMAS = ("3", "4")
#: How many matching passages a literal or keyword query weighs before it ranks papers.
CANDIDATES = 2000
TRIGRAM = 3
#: A literal longer than this, or holding a LIKE wildcard, is looked up by trigrams and checked whole on each
#: passage found, not by LIKE. LIKE asks for every trigram of its pattern, and the index keeps no positions,
#: so each costs its whole posting list: cheap for a word, most of a second for a sentence. The lookup reads
#: the :data:`PROBE_GRAMS` rarest, and twice as many while the passages found fill :data:`CANDIDATES`.
LONG_LITERAL = 12
PROBE_GRAMS = 6
#: A long literal's word lookup runs when its rarest word is in at most RARE_BUDGET passages.
RARE_BUDGET = 50_000
#: The passages, summed over its words, that a schema-4 base's AND may read posting lists for: the long
#: literal's word lookup and the AND leg's scan in id order. Cold, a posting list costs about a random read per
#: page, so past this the AND leg is the champion lists' intersection alone.
READ_BUDGET = 150_000
#: Both budgets grow this many times on a base of 64 KB pages, as kb's build writes it: vacuumed, each posting
#: list lies in ascending pages and the kernel reads ahead. On altair, cold, an AND over 3M postings took 1.2 s
#: there and 66 s on 4 KB pages.
BIG_PAGE_BUDGET = 64
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
#: The keyword OR leg of a schema-4 base judges coverage on the text of this many of its best passages, read
#: :data:`READ_DEPTH` at a time. Coverage judged from list membership alone cut a hand-test ask from 497 papers
#: to 3.
OR_CANDIDATES = 300
#: Passage reads in flight at once. altair's root disk reads 17-20 random pages a second at depth 1 and about
#: 290 at depth 32.
READ_DEPTH = 32
#: Paper lookups up to this many run on the caller's connection; more are spread over READ_DEPTH.
PAPERS_SERIAL = 4
#: A filter that names at most this many papers, as the author filter does, is answered from those papers'
#: own passages, read and scored here; a larger one filters the index's candidates.
FILTERED_PAPERS = 300
BM25_K1, BM25_B = 1.2, 0.75
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
    #: Only these papers, as the author filter names them; None admits every paper.
    papers: set[str] | None = None

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

    def admits(self, paper: sqlite3.Row) -> bool:
        return (self.papers is None or paper["paper_id"] in self.papers) and self.keeps(paper["ts"])


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
            source = "term_df" if _has(conn, "term_df") else "passage_vocab"
            row = conn.execute(f"SELECT doc FROM {source} WHERE term = ?", (stem,)).fetchone()
            n = row[0] if row else 0
            if n >= DF_CACHED:
                if len(_df_cache) >= STEM_CACHE:
                    _df_cache.clear()
                _df_cache[(*version, stem)] = n
        out[word] = n
    return out


def keyword_words(df: dict[str, int]) -> tuple[list[str], list[str], int]:
    """Every word some passage holds, rarest first; the rarest of them within :data:`OR_BUDGET`, empty when
    an OR of them would match no more than the AND; and how many passages that OR can match."""
    held = sorted((n, w) for w, n in df.items() if n)
    kept, spent = [], 0
    for n, w in held:
        if kept and spent + n > OR_BUDGET:
            break
        kept.append(w)
        spent += n
    if len(held) < 2:
        kept, spent = [], 0
    return [w for _, w in held], kept, spent


def keyword_plan(df: dict[str, int]) -> tuple[tuple[str, int], tuple[str, int]]:
    """From each query word's passage count: an AND of every word some passage holds, and an OR of the rarest
    of them within :data:`OR_BUDGET`; the OR is empty when it would match no more than the AND. Each comes with
    the number of passages it can match, which is what scoring it costs."""
    every, kept, spent = keyword_words(df)
    if not every:
        return ("", 0), ("", 0)
    return ((" AND ".join(f'"{w}"' for w in every), min(df[w] for w in every)),
            (" OR ".join(f'"{w}"' for w in kept), spent) if kept else ("", 0))


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


def _has(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (table,)).fetchone() is not None


def _open_ro(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


class FastLayer:
    def __init__(self, base: Path, delta: Path) -> None:
        self.base_path, self.delta_path = base, delta
        #: The library's papers copied out of the base, so a library search reads a few megabytes, not the base.
        self.library_path = delta.with_name(delta.stem + "_library.db")
        self._library_lock = threading.Lock()

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
            if row and row[0] in SCHEMAS:
                stores.append(self.base_path)
        if self.delta_path.exists():
            stores.append(self.delta_path)
        return stores

    def library(self) -> set[str]:
        """The paper ids of the Zotero library's items."""
        conn = _open_ro(self.delta_path)
        if conn is None:
            return set()
        try:
            return {r[0] for r in conn.execute("SELECT paper_id FROM library")}
        except sqlite3.OperationalError:
            return set()
        finally:
            conn.close()

    def library_copy(self) -> Path | None:
        """The library store: each library paper the base holds, its row and passages, with their own indexes.
        It is rewritten when the library or the base changes, as its signature records."""
        with self._library_lock:
            mine = sorted(p for p in self.library() if not p.startswith("zotero:"))
            try:
                st = self.base_path.stat()
            except OSError:
                return None
            sign = hashlib.sha1(f"{st.st_size}:{st.st_mtime_ns}:{','.join(mine)}".encode()).hexdigest()
            have = _open_ro(self.library_path)
            if have is not None:
                try:
                    row = have.execute("SELECT value FROM meta WHERE key='signature'").fetchone()
                    if row and row[0] == sign:
                        return self.library_path
                except sqlite3.OperationalError:
                    pass
                finally:
                    have.close()
            tmp = self.library_path.with_name(self.library_path.name + ".building")
            tmp.unlink(missing_ok=True)
            db = sqlite3.connect(tmp)
            try:
                db.executescript("PRAGMA page_size=65536; PRAGMA journal_mode=OFF;" + DELTA_DDL)
                db.execute("ATTACH ? AS b", (f"file:{self.base_path}?mode=ro",))
                db.execute("CREATE TEMP TABLE want(paper_id TEXT PRIMARY KEY)")
                db.executemany("INSERT INTO want VALUES (?)", [(p,) for p in mine])
                cols = "paper_id, doi, title, authors, year, journal, domains, ts"
                db.execute(f"INSERT INTO paper({cols}) SELECT {cols} FROM b.paper WHERE paper_id IN temp.want")
                db.execute("INSERT INTO passage(id, paper_id, zone, ord, start, end, section, text)"
                           " SELECT id, paper_id, zone, ord, start, end, section, text FROM b.passage"
                           " WHERE paper_id IN temp.want ORDER BY id")
                db.execute("INSERT INTO meta VALUES ('signature', ?)", (sign,))
                db.commit()
                db.execute("DETACH b")
                db.execute("INSERT INTO passage_tri(passage_tri) VALUES ('optimize')")
                db.execute("INSERT INTO passage_bm25(passage_bm25) VALUES ('optimize')")
                db.commit()
                db.execute("VACUUM")
            finally:
                db.close()
            os.replace(tmp, self.library_path)
            return self.library_path

    def paper(self, paper_id: str) -> dict[str, Any] | None:
        """One paper in full: its row, and its abstract and body as one text, or None when no store holds it."""
        for path in self._stores():
            got = _on(path, _paper_whole, paper_id)
            if got is not None:
                return got
        return None

    def search_library(self, query: str, filters: Filters, limit: int,
                       per_hit: int) -> dict[str, list[dict[str, Any]]]:
        """:meth:`search` over the library store and the delta, which hold every library paper."""
        stores = [p for p in (self.library_copy(), self.delta_path) if p is not None and p.exists()]
        return self.search(query, filters, limit, per_hit, stores)

    def search(self, query: str, filters: Filters, limit: int, per_hit: int,
               stores: list[Path] | None = None) -> dict[str, list[dict[str, Any]]]:
        """``exact``: papers holding the query literally, then papers holding every one of its words.
        ``related``: the other papers holding its rarest words. Each hit quotes its passages.

        The three index lookups (the literal's trigrams, the AND of the words, the OR of the rarest) run
        at once, each on its own connection; the answer is then assembled in order."""
        if filters.blocked or not query.strip():
            return {"exact": [], "related": []}
        paths = self._stores() if stores is None else stores
        if not paths:
            return {"exact": [], "related": []}
        needle, words = literal(query), terms(query)
        if filters.papers is not None and len(filters.papers) <= FILTERED_PAPERS:
            return _filtered(paths, needle, words, filters, limit, per_hit)
        looked = _Lookups(paths, needle, words)
        stores = [_open_ro(p) for p in paths]
        try:
            exact = _literal_hits(stores, needle, filters, limit, per_hit, looked.literal())
            seen = {h["id"] for h in exact}
            if len(exact) < limit:
                exact += _keyword_hits(stores, looked.every(), filters, limit - len(exact), per_hit, seen, None,
                                       looked.readers(), looked.early)
            related = _keyword_hits(stores, looked.related(), filters, limit, per_hit, seen, looked.weighed,
                                    looked.readers(rows=True), looked.early)
            return {"exact": exact, "related": related}
        finally:
            for conn in stores:
                conn.close()


def _filtered(paths: list[Path], needle: str, words: list[str], filters: Filters, limit: int,
              per_hit: int) -> dict[str, list[dict[str, Any]]]:
    """The answer among the few papers ``filters`` names, from all their passages: literal papers first, then
    papers whose best passage holds every word, by BM25; ``related`` holds the rest that pass
    :data:`MIN_COVERAGE`. The index's candidates would drop a named paper ranked below their cut."""
    wanted = sorted(filters.papers or ())
    stores = [_open_ro(p) for p in paths]
    try:
        found = []
        for path, conn in zip(paths, stores):
            papers = {k: r for k, r in _papers(conn, wanted).items() if filters.admits(r)}
            found.append((conn, papers, _passages_of(path, sorted(papers)), _weigh(conn, words)))
        exact = _literal_hits([c for c, *_ in found], needle, filters, limit, per_hit,
                              [rows for _, _, rows, _ in found])
        seen = {h["id"] for h in exact}
        every, related = [], []
        for _, papers, rows, (df, total) in found:
            for score, paper, best, holds_all in _bm25_papers(papers, rows, df, total, seen):
                if holds_all:
                    every.append((score, paper, best))
                elif coverage(df, total, best[0]["text"]) >= MIN_COVERAGE:
                    related.append((score, paper, best))
        def ranked(scored):
            hits = []
            for score, paper, best in sorted(scored, key=lambda s: -s[0]):
                if len(hits) < limit and paper["paper_id"] not in seen:
                    seen.add(paper["paper_id"])
                    hits.append(_hit(paper, "keyword", score, [_quote(p) for p in best[:per_hit]]))
            return hits
        return {"exact": (exact + ranked(every))[:limit], "related": ranked(related)}
    finally:
        for conn in stores:
            conn.close()


def _passages_of(path: Path, paper_ids: list[str]) -> list[sqlite3.Row]:
    """Every passage of the papers ``paper_ids``, read :data:`READ_DEPTH` papers' worth at a time."""
    if not paper_ids:
        return []
    step = -(-len(paper_ids) // READ_DEPTH)
    parts = [_reads.submit(_on, path, _passages_by_paper, paper_ids[i:i + step])
             for i in range(0, len(paper_ids), step)]
    return [r for f in parts for r in f.result()]


def _passages_by_paper(conn: sqlite3.Connection, paper_ids: list[str]) -> list[sqlite3.Row]:
    return conn.execute(f"SELECT * FROM passage WHERE paper_id IN ({','.join('?' * len(paper_ids))})"
                        " ORDER BY id", paper_ids).fetchall()


def _bm25_papers(papers: dict[str, sqlite3.Row], rows: list[sqlite3.Row], df: dict[str, int], total: int,
                 skip: set[str]) -> list[tuple[float, sqlite3.Row, list[sqlite3.Row], bool]]:
    """Each paper outside ``skip`` with a passage holding a query word: its best BM25, its passages best first,
    and whether its best passage holds every word some passage holds. BM25 takes the word counts ``df`` of the
    whole store and the passage lengths of these papers."""
    stem_of = stems([w for w, n in df.items() if n])
    if not stem_of or not rows:
        return []
    idf = {s: math.log(1 + (total - df[w] + 0.5) / (df[w] + 0.5)) for w, s in stem_of.items()}
    tokens = [re.findall(r"\w+", p["text"].lower()) for p in rows]
    to_stem = stems(list({t for ts in tokens for t in ts}))
    lengths = [len(ts) for ts in tokens]
    mean = sum(lengths) / len(lengths) or 1.0
    scored: dict[str, list[tuple[float, sqlite3.Row, bool]]] = {}
    for p, ts, n in zip(rows, tokens, lengths):
        if p["paper_id"] in skip or p["paper_id"] not in papers:
            continue
        tf: dict[str, int] = {}
        for t in ts:
            s = to_stem.get(t)
            if s in idf:
                tf[s] = tf.get(s, 0) + 1
        if not tf:
            continue
        norm = BM25_K1 * (1 - BM25_B + BM25_B * n / mean)
        score = sum(idf[s] * f * (BM25_K1 + 1) / (f + norm) for s, f in tf.items())
        scored.setdefault(p["paper_id"], []).append((score, p, len(tf) == len(idf)))
    out = []
    for pid, held in scored.items():
        held.sort(key=lambda h: -h[0])
        out.append((held[0][0], papers[pid], [p for _, p, _ in held], held[0][2]))
    return out


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
    """The papers ``ids``; many are read :data:`READ_DEPTH` at a time, as each costs a cold index descent."""
    if not ids:
        return {}
    if len(ids) <= PAPERS_SERIAL:
        return _paper_rows(conn, ids)
    path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    step = -(-len(ids) // READ_DEPTH)
    parts = [_reads.submit(_on, path, _paper_rows, ids[i:i + step]) for i in range(0, len(ids), step)]
    return {k: r for f in parts for k, r in f.result().items()}


def _paper_rows(conn: sqlite3.Connection, ids: list[str]) -> dict[str, sqlite3.Row]:
    return {r["paper_id"]: r for r in conn.execute(
        f"SELECT * FROM paper WHERE paper_id IN ({','.join('?' * len(ids))})", ids)}


def _paper_whole(conn: sqlite3.Connection, paper_id: str) -> dict[str, Any] | None:
    paper = conn.execute("SELECT * FROM paper WHERE paper_id = ?", (paper_id,)).fetchone()
    if paper is None:
        return None
    parts = conn.execute("SELECT zone, section, text FROM passage WHERE paper_id = ? AND zone != 'title'"
                         " ORDER BY zone, ord",
                         (paper_id,)).fetchall()
    parts = sorted(parts, key=lambda p: _ZONE_RANK.get(p["zone"], 2))
    out = {k: v for k, v in _hit(paper, "", 0.0, []).items() if k in ("id", "title", "ts", "kind", "url")}
    return {**out, "authors": json.loads(paper["authors"] or "[]"), "journal": paper["journal"],
            "text": "\n\n".join(p["text"] for p in parts)}


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

    On a store with champion lists, which counts its words, nothing is read one passage at a time. A literal
    with two whole words inside is looked up first as an AND of its rarest words within
    :data:`READ_BUDGET`, and those words within the literal's span of each other: a phrase would read the
    position list of every word in it, common ones included, and cold that costs tens of seconds. Else, or when that leaves :data:`CANDIDATES` passages or more, it is
    looked up by its trigrams, the two rarest, the :data:`PROBE_GRAMS` rarest, twice as many, then all. The passages
    found are read concurrently, at most :data:`CANDIDATES` of them.

    On an older store, a short literal is a LIKE, which the trigram index narrows to the passages holding all
    its trigrams and SQLite then checks on the text; a longer one with two whole words inside is looked up as
    their phrase; any other, and any with a LIKE wildcard, by trigrams alone, as above, and a lookup that
    still fills the cap falls back to the LIKE, which checks.
    """
    wild = re.search(r"[%_]", needle)
    like = ("SELECT p.* FROM passage_tri JOIN passage p ON p.id = passage_tri.rowid"
            " WHERE passage_tri.text LIKE ? LIMIT ?", (f"%{needle}%", CANDIDATES))
    counted = _has(conn, "term_df")
    if len(needle) <= LONG_LITERAL and not wild and not counted:
        return conn.execute(*like).fetchall()
    phrase = _bounded_phrase(needle)
    if phrase and counted:
        df = document_frequency(conn, terms(phrase))
        if df and min(df.values()) == 0:
            return []
        rare_budget, read_budget = _budgets(conn)
        rare, spent = [], 0
        for w in sorted(df, key=df.__getitem__):
            if rare and spent + df[w] > read_budget:
                break
            rare.append(w)
            spent += df[w]
        if rare and df[rare[0]] <= rare_budget:
            ids = [r[0] for r in conn.execute("SELECT rowid FROM passage_bm25 WHERE passage_bm25 MATCH ? LIMIT ?",
                                              (_near(rare, phrase), CANDIDATES))]
            if len(ids) < CANDIDATES:
                return _read_ids(conn, ids)
    elif phrase:
        ids = [r[0] for r in conn.execute("SELECT rowid FROM passage_bm25 WHERE passage_bm25 MATCH ? LIMIT ?",
                                          (phrase, CANDIDATES))]
        if len(ids) < CANDIDATES:
            return _passages_by_id(conn, ids) if ids else []
    grams = sorted({needle[i:i + TRIGRAM].lower() for i in range(len(needle) - TRIGRAM + 1)})
    df = _gram_counts(conn, grams)
    if len(df) < len(grams):
        return [] if wild else conn.execute(*like).fetchall()
    ordered = sorted(grams, key=df.__getitem__)
    rounds = (2, PROBE_GRAMS, 2 * PROBE_GRAMS, len(ordered)) if counted else (PROBE_GRAMS, 2 * PROBE_GRAMS)
    for count in rounds:
        ids = _trigram_ids(conn, ordered[:count])
        if len(ids) < CANDIDATES or count >= len(ordered):
            break
    if counted:
        return _read_ids(conn, ids)
    if len(ids) < CANDIDATES or len(ordered) <= 2 * PROBE_GRAMS:
        return _passages_by_id(conn, ids) if ids else []
    return _passages_by_id(conn, ids) if wild else conn.execute(*like).fetchall()


def _near(words: list[str], phrase: str) -> str:
    """An FTS5 query for the passages holding ``words`` within ``phrase``'s own span of each other. Every passage
    holding the literal matches, since its words lie that close there; a 64 KB-page base reads the position
    lists in order, so this costs about what the AND does and leaves far fewer passages to read."""
    quoted = " ".join(f'"{w}"' for w in words)
    span = len(re.findall(r"[A-Za-z0-9]+", phrase))
    return f"NEAR({quoted}, {span})" if len(words) > 1 else quoted


def _budgets(conn: sqlite3.Connection) -> tuple[int, int]:
    """:data:`RARE_BUDGET` and :data:`READ_BUDGET` for ``conn``'s store, by its page size."""
    scale = BIG_PAGE_BUDGET if conn.execute("PRAGMA page_size").fetchone()[0] >= 65536 else 1
    return RARE_BUDGET * scale, READ_BUDGET * scale


def _read_ids(conn: sqlite3.Connection, ids: list[int]) -> list[sqlite3.Row]:
    """The passages ``ids`` of ``conn``'s store, read concurrently (:func:`_rows`), in id order."""
    got = _rows(Path(conn.execute("PRAGMA database_list").fetchone()[2]), ids)
    return [got[i] for i in sorted(got)]


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
            if paper is None or not filters.admits(paper):
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
          skip: set[str], weighed: tuple[dict[str, int], int] | None,
          read: Any = None) -> list[tuple[float, sqlite3.Row, list[sqlite3.Row]]]:
    """Each paper in the ranked passages that the filters keep and, when ``weighed`` (the query words' passage
    counts and the passage total) is given, that holds :data:`MIN_COVERAGE` of the query, with its passages in
    rank order. Passages are read in chunks, by ``read`` (passage ids to rows) when given, and the walk stops at
    ``want`` papers when a caller needs no more; the chunk it stops in still adds passages to the papers taken."""
    accepted: dict[str, tuple[float, sqlite3.Row, list[sqlite3.Row]]] = {}
    refused: set[str] = set()
    start, size = 0, FIRST_CHUNK if want is None else max(FIRST_CHUNK, want * 3)
    while start < len(ranked) and (want is None or len(accepted) < want):
        chunk = ranked[start:start + size]
        start, size = start + size, size * 4
        rows = read([i for i, _ in chunk]) if read else {r["id"]: r for r in conn.execute(
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
                continue
            paper = papers.get(pid)
            if paper is None or not filters.admits(paper) or \
                    (weighed and coverage(weighed[0], weighed[1], p["text"]) < MIN_COVERAGE):
                refused.add(pid)
                continue
            accepted[pid] = (-rank, paper, [p])
    return list(accepted.values())


def _keyword_hits(stores: list[sqlite3.Connection], ranked: list[list[tuple[int, float]]], filters: Filters,
                  limit: int, per_hit: int, skip: set[str], weighed: tuple[dict[str, int], int] | None,
                  readers: list[Any] | None = None, early: list[bool] | None = None) -> list[dict[str, Any]]:
    """Papers whose passages are among the ``ranked`` ones, best BM25 first; ``weighed`` asks, besides, that
    a paper hold at least :data:`MIN_COVERAGE` of the query's information. The papers returned join ``skip``.
    A store marked ``early`` stops at ``limit`` papers even when a hit quotes more than one passage."""
    if limit <= 0:
        return []
    scored: list[tuple[float, sqlite3.Row, list[sqlite3.Row]]] = []
    readers = readers or [None] * len(stores)
    early = early or [False] * len(stores)
    for conn, ids, read, stop in zip(stores, ranked, readers, early):
        scored += _walk(conn, ids, filters, limit if per_hit <= 1 or stop else None, skip, weighed, read)
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
_reads = ThreadPoolExecutor(max_workers=READ_DEPTH, thread_name_prefix="kb-read")


def _rows(path: Path, ids: list[int]) -> dict[int, sqlite3.Row]:
    """The passages ``ids``, read :data:`READ_DEPTH` at a time, each part on a connection of its own."""
    if not ids:
        return {}
    step = -(-len(ids) // READ_DEPTH)
    parts = [_reads.submit(_on, path, _passages_by_id, ids[i:i + step]) for i in range(0, len(ids), step)]
    return {r["id"]: r for f in parts for r in f.result()}


def _champion_lists(conn: sqlite3.Connection, terms_: list[str]) -> dict[str, tuple[dict[int, float], float]]:
    """Each stem's champion list as {passage: rank}, with the rank a passage missing from it gets: the list's
    last rank when the list was cut short, else 0, as the passage does not hold the stem."""
    depth = int(conn.execute("SELECT value FROM meta WHERE key='champion_depth'").fetchone()[0])
    out = {}
    for term in terms_:
        got = conn.execute("SELECT passage, score FROM champion WHERE term = ? ORDER BY score", (term,)).fetchall()
        out[term] = ({p: s for p, s in got}, got[-1][1] if len(got) >= depth else 0.0)
    return out


def _champion_legs(conn: sqlite3.Connection, every: list[str],
                   either: list[str]) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """The AND and the OR leg of a schema-4 base, each as (passage id, rank), best first.

    The OR sums each passage's ranks over the champion lists of the words ``either`` and keeps the best
    :data:`OR_CANDIDATES`. The AND takes the passages that hold every word ``every``: those in every list,
    whose sum is their exact rank, and, within :data:`READ_BUDGET`, the first :data:`CANDIDATES` in id order,
    the titles and abstracts first, each ranked by its sum with a cut list's last rank standing in where it is
    missing."""
    stem = stems(every)
    lists = _champion_lists(conn, sorted({stem[w] for w in every if w in stem}))
    sums: dict[int, float] = {}
    for w in either:
        for p, s in lists[stem[w]][0].items():
            sums[p] = sums.get(p, 0.0) + s
    related = sorted(sums.items(), key=lambda r: r[1])[:OR_CANDIDATES]
    if not every:
        return [], related
    found: set[int] = set()
    if sum(document_frequency(conn, every).values()) <= _budgets(conn)[1]:
        expr = " AND ".join(f'"{w}"' for w in every)
        found = {r[0] for r in conn.execute("SELECT rowid FROM passage_bm25 WHERE passage_bm25 MATCH ? LIMIT ?",
                                            (expr, CANDIDATES))}
    held = [lists[stem[w]] for w in every]
    found |= set.intersection(*(set(m) for m, _ in held))
    ranked = sorted(((p, sum(m.get(p, floor) for m, floor in held)) for p in found), key=lambda r: r[1])
    return ranked[:CANDIDATES], related


def _champion_query(path: Path, every: list[str], either: list[str]) -> tuple[list, list, dict[int, sqlite3.Row]]:
    """Both legs of a schema-4 base, and the OR leg's passages, read at once."""
    and_leg, or_leg = _on(path, _champion_legs, every, either)
    return and_leg, or_leg, _rows(path, [i for i, _ in or_leg])


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


def _champions(path: Path) -> bool:
    conn = _open_ro(path)
    try:
        return _has(conn, "champion")
    finally:
        conn.close()


class _Lookups:
    """The index lookups of one query, started on every store at once, each on a connection of its own: SQLite
    holds no lock a reader needs, and releases the interpreter while it scans, so the slowest one sets the time.
    A store with champion lists answers both keyword legs from them, and its OR leg's passages are read as
    soon as they are known. In a store without them, an AND that is dear to score waits until the literal hits
    show whether the answer needs it."""

    def __init__(self, paths: list[Path], needle: str, words: list[str]) -> None:
        self._paths = paths
        self._literal = [_pool.submit(_on, p, _literal_rows, needle) if len(needle) >= TRIGRAM else None
                         for p in paths]
        weights = [_pool.submit(_on, p, _weigh, words) if words else None for p in paths]
        self.early = [_champions(p) for p in paths]
        self.weighed: tuple[dict[str, int], int] = ({}, 0)
        self._every: list[Any] = []
        self._related: list[Any] = []
        self._read: list[Future | None] = []
        for path, weight, champion in zip(paths, weights, self.early):
            self._read.append(None)
            if weight is None:
                self._every.append([])
                self._related.append([])
                continue
            df, total = weight.result()
            if path == paths[0]:
                self.weighed = (df, total)
            if champion:
                every, either, _ = keyword_words(df)
                legs = _pool.submit(_champion_query, path, every, either)
                self._every.append(legs)
                self._related.append(legs)
                self._read[-1] = legs
                continue
            (and_expr, and_cost), (or_expr, or_cost) = keyword_plan(df)
            self._every.append(_split(path, and_expr, and_cost, total) if and_cost <= SHARD_PASSAGES
                               else (path, and_expr, and_cost, total))
            self._related.append(_split(path, or_expr, or_cost, total))

    def literal(self) -> list[list[sqlite3.Row]]:
        return [f.result() if f else [] for f in self._literal]

    def every(self) -> list[list[tuple[int, float]]]:
        out = []
        for e in self._every:
            if isinstance(e, Future):
                out.append(e.result()[0])
            else:
                out.append(_best(_split(*e) if isinstance(e, tuple) else e))
        return out

    def related(self) -> list[list[tuple[int, float]]]:
        return [e.result()[1] if isinstance(e, Future) else _best(e) for e in self._related]

    def readers(self, rows: bool = False) -> list[Any]:
        """Per store, how its walk reads passages: concurrently from a store with champion lists, taking the OR
        leg's passages already read when ``rows``; else None, one query per chunk."""
        out: list[Any] = []
        for path, champion, read in zip(self._paths, self.early, self._read):
            if not champion:
                out.append(None)
                continue
            have = read.result()[2] if rows and read else {}
            out.append(lambda ids, path=path, have=have: {**_rows(path, [i for i in ids if i not in have]),
                                                          **{i: have[i] for i in ids if i in have}})
        return out


def _best(parts: list[Future]) -> list[tuple[int, float]]:
    got = [r for f in parts for r in f.result()]
    return got if len(parts) < 2 else sorted(got, key=lambda r: r[1])[:CANDIDATES]
