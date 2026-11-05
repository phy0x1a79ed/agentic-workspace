"""Each index source's entries and chunk vectors, in LanceDB under ``AWM_DIR/services/search/lance/``.

A source has two tables. ``<source>`` holds one row per entry: its id and one column per
declared field. ``<source>__chunks`` holds one row per chunk of the entry's content, with
the chunk's vector and a copy of every non-content field, so a vector search filters
before it ranks. A chunk keeps the hash of its text, and an upsert reuses the stored
vector of an unchanged chunk, so a retag embeds nothing.

Rows written since the last index update are scanned flat, so a read sees a write at once.
Maintenance folds new rows into delta indexes and merges the deltas only when they stack
up, because a merge reads the whole vector column.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import lancedb
import numpy as np
import pyarrow as pa

from awm import config
from awm.persistence import embeddings as engine

from awm.search.registry import Field

log = logging.getLogger("awm.search.lance_store")

#: Bump when the table layout or the chunking changes: every source rebuilds on start.
ENGINE_VERSION = 1
INDEX_CACHE_BYTES = 64 << 20
METADATA_CACHE_BYTES = 32 << 20
DIM = 384
#: Below this many chunks a flat scan is exact and small, so no vector index is built.
IVF_MIN_ROWS = 5000
#: Delta indexes that may stack up before a merge folds them.
MERGE_AT = 8
KEEP_VERSIONS = timedelta(hours=1)
CHUNKS = "__chunks"
TS = pa.timestamp("us", tz="UTC")
_ARROW = {"content": pa.string(), "categorical": pa.list_(pa.string()), "numeric": pa.float64(), "datetime": TS}
_FTS = dict(with_position=True, base_tokenizer="simple", lower_case=True, stem=False,
            remove_stop_words=False, ascii_folding=True)


def root() -> Path:
    return config.AWM_DIR / "services" / "search" / "lance"


def entries_schema(fields: dict[str, Field]) -> pa.Schema:
    return pa.schema([("id", pa.string()), *((n, _ARROW[f.cls]) for n, f in fields.items())])


def chunks_schema(fields: dict[str, Field]) -> pa.Schema:
    kept = [(n, _ARROW[f.cls]) for n, f in fields.items() if f.cls != "content"]
    return pa.schema([("id", pa.string()), ("chunk", pa.int32()), ("hash", pa.string()), *kept,
                      ("vector", pa.list_(pa.float32(), DIM))])


def parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _cell(f: Field, value: Any) -> Any:
    if f.cls == "content":
        return "" if value is None else str(value)
    if f.cls == "categorical":
        items = value if isinstance(value, list) else [] if value in (None, "") else [value]
        return [str(x) for x in items if x not in (None, "")]
    if f.cls == "numeric":
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    return parse_ts(value)


def lead(fields: dict[str, Field], hit: Sequence[str]) -> str:
    """The content field that heads each chunk: the first content field a hit shows, else the first declared."""
    content = [n for n, f in fields.items() if f.cls == "content"]
    return next((h for h in hit if h in content), content[0])


def _chunk_texts(row: dict[str, Any], fields: dict[str, Field], head: str) -> list[str]:
    """The texts embedded for one entry: its body chunks, each under the lead field and the entry's date."""
    title = row.get(head) or ""
    body = "\n\n".join(row[n] for n, f in fields.items() if f.cls == "content" and n != head and row.get(n))
    stamp = next((row[n] for n, f in fields.items() if f.cls == "datetime" and row.get(n)), None)
    doc = engine.Document(row["id"], body, title=title, date=stamp.date().isoformat() if stamp else "")
    parts = engine.chunk(body, engine.CHUNK_TOKENS)
    if not parts:
        return [title.strip()] if title.strip() else []
    return [f"{engine.header(doc, sec)}\n{text}" for sec, text in parts]


def _hash(text: str) -> str:
    return hashlib.sha1(f"{ENGINE_VERSION}\x00{text}".encode()).hexdigest()


def quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def id_list(ids: Iterable[str]) -> str:
    return "id IN (" + ", ".join(quote(i) for i in ids) + ")"


class Tables:
    """One source's entry and chunk tables."""

    def __init__(self, db: lancedb.DBConnection, name: str, fields: dict[str, Field], hit: Sequence[str]) -> None:
        self.name, self.fields, self.hit = name, fields, tuple(hit)
        self.head = lead(fields, hit)
        self.content = [n for n, f in fields.items() if f.cls == "content"]
        existing = set(db.table_names())
        self.entries = (db.open_table(name) if name in existing
                        else db.create_table(name, schema=entries_schema(fields)))
        chunks = name + CHUNKS
        self.chunks = (db.open_table(chunks) if chunks in existing
                       else db.create_table(chunks, schema=chunks_schema(fields)))

    # -- writes -----------------------------------------------------------

    def upsert(self, rows: list[dict[str, Any]], embedder: engine.Embedder | None) -> int:
        """Write entries ``{id, <field>: value}``, replacing any stored under the same id. Returns chunks embedded."""
        if not rows:
            return 0
        cells = [{"id": str(r["id"]), **{n: _cell(f, r.get(n)) for n, f in self.fields.items()}} for r in rows]
        ids = [c["id"] for c in cells]
        self.entries.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(
            pa.Table.from_pylist(cells, schema=entries_schema(self.fields)))
        if embedder is None:
            self.chunks.delete(id_list(ids))
            return 0
        texts = {c["id"]: _chunk_texts(c, self.fields, self.head) for c in cells}
        held = self._held_vectors(ids)
        todo = sorted({t for ts in texts.values() for t in ts if _hash(t) not in held})
        if todo:
            for t, v in zip(todo, embedder.encode(todo, query=False)):
                held[_hash(t)] = np.asarray(v, dtype=np.float32)
        kept = {n: f for n, f in self.fields.items() if f.cls != "content"}
        out = [{"id": c["id"], "chunk": j, "hash": _hash(t), **{n: c[n] for n in kept}, "vector": held[_hash(t)]}
               for c in cells for j, t in enumerate(texts[c["id"]])]
        self.chunks.delete(id_list(ids))
        if out:
            self.chunks.add(pa.Table.from_pylist(out, schema=chunks_schema(self.fields)))
        return len(todo)

    def _held_vectors(self, ids: list[str]) -> dict[str, np.ndarray]:
        if self.chunks.count_rows() == 0:
            return {}
        got = self.chunks.search().where(id_list(ids)).select(["hash", "vector"]).limit(None).to_arrow()
        vectors = got["vector"].to_numpy(zero_copy_only=False) if got.num_rows else []
        return {h: np.asarray(v, dtype=np.float32) for h, v in zip(got["hash"].to_pylist(), vectors)}

    def delete(self, ids: Sequence[str]) -> None:
        if ids:
            self.entries.delete(id_list(ids))
            self.chunks.delete(id_list(ids))

    # -- maintenance ------------------------------------------------------

    def _ensure_indexes(self) -> None:
        have = {i.name for i in self.entries.list_indices()}
        if self.entries.count_rows():
            if "id_idx" not in have:
                self.entries.create_scalar_index("id", index_type="BTREE")
            for name in self.content:
                if f"{name}_idx" not in have:
                    self.entries.create_fts_index(name, **_FTS)
            for name, f in self.fields.items():
                if f.cls != "content" and f"{name}_idx" not in have:
                    self.entries.create_scalar_index(name, index_type="LABEL_LIST" if f.cls == "categorical"
                                                     else "BTREE")
        rows = self.chunks.count_rows()
        have = {i.name for i in self.chunks.list_indices()}
        if rows and "id_idx" not in have:
            self.chunks.create_scalar_index("id", index_type="BTREE")
        if rows >= IVF_MIN_ROWS and "vector_idx" not in have:
            self.chunks.create_index(metric="cosine", index_type="IVF_RQ", num_partitions=max(16, int(rows ** 0.5)),
                                     num_bits=1, vector_column_name="vector")

    def maintain(self) -> dict[str, Any]:
        """Fold new rows into the indexes, compact small files, merge stacked deltas, drop old versions."""
        self._ensure_indexes()
        merged = []
        for table in (self.entries, self.chunks):
            stacked = max((table.index_stats(i.name).num_indices for i in table.list_indices()), default=0)
            ds = table.to_lance()
            if stacked > MERGE_AT:
                ds.optimize.optimize_indices(num_indices_to_merge=stacked)
                merged.append(table.name)
            else:
                ds.optimize.optimize_indices(num_indices_to_merge=0)
            ds.optimize.compact_files(materialize_deletions_threshold=0.5)
            table.cleanup_old_versions(older_than=KEEP_VERSIONS)
        return {"merged": merged}

    def count(self) -> int:
        return self.entries.count_rows()


class Store:
    """The LanceDB connection all sources share, with capped caches."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or root()
        self.path.mkdir(parents=True, exist_ok=True)
        session = lancedb.Session(index_cache_size_bytes=INDEX_CACHE_BYTES,
                                  metadata_cache_size_bytes=METADATA_CACHE_BYTES)
        self.db = lancedb.connect(str(self.path), read_consistency_interval=timedelta(0), session=session)
        self._open: dict[str, Tables] = {}
        self._lock = threading.Lock()

    def tables(self, name: str, fields: dict[str, Field], hit: Sequence[str]) -> Tables:
        with self._lock:
            held = self._open.get(name)
            if held is None or held.fields != fields or held.hit != tuple(hit):
                held = self._open[name] = Tables(self.db, name, fields, hit)
            return held

    def has(self, name: str) -> bool:
        return name in self.db.table_names()

    def drop(self, name: str) -> None:
        """Remove a source's tables; the next :meth:`tables` creates them empty."""
        with self._lock:
            self._open.pop(name, None)
            for table in (name, name + CHUNKS):
                if table in self.db.table_names():
                    self.db.drop_table(table)
                    shutil.rmtree(self.path / f"{table}.lance", ignore_errors=True)
