"""Move aged sessions into the archive, and prune the archive.

Two passes, deliberately separate because they differ in reversibility.

``archive`` gzips each aged session into the mirrored archive tree and removes
the original. Reversible: :func:`restore` puts it back.

``prune`` deletes from the archive. Not reversible, so it is off unless a
retention is configured, and it reports what it would remove before removing
anything.

THE SESSION IS THE UNIT, NOT THE FILE
The shell script this replaces walked ``find -maxdepth 2 -name '*.jsonl'``, which
reaches a session's transcript and not its sidecar. It therefore archived the
conversation and left ``tool-results/`` and ``subagents/`` behind for good — 221
orphaned directories holding 413 MB on altair when this service was written.
Everything here addresses a :class:`~awm.transcripts.layout.Session`, which is
both parts, so that failure cannot recur.

CAUTION: an aged session is decided by the newest file anywhere in it, so a live
conversation whose subagent wrote recently is never swept even if its transcript
looks old. A session a Claude Code process still holds open is skipped outright,
however old it looks — see :mod:`awm.transcripts.live`.
"""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import time
from pathlib import Path

from awm.transcripts import live
from awm.transcripts.layout import ARCHIVE, PROJECTS, Session, sessions

log = logging.getLogger("awm.transcripts.sweep")

DEFAULT_RETENTION_DAYS = 7


def aged(root: Path, days: float, *, skip_live: bool = False) -> list[Session]:
    """Sessions under ``root`` whose newest file is older than ``days``.

    ``skip_live`` excludes conversations a Claude Code process still holds open.
    A background job parked for a fortnight is idle, not finished, and archiving
    its transcript would leave it writing to a path that no longer exists.
    """
    cutoff = time.time() - days * 86400
    out = [s for s in sessions(root) if 0 < s.mtime < cutoff]
    if skip_live:
        held = live.session_ids()
        out = [s for s in out if s.session_id not in held]
    return out


def _gzip_into(src: Path, dest: Path) -> int:
    """Compress ``src`` to ``dest`` through a temporary file in the same dir.

    The rename is what makes an interrupted sweep safe: a reader never sees a
    partial member, and the source is removed only after the rename lands.

    The source's modification time is carried onto the archived copy, so a
    session's age means the same thing on both sides and ``prune --days N``
    reads as "older than N days" rather than "archived more than N days ago".

    CAUTION: the shell script this service replaces did not do this, so every
    file in the archive it built carries its archival date instead. Pruning that
    legacy portion measures from when it was swept.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    st = src.stat()
    with open(src, "rb") as fin, gzip.open(tmp, "wb") as fout:
        shutil.copyfileobj(fin, fout, length=1 << 20)
    os.utime(tmp, (st.st_atime, st.st_mtime))
    os.replace(tmp, dest)
    return dest.stat().st_size


def _carry_mtime(src: Path, dest: Path) -> None:
    """Give ``dest`` ``src``'s timestamps, so a restored session is not new."""
    try:
        st = src.stat()
        os.utime(dest, (st.st_atime, st.st_mtime))
    except OSError:
        pass


def archive_one(s: Session, *, dry_run: bool = False) -> dict:
    """Archive one session's transcript and sidecar together."""
    out = {"project": s.project, "session": s.session_id,
           "bytes_before": s.bytes, "bytes_after": 0, "files": 0,
           "sidecar": s.sidecar is not None, "orphan": s.transcript is None}
    if dry_run:
        return out
    if s.transcript is not None:
        dest = ARCHIVE / s.project / (s.session_id + ".jsonl.gz")
        out["bytes_after"] += _gzip_into(s.transcript, dest)
        out["files"] += 1
        s.transcript.unlink()
    if s.sidecar is not None:
        for dirpath, _, names in os.walk(s.sidecar):
            rel = Path(dirpath).relative_to(s.sidecar)
            for n in names:
                dest = (ARCHIVE / s.project / s.session_id / rel / (n + ".gz"))
                out["bytes_after"] += _gzip_into(Path(dirpath) / n, dest)
                out["files"] += 1
        shutil.rmtree(s.sidecar, ignore_errors=True)
    return out


def archive(*, days: float = DEFAULT_RETENTION_DAYS,
            dry_run: bool = False) -> dict:
    """Archive every session older than ``days``.

    A failure on one session is logged and the sweep continues: one unreadable
    file must not stop the other several hundred from being swept.
    """
    started = time.time()
    done, failed = [], []
    for s in aged(PROJECTS, days, skip_live=True):
        try:
            done.append(archive_one(s, dry_run=dry_run))
        except OSError as exc:
            log.warning("transcripts: could not archive %s/%s: %s",
                        s.project, s.session_id, exc)
            failed.append({"project": s.project, "session": s.session_id,
                           "error": str(exc)})
    return {
        "dry_run": dry_run,
        "retention_days": days,
        "sessions": len(done),
        "orphans": sum(1 for d in done if d["orphan"]),
        "files": sum(d["files"] for d in done),
        "bytes_before": sum(d["bytes_before"] for d in done),
        "bytes_after": sum(d["bytes_after"] for d in done),
        "failed": failed,
        "seconds": round(time.time() - started, 2),
    }


def prune(*, days: float, dry_run: bool = True) -> dict:
    """Delete archived sessions older than ``days``.

    The only irreversible operation in this service. ``dry_run`` defaults to
    True here and nowhere else, so the careless call reports rather than
    deletes, and a caller has to say the word to lose anything.
    """
    started = time.time()
    victims = aged(ARCHIVE, days)
    freed = sum(s.bytes for s in victims)
    if not dry_run:
        for s in victims:
            if s.transcript is not None:
                s.transcript.unlink(missing_ok=True)
            if s.sidecar is not None:
                shutil.rmtree(s.sidecar, ignore_errors=True)
    return {
        "dry_run": dry_run,
        "retention_days": days,
        "sessions": len(victims),
        "bytes_freed": freed,
        "seconds": round(time.time() - started, 2),
    }


def restore(project: str, session_id: str) -> dict:
    """Put one archived session back into the live tree.

    Refuses rather than overwrites when the live copy already exists. A restore
    that clobbered a live conversation would be the one way this service could
    destroy the thing it exists to preserve.
    """
    src_t = ARCHIVE / project / (session_id + ".jsonl.gz")
    src_d = ARCHIVE / project / session_id
    if not src_t.exists() and not src_d.is_dir():
        raise FileNotFoundError(f"{project}/{session_id} is not in the archive")
    dst_t = PROJECTS / project / (session_id + ".jsonl")
    dst_d = PROJECTS / project / session_id
    if dst_t.exists() or dst_d.exists():
        raise FileExistsError(
            f"{project}/{session_id} already exists in the live tree; "
            f"refusing to overwrite it")

    files = 0
    if src_t.exists():
        dst_t.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(src_t, "rb") as fin, open(dst_t, "wb") as fout:
            shutil.copyfileobj(fin, fout, length=1 << 20)
        _carry_mtime(src_t, dst_t)
        files += 1
    if src_d.is_dir():
        for dirpath, _, names in os.walk(src_d):
            rel = Path(dirpath).relative_to(src_d)
            for n in names:
                if not n.endswith(".gz"):
                    continue
                out = dst_d / rel / n[: -len(".gz")]
                out.parent.mkdir(parents=True, exist_ok=True)
                src = Path(dirpath) / n
                with gzip.open(src, "rb") as fin, open(out, "wb") as fout:
                    shutil.copyfileobj(fin, fout, length=1 << 20)
                _carry_mtime(src, out)
                files += 1
    return {"project": project, "session": session_id, "files": files,
            "path": str(dst_t)}
