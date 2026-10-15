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
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

SCHEMA = 2
#: How many matching passages a literal or keyword query weighs before it ranks papers.
CANDIDATES = 2000
#: The trigram index serves LIKE only for a pattern holding a run this long free of % and _.
TRIGRAM = 3
#: A keyword query ORs at most this many of its rarest terms, each in at most this share of passages.
#: A common term costs a long posting list and adds almost nothing to BM25.
KEYWORD_TERMS = 6
COMMON_SHARE = 0.10
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


def _df(conn: sqlite3.Connection, word: str) -> int | None:
    """How many passages hold ``word``'s stem, found by trimming the word down to a stem the vocabulary has."""
    for cut in range(len(word), max(2, len(word) - 4), -1):
        stem = word[:cut]
        row = conn.execute("SELECT doc FROM passage_vocab WHERE term = ?", (stem,)).fetchone()
        if row is None and stem.endswith("y"):
            row = conn.execute("SELECT doc FROM passage_vocab WHERE term = ?", (stem[:-1] + "i",)).fetchone()
        if row:
            return row[0]
    return None


def keyword_expr(conn: sqlite3.Connection, text: str) -> str:
    """An OR of the query's rarest terms that some passage holds and few passages share."""
    words = terms(text)
    total = conn.execute("SELECT MAX(id) FROM passage").fetchone()[0] or 1
    counted = [(n, w) for w in words if (n := _df(conn, w))]
    kept = [w for n, w in sorted(counted) if n <= COMMON_SHARE * total][:KEYWORD_TERMS]
    kept = kept or [w for _, w in sorted(counted)][:2]
    return " OR ".join(f'"{w}"' for w in kept)


def _like_runs(needle: str) -> str:
    """The longest run of ``needle`` free of LIKE's wildcards, which the trigram index can serve."""
    return max(re.split(r"[%_\\]", needle), key=len)


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

    def exact(self, query: str, filters: Filters, limit: int, per_hit: int) -> list[dict[str, Any]]:
        """Papers holding the query literally, then papers matching its words, each with its passages."""
        if filters.blocked or not query.strip():
            return []
        stores = self._stores()
        try:
            hits = _literal_hits(stores, literal(query), filters, limit, per_hit)
            seen = {h["id"] for h in hits}
            if len(hits) < limit:
                hits += _keyword_hits(stores, query, filters, limit - len(hits), per_hit, seen)
            return hits
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
    run = _like_runs((item.get("title") or "").strip()[:200])
    if len(run) < TRIGRAM:
        return None
    rows = base.execute(
        "SELECT p.paper_id, pa.title, pa.year FROM passage_tri JOIN passage p ON p.id = passage_tri.rowid"
        " JOIN paper pa ON pa.paper_id = p.paper_id"
        " WHERE passage_tri.text LIKE ? AND p.zone = 'title' LIMIT 20", (f"%{run}%",)).fetchall()
    for r in rows:
        if _norm_title(r["title"]) == title and str(r["year"]) == str(year)[:4]:
            return r["paper_id"]
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


def _literal_hits(stores: list[sqlite3.Connection], needle: str, filters: Filters, limit: int,
                  per_hit: int) -> list[dict[str, Any]]:
    run = _like_runs(needle)
    if len(run) < TRIGRAM:
        return []
    pattern = re.compile(re.escape(needle), re.IGNORECASE)
    ranked: list[tuple[tuple, sqlite3.Row, list[dict[str, Any]]]] = []
    seen: set[str] = set()
    for conn in stores:
        rows = conn.execute(
            "SELECT p.* FROM passage_tri JOIN passage p ON p.id = passage_tri.rowid"
            " WHERE passage_tri.text LIKE ? LIMIT ?", (f"%{run}%", CANDIDATES)).fetchall()
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
                  skip: set[str]) -> list[dict[str, Any]]:
    if not terms(query) or limit <= 0:
        return []
    scored: list[tuple[float, sqlite3.Row, list[dict[str, Any]]]] = []
    for conn in stores:
        expr = keyword_expr(conn, query)
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
        for pid, found in by_paper.items():
            paper = papers.get(pid)
            if paper is None or not filters.keeps(paper["ts"]):
                continue
            skip.add(pid)
            scored.append((-found[0]["rank"], paper, [_quote(p) for p in found[:per_hit]]))
    scored.sort(key=lambda s: s[0], reverse=True)
    return [_hit(paper, "keyword", score, quotes) for score, paper, quotes in scored[:limit]]
