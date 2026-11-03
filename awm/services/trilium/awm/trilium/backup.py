"""The vault's nightly copy to mizar, and the check that holds a bad night (fleet.md R105 items 2-3).

deneb writes a consistent copy with ``VACUUM INTO``, never a plain file copy.
mizar pulls it over its edge, compares it with the last approved copy, and
keeps three zstd copies: yesterday, last week and last month. The weekly copy
is replaced once a week and the monthly once a month, each by the next younger
copy, so the weekly is up to 8 days old and the monthly up to 38. A night that
trips a check is held for Tony on the approvals queue and rotates nothing.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

SLOTS = ("daily", "weekly", "monthly")
WEEK_S = 7 * 86400
MONTH_S = 30 * 86400
HISTORY_NIGHTS = 30


def consistent_copy(src: Path, dst: Path) -> Path:
    """Write a consistent copy of the live database ``src`` to ``dst`` (deneb side)."""
    dst.unlink(missing_ok=True)
    con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        con.execute("VACUUM INTO ?", (str(dst),))
    finally:
        con.close()
    return dst


@dataclass
class Facts:
    """What the checks read from one copy."""

    live: dict[str, str]          # noteId -> blobId, for notes not soft-deleted
    deleted: set[str]             # noteIds soft-deleted
    scripts: dict[str, str]       # noteId -> blobId, for script notes
    tokens: set[str]              # ETAPI token ids
    max_change_id: int


def _is_script(note_type: str, mime: str) -> bool:
    return note_type == "code" and "javascript" in (mime or "") and "env=" in (mime or "")


def read_facts(db: Path) -> Facts:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        live, deleted, scripts = {}, set(), {}
        for note_id, blob_id, is_deleted, note_type, mime in con.execute(
                "SELECT noteId, blobId, isDeleted, type, mime FROM notes"):
            if is_deleted:
                deleted.add(note_id)
                continue
            live[note_id] = blob_id
            if _is_script(note_type, mime):
                scripts[note_id] = blob_id
        tokens = {r[0] for r in con.execute("SELECT etapiTokenId FROM etapi_tokens WHERE isDeleted = 0")}
        max_change = con.execute("SELECT COALESCE(MAX(id), 0) FROM entity_changes").fetchone()[0]
    finally:
        con.close()
    return Facts(live, deleted, scripts, tokens, int(max_change))


def _p95(values: list[int]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))])


@dataclass
class Verdict:
    reasons: list[str] = field(default_factory=list)
    changes: int = 0

    @property
    def hold(self) -> bool:
        return bool(self.reasons)


def compare(new: Facts, approved: Facts, nightly_changes: list[int]) -> Verdict:
    """The R105 item 3 checks of tonight's copy against the last approved one."""
    v = Verdict(changes=max(0, new.max_change_id - approved.max_change_id))
    n = max(1, len(approved.live))
    gone = [i for i in approved.live if i not in new.live]
    if len(gone) > max(25, 0.02 * n):
        v.reasons.append(f"{len(gone)} notes are gone")
    changed = [i for i, b in approved.live.items() if i in new.live and new.live[i] != b]
    if len(changed) > max(100, 0.05 * n):
        v.reasons.append(f"{len(changed)} notes changed content")
    limit = max(5000, 3 * _p95(nightly_changes[-HISTORY_NIGHTS:]))
    if v.changes > limit:
        v.reasons.append(f"{v.changes} entity changes tonight, over {limit:.0f}")
    erased = [i for i in gone if i not in new.deleted]
    if erased:
        v.reasons.append(f"{len(erased)} notes erased without a soft delete")
    if new.tokens - approved.tokens:
        v.reasons.append("a new API token exists")
    if any(approved.scripts.get(i) != b for i, b in new.scripts.items()):
        v.reasons.append("a script note is new or changed")
    return v


def _zstd(src: Path, dst: Path) -> None:
    subprocess.run(["zstd", "-q", "-f", "-19", str(src), "-o", str(dst)], check=True)


def _unzstd(src: Path, dst: Path) -> None:
    subprocess.run(["zstd", "-q", "-d", "-f", str(src), "-o", str(dst)], check=True)


class Store:
    """mizar's copies: ``<slot>.db.zst`` for each slot, ``approved.db`` as the
    baseline, ``held-<stamp>.db.zst`` for a held night, and ``state.json``."""

    def __init__(self, root: Path, clock: Callable[[], float] = time.time) -> None:
        self.root = root
        self.clock = clock
        root.mkdir(parents=True, exist_ok=True)

    @property
    def _state_path(self) -> Path:
        return self.root / "state.json"

    def state(self) -> dict:
        try:
            return json.loads(self._state_path.read_text())
        except FileNotFoundError:
            return {"slots": {}, "nightly_changes": [], "held": []}

    def _save(self, state: dict) -> None:
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(self._state_path)

    def _rotate(self, copy: Path, state: dict) -> None:
        now = self.clock()
        slots = state["slots"]
        if "daily" in slots:
            if now - slots.get("weekly", 0) >= WEEK_S:
                if now - slots.get("monthly", 0) >= MONTH_S and "weekly" in slots:
                    shutil.copyfile(self.root / "weekly.db.zst", self.root / "monthly.db.zst")
                    slots["monthly"] = slots["weekly"]
                shutil.copyfile(self.root / "daily.db.zst", self.root / "weekly.db.zst")
                slots["weekly"] = slots["daily"]
        _zstd(copy, self.root / "daily.db.zst")
        slots["daily"] = now
        shutil.copyfile(copy, self.root / "approved.db")

    def accept(self, copy: Path) -> Verdict:
        """Check tonight's copy. Rotate it in when it passes, else hold it."""
        state = self.state()
        new = read_facts(copy)
        baseline = self.root / "approved.db"
        if not baseline.exists():
            self._rotate(copy, state)
            self._save(state)
            return Verdict()
        verdict = compare(new, read_facts(baseline), state["nightly_changes"])
        state["nightly_changes"] = (state["nightly_changes"] + [verdict.changes])[-HISTORY_NIGHTS:]
        if verdict.hold:
            stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(self.clock()))
            _zstd(copy, self.root / f"held-{stamp}.db.zst")
            state["held"].append({"stamp": stamp, "reasons": verdict.reasons})
        else:
            self._rotate(copy, state)
        self._save(state)
        return verdict

    def release(self, stamp: str) -> None:
        """Tony approved the held night ``stamp``: it becomes the baseline and rotates in."""
        state = self.state()
        held = self.root / f"held-{stamp}.db.zst"
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "vault.db"
            _unzstd(held, copy)
            self._rotate(copy, state)
        held.unlink()
        state["held"] = [h for h in state["held"] if h["stamp"] != stamp]
        self._save(state)


def nightly(fetch: Callable[[Path], None], store: Store,
            hold: Callable[[list[str]], None] | None = None) -> Verdict:
    """mizar's nightly run: ``fetch`` writes deneb's consistent copy to a path,
    the store checks and keeps it, and ``hold`` files the approval request."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "vault.db"
        fetch(copy)
        verdict = store.accept(copy)
    if verdict.hold and hold is not None:
        hold(verdict.reasons)
    return verdict


def _ssh_fetch(host: str, path: str) -> Callable[[Path], None]:
    def fetch(dst: Path) -> None:
        with open(dst, "wb") as out:
            subprocess.run(["ssh", "-o", "BatchMode=yes", host, "cat", path], stdout=out, check=True)
    return fetch


def _file_hold(host: str) -> Callable[[list[str]], None]:
    def hold(reasons: list[str]) -> None:
        from awm.trustpolicy.requests import file_request
        file_request("vault_hold", node=host,
                     reason="Tonight's vault copy tripped a check: " + "; ".join(reasons))
    return hold


def main(argv: list[str] | None = None) -> int:
    """``copy --src DB --dst PATH`` on deneb; ``pull --host H --path P --store DIR`` on mizar."""
    import argparse
    ap = argparse.ArgumentParser(prog="python -m awm.trilium.backup")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("copy")
    c.add_argument("--src", type=Path, required=True)
    c.add_argument("--dst", type=Path, required=True)
    p = sub.add_parser("pull")
    p.add_argument("--host", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--store", type=Path, required=True)
    p.add_argument("--release", metavar="STAMP")
    a = ap.parse_args(argv)
    if a.cmd == "copy":
        consistent_copy(a.src, a.dst)
        return 0
    store = Store(a.store)
    if a.release:
        store.release(a.release)
        return 0
    verdict = nightly(_ssh_fetch(a.host, a.path), store, hold=_file_hold(a.host))
    print(json.dumps({"held": verdict.hold, "reasons": verdict.reasons, "changes": verdict.changes}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
