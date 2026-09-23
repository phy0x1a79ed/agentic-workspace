"""SQLite index of the webui's output images, virtual folders and trash.

Folders are labels: an image belongs to any number of them through
`memberships`, and no folder operation ever touches an image file. The only
file operations are trash (move aside), restore (move back) and purge.
"""
import json
import os
import shutil
import sqlite3
import threading
import time

from meta import read_image

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
# The webui may still be writing a file this young.
SETTLE_S = 2.0
_BATCH = 200

SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY,
    relpath TEXT NOT NULL UNIQUE,
    dir TEXT NOT NULL,
    name TEXT NOT NULL,
    mtime REAL NOT NULL,
    size INTEGER NOT NULL,
    width INTEGER,
    height INTEGER,
    meta TEXT,
    error TEXT,
    state TEXT NOT NULL DEFAULT 'ok',
    trash_path TEXT,
    trashed_at REAL
);
CREATE INDEX IF NOT EXISTS images_dir ON images(dir, state);
CREATE INDEX IF NOT EXISTS images_order ON images(state, mtime DESC, name DESC);
CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    parent_id INTEGER REFERENCES folders(id) ON DELETE CASCADE,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS memberships (
    folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    added REAL NOT NULL,
    PRIMARY KEY (folder_id, image_id)
);
CREATE INDEX IF NOT EXISTS memberships_image ON memberships(image_id);
"""

_ORDER = "ORDER BY i.mtime DESC, i.name DESC"


class StoreError(ValueError):
    pass


class Store:
    def __init__(self, outputs_dir, data_dir):
        self.outputs = os.path.realpath(outputs_dir)
        self.data = data_dir
        self.trash_dir = os.path.join(data_dir, "trash")
        os.makedirs(self.trash_dir, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(os.path.join(data_dir, "view.db"), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(SCHEMA)

    def close(self):
        with self._lock:
            self._db.close()

    # ---- scanning -------------------------------------------------------

    def _walk(self):
        for dirpath, dirnames, filenames in os.walk(self.outputs):
            dirnames.sort()
            for fn in filenames:
                if fn.lower().endswith(IMAGE_EXTS):
                    full = os.path.join(dirpath, fn)
                    yield full, os.path.relpath(full, self.outputs).replace(os.sep, "/")

    def scan(self):
        """Index new and changed files and mark vanished ones missing."""
        with self._lock:
            known = {r["relpath"]: (r["mtime"], r["size"], r["state"])
                     for r in self._db.execute("SELECT relpath, mtime, size, state FROM images")}
        now = time.time()
        seen = set()
        pending = []
        stats = {"added": 0, "updated": 0, "missing": 0}
        for full, rel in self._walk():
            try:
                st = os.stat(full)
            except OSError:
                continue
            if now - st.st_mtime < SETTLE_S:
                continue
            seen.add(rel)
            prev = known.get(rel)
            if prev and prev[0] == st.st_mtime and prev[1] == st.st_size and prev[2] == "ok":
                continue
            info = read_image(full, rel)
            stats["updated" if prev else "added"] += 1
            pending.append((rel, st, info))
            if len(pending) >= _BATCH:
                self._upsert(pending)
                pending = []
        if pending:
            self._upsert(pending)
        gone = [rel for rel, (_, _, state) in known.items() if state == "ok" and rel not in seen]
        # A file still younger than SETTLE_S was skipped above, not removed.
        gone = [rel for rel in gone if not os.path.exists(os.path.join(self.outputs, rel))]
        if gone:
            with self._lock, self._db:
                self._db.executemany("UPDATE images SET state='missing' WHERE relpath=?", [(r,) for r in gone])
            stats["missing"] = len(gone)
        return stats

    def _upsert(self, rows):
        with self._lock, self._db:
            for rel, st, info in rows:
                d, _, name = rel.rpartition("/")
                self._db.execute(
                    """INSERT INTO images (relpath, dir, name, mtime, size, width, height, meta, error, state)
                       VALUES (?,?,?,?,?,?,?,?,?,'ok')
                       ON CONFLICT(relpath) DO UPDATE SET
                         mtime=excluded.mtime, size=excluded.size, width=excluded.width,
                         height=excluded.height, meta=excluded.meta, error=excluded.error,
                         state='ok', trash_path=NULL, trashed_at=NULL""",
                    (rel, d, name, st.st_mtime, st.st_size, info["width"], info["height"],
                     json.dumps(info["meta"]) if info["meta"] else None, info["error"]))

    # ---- reading --------------------------------------------------------

    def tree(self):
        """Physical directories with direct and recursive image counts."""
        with self._lock:
            rows = self._db.execute(
                "SELECT dir, COUNT(*) AS n FROM images WHERE state='ok' GROUP BY dir").fetchall()
        dirs = {}
        for r in rows:
            parts = r["dir"].split("/") if r["dir"] else []
            dirs.setdefault(r["dir"], {"direct": 0, "total": 0})["direct"] = r["n"]
            for i in range(len(parts) + 1):
                dirs.setdefault("/".join(parts[:i]), {"direct": 0, "total": 0})["total"] += r["n"]
        return [{"path": p, "direct": c["direct"], "total": c["total"]} for p, c in sorted(dirs.items())]

    def folders(self):
        with self._lock:
            rows = self._db.execute(
                """SELECT f.id, f.name, f.parent_id,
                          (SELECT COUNT(*) FROM memberships m JOIN images i ON i.id=m.image_id
                            WHERE m.folder_id=f.id AND i.state='ok') AS count
                   FROM folders f ORDER BY f.name COLLATE NOCASE""").fetchall()
        return [dict(r) for r in rows]

    def trash_count(self):
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM images WHERE state='trash'").fetchone()[0]

    def list_images(self, source, offset=0, limit=200):
        """One page of images for `dir:<path>`, `folder:<id>` or `trash`, newest first."""
        kind, _, arg = source.partition(":")
        if kind == "dir":
            if arg:
                where, params = "i.state='ok' AND (i.dir=? OR i.dir LIKE ? ESCAPE '\\')", [arg, _like_prefix(arg)]
            else:
                where, params = "i.state='ok'", []
            frm = "images i"
        elif kind == "folder":
            frm = "images i JOIN memberships m ON m.image_id=i.id"
            where, params = "i.state='ok' AND m.folder_id=?", [int(arg)]
        elif kind == "trash":
            frm, where, params = "images i", "i.state='trash'", []
        else:
            raise StoreError(f"unknown source {source!r}")
        with self._lock:
            total = self._db.execute(f"SELECT COUNT(*) FROM {frm} WHERE {where}", params).fetchone()[0]
            rows = self._db.execute(
                f"SELECT i.id, i.relpath, i.name, i.width, i.height, i.error IS NOT NULL AS broken "
                f"FROM {frm} WHERE {where} {_ORDER} LIMIT ? OFFSET ?",
                params + [int(limit), int(offset)]).fetchall()
        return {"total": total, "items": [dict(r) for r in rows]}

    def get_image(self, image_id):
        with self._lock:
            r = self._db.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
            if r is None:
                raise StoreError("no such image")
            folder_ids = [x[0] for x in self._db.execute(
                "SELECT folder_id FROM memberships WHERE image_id=?", (image_id,))]
        out = dict(r)
        out["meta"] = json.loads(r["meta"]) if r["meta"] else None
        out["folders"] = folder_ids
        return out

    def file_path(self, image_id):
        """Absolute path of an image's bytes, wherever it currently lives."""
        with self._lock:
            r = self._db.execute("SELECT relpath, state, trash_path FROM images WHERE id=?",
                                 (image_id,)).fetchone()
        if r is None or r["state"] == "missing":
            raise StoreError("no such image")
        return r["trash_path"] if r["state"] == "trash" else os.path.join(self.outputs, r["relpath"])

    # ---- folders --------------------------------------------------------

    def _folder_exists(self, folder_id):
        return self._db.execute("SELECT 1 FROM folders WHERE id=?", (folder_id,)).fetchone() is not None

    def _check_name(self, name, parent_id, exclude_id=None):
        name = (name or "").strip()
        if not name:
            raise StoreError("folder name is empty")
        clash = self._db.execute(
            "SELECT id FROM folders WHERE name=? AND parent_id IS ? AND id IS NOT ?",
            (name, parent_id, exclude_id)).fetchone()
        if clash:
            raise StoreError(f"a folder named {name!r} already exists there")
        return name

    def create_folder(self, name, parent_id=None):
        with self._lock, self._db:
            if parent_id is not None and not self._folder_exists(parent_id):
                raise StoreError("no such parent folder")
            name = self._check_name(name, parent_id)
            cur = self._db.execute("INSERT INTO folders (name, parent_id, created) VALUES (?,?,?)",
                                   (name, parent_id, time.time()))
            return cur.lastrowid

    def rename_folder(self, folder_id, name):
        with self._lock, self._db:
            r = self._db.execute("SELECT parent_id FROM folders WHERE id=?", (folder_id,)).fetchone()
            if r is None:
                raise StoreError("no such folder")
            name = self._check_name(name, r["parent_id"], exclude_id=folder_id)
            self._db.execute("UPDATE folders SET name=? WHERE id=?", (name, folder_id))

    def reparent_folder(self, folder_id, parent_id):
        with self._lock, self._db:
            r = self._db.execute("SELECT name FROM folders WHERE id=?", (folder_id,)).fetchone()
            if r is None:
                raise StoreError("no such folder")
            ancestor = parent_id
            while ancestor is not None:
                if ancestor == folder_id:
                    raise StoreError("cannot move a folder into itself")
                row = self._db.execute("SELECT parent_id FROM folders WHERE id=?", (ancestor,)).fetchone()
                if row is None:
                    raise StoreError("no such parent folder")
                ancestor = row["parent_id"]
            self._check_name(r["name"], parent_id, exclude_id=folder_id)
            self._db.execute("UPDATE folders SET parent_id=? WHERE id=?", (parent_id, folder_id))

    def delete_folder(self, folder_id):
        """Remove a folder, its subfolders and their memberships. Images are untouched."""
        with self._lock, self._db:
            if self._db.execute("DELETE FROM folders WHERE id=?", (folder_id,)).rowcount == 0:
                raise StoreError("no such folder")

    def add_to_folder(self, folder_id, image_ids):
        with self._lock, self._db:
            if not self._folder_exists(folder_id):
                raise StoreError("no such folder")
            now = time.time()
            cur = self._db.executemany(
                "INSERT OR IGNORE INTO memberships (folder_id, image_id, added) "
                "SELECT ?, id, ? FROM images WHERE id=?",
                [(folder_id, now, int(i)) for i in image_ids])
            return cur.rowcount

    def remove_from_folder(self, folder_id, image_ids):
        with self._lock, self._db:
            cur = self._db.executemany("DELETE FROM memberships WHERE folder_id=? AND image_id=?",
                                       [(folder_id, int(i)) for i in image_ids])
            return cur.rowcount

    def move_between_folders(self, src_id, dst_id, image_ids):
        with self._lock, self._db:
            added = self.add_to_folder(dst_id, image_ids)
            if src_id != dst_id:
                self.remove_from_folder(src_id, image_ids)
            return added

    # ---- trash ----------------------------------------------------------

    def trash(self, image_ids):
        """Move images (and any sidecar .txt) aside. Memberships are kept for restore."""
        moved = 0
        for image_id in image_ids:
            with self._lock, self._db:
                r = self._db.execute("SELECT relpath, state FROM images WHERE id=?", (int(image_id),)).fetchone()
                if r is None or r["state"] != "ok":
                    continue
                src = os.path.join(self.outputs, r["relpath"])
                dest_dir = os.path.join(self.trash_dir, str(int(image_id)))
                os.makedirs(dest_dir, exist_ok=True)
                dest = os.path.join(dest_dir, os.path.basename(src))
                shutil.move(src, dest)
                sidecar = os.path.splitext(src)[0] + ".txt"
                if os.path.isfile(sidecar):
                    shutil.move(sidecar, os.path.join(dest_dir, os.path.basename(sidecar)))
                self._db.execute("UPDATE images SET state='trash', trash_path=?, trashed_at=? WHERE id=?",
                                 (dest, time.time(), int(image_id)))
                moved += 1
        return moved

    def restore(self, image_ids):
        restored, skipped = 0, []
        for image_id in image_ids:
            with self._lock, self._db:
                r = self._db.execute("SELECT relpath, state, trash_path FROM images WHERE id=?",
                                     (int(image_id),)).fetchone()
                if r is None or r["state"] != "trash":
                    continue
                dest = os.path.join(self.outputs, r["relpath"])
                if os.path.exists(dest):
                    skipped.append(int(image_id))
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.move(r["trash_path"], dest)
                src_dir = os.path.dirname(r["trash_path"])
                for leftover in os.listdir(src_dir):
                    shutil.move(os.path.join(src_dir, leftover), os.path.join(os.path.dirname(dest), leftover))
                os.rmdir(src_dir)
                st = os.stat(dest)
                self._db.execute(
                    "UPDATE images SET state='ok', trash_path=NULL, trashed_at=NULL, mtime=?, size=? WHERE id=?",
                    (st.st_mtime, st.st_size, int(image_id)))
                restored += 1
        return {"restored": restored, "skipped": skipped}

    def purge(self, image_ids=None):
        """Permanently delete trashed images: the given ids, or the whole trash."""
        with self._lock:
            if image_ids is None:
                rows = self._db.execute("SELECT id, trash_path FROM images WHERE state='trash'").fetchall()
            else:
                rows = [r for i in image_ids for r in self._db.execute(
                    "SELECT id, trash_path FROM images WHERE id=? AND state='trash'", (int(i),))]
            for r in rows:
                shutil.rmtree(os.path.dirname(r["trash_path"]), ignore_errors=True)
            with self._db:
                self._db.executemany("DELETE FROM images WHERE id=?", [(r["id"],) for r in rows])
        return len(rows)


def _like_prefix(path):
    return path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"
