"""Snapshots, restores and the markdown export — the data lifecycle verbs.

Three facilities, and awm invents none of them. Trilium copies its own database
under its sync mutex, keeps its own per-note revisions, and exports its own
tree as markdown. What this module adds is where the results go: into the
person's `userdata` scope, pinned by DVC and committed by git, so a knowledge
base has the same history as code.

**Three kinds of copy, and they are not interchangeable.**

  - A *rolling backup* is Trilium's own daily, weekly and monthly rotation. It
    lives beside the live database, is overwritten on a schedule, and is pinned
    by nothing.
  - A *snapshot* is a named database copy this module moved into the DVC chunk.
    It is written once under a name that is never reused, which is what makes a
    read-only hardlink into the shared cache safe. This is the restore path.
  - The *export* is markdown, and it is a **derived view**. Trilium stores
    markup as HTML, so the export is a conversion and importing it back is
    lossy. It exists to be read, diffed, searched and merged by a person. It is
    not how a vault is recovered.

**Why `restore` is whole-vault and not per-note.** Restoring one note's
revision is `POST /api/revisions/{id}/restore`, on the internal API, behind
`checkApiAuth` — which wants an express session, which wants the person's
password. This service holds a token and not a password, on purpose. So the
single-note restore stays where the person's own session already is: one click
in Trilium's revisions dialog. What this module restores is the whole database.
"""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from awm.trilium import etapi, instances
from awm.trilium.instances import Instance

log = logging.getLogger("awm.trilium.vault")

#: Trilium's own backup-name sanitiser, mirrored so the name we ask for is the
#: name we then go looking for. See `backupNow` in `apps/server/src/backup_provider.ts`.
_NAME_STRIP = re.compile(r"[^a-zA-Z0-9_-]")

#: A plain database copy, and a compressed or encrypted container. Only the
#: first can be restored by moving a file — see `restore_files`.
PLAIN_EXT = ".db"
CONTAINER_EXT = ".tnbackup"

#: The chunk, relative to the scope worktree. One path, named once, because the
#: pin, the commit and the `.gitignore` DVC writes all have to agree on it.
CHUNK = "data/backups"

GIT_TIMEOUT_S = 120
DVC_TIMEOUT_S = 1800


# -- git and dvc ------------------------------------------------------------
#
# The policy for how awm uses DVC — the shared content-addressed cache, the
# merge driver, the hooks — lives in `awm/scopes/data_dvc.py` and is not
# restated here. This is only the little that a service needs to run the two
# binaries against one scope worktree.

_DVC_FALLBACKS = (
    Path.home() / "lib/miniforge3/envs/dvc/bin/dvc",
    Path.home() / "lib/miniforge3/envs/awm/bin/dvc",
    Path("/usr/local/bin/dvc"),
    Path("/usr/bin/dvc"),
)


def dvc_bin() -> str | None:
    """Absolute `dvc`, or None. Same order as the scopes service: override,
    PATH, then the known env locations — a service under systemd has a minimal
    PATH and dvc lives in its own mamba env."""
    override = os.environ.get("AWM_DVC_BIN")
    if override and Path(override).is_file() and os.access(override, os.X_OK):
        return override
    found = shutil.which("dvc")
    if found:
        return found
    for cand in _DVC_FALLBACKS:
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def _env() -> dict[str, str]:
    env = dict(os.environ)
    binp = dvc_bin()
    if binp:
        env["PATH"] = str(Path(binp).parent) + os.pathsep + env.get("PATH", "")
    # A daemon has no global git identity, and without one every commit fails.
    env.setdefault("GIT_AUTHOR_NAME", "awm")
    env.setdefault("GIT_AUTHOR_EMAIL", "awm@localhost")
    env.setdefault("GIT_COMMITTER_NAME", "awm")
    env.setdefault("GIT_COMMITTER_EMAIL", "awm@localhost")
    return env


def _git(scope: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(scope), *args], capture_output=True,
                          text=True, env=_env(), timeout=GIT_TIMEOUT_S)


def _dvc(scope: Path, *args: str) -> subprocess.CompletedProcess:
    binp = dvc_bin()
    if not binp:
        return subprocess.CompletedProcess([], 127, "", "dvc not found (set AWM_DVC_BIN)")
    return subprocess.run([binp, *args], cwd=str(scope), capture_output=True,
                          text=True, env=_env(), timeout=DVC_TIMEOUT_S)


def _out(r: subprocess.CompletedProcess) -> str:
    return ((r.stdout or "") + (r.stderr or "")).strip()


def _is_dvc_repo(scope: Path) -> bool:
    """The opt-in is the checkout: `.dvc/config` is tracked, so the branch says
    whether this worktree is on DVC."""
    return (scope / ".dvc" / "config").is_file()


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _pin_and_commit(inst: Instance, message: str, paths: list[str]) -> dict[str, Any]:
    """Pin the snapshot chunk, stage `paths`, and make one commit of the lot.

    One commit, not two, so the markdown and the database pin that produced it
    move together — a tree whose text says one thing and whose pin says another
    is worse than either alone.
    """
    scope = inst.scope
    report: dict[str, Any] = {"committed": False}
    staged = list(paths)

    if inst.snapshots_dir.is_dir() and any(inst.snapshots_dir.iterdir()):
        if not _is_dvc_repo(scope):
            report["pin"] = "skipped: worktree does not track a .dvc/config"
        else:
            r = _dvc(scope, "add", CHUNK)
            report["pin"] = "ok" if r.returncode == 0 else f"failed: {_out(r)[-400:]}"
            if r.returncode == 0:
                # `dvc add` writes both: the pin, and a `.gitignore` telling git
                # to leave the chunk itself alone. Committing one without the
                # other leaves the binaries staged as blobs on the next commit.
                staged += [f"{CHUNK}.dvc", "data/.gitignore"]

    add = _git(scope, "add", "--", *staged)
    if add.returncode != 0:
        report["error"] = f"git add: {_out(add)[-400:]}"
        return report

    if not _out(_git(scope, "diff", "--cached", "--name-only")):
        report["detail"] = "nothing changed"
        return report

    commit = _git(scope, "commit", "-m", message)
    if commit.returncode != 0:
        report["error"] = f"git commit: {_out(commit)[-400:]}"
        return report
    report["committed"] = True
    report["rev"] = _out(_git(scope, "rev-parse", "--short", "HEAD"))
    report["message"] = message
    return report


# -- snapshots --------------------------------------------------------------


def _find_backup(directory: Path, name: str) -> Path | None:
    for ext in (PLAIN_EXT, CONTAINER_EXT):
        cand = directory / f"backup-{name}{ext}"
        if cand.is_file():
            return cand
    return None


def snapshot(inst: Instance, name: str | None = None, *,
             note_id: str | None = None, commit: bool = True) -> dict[str, Any]:
    """Take a named point this vault can be returned to.

    With `note_id`, that is one note's revision — Trilium's own machinery, the
    same thing its revisions dialog lists, and the thing a person restores with
    one click there.

    Without one, it is the whole database: Trilium copies it under its sync
    mutex, and the copy is moved into the DVC chunk under a name carrying a UTC
    timestamp. The timestamp is not decoration. A pinned file is a read-only
    hardlink into the shared cache, so a name that repeated would be a write
    that fails — every snapshot gets a name no other snapshot has.
    """
    api = etapi.client(inst)

    if note_id:
        api.save_revision(note_id, description=name or "awm snapshot")
        return {"kind": "revision", "user": inst.user, "note_id": note_id,
                "revisions": len(api.revisions(note_id))}

    label = _NAME_STRIP.sub("", name or "snapshot") or "snapshot"
    snap_name = f"{label}-{_stamp()}"

    inst.rolling_dir.mkdir(parents=True, exist_ok=True)
    api.backup(snap_name)

    produced = _find_backup(inst.rolling_dir, snap_name)
    if produced is None:
        raise RuntimeError(
            f"Trilium reported the backup written and nothing named "
            f"backup-{snap_name}.* is in {inst.rolling_dir}")

    inst.snapshots_dir.mkdir(parents=True, exist_ok=True)
    dest = inst.snapshots_dir / produced.name
    if dest.exists():
        raise FileExistsError(f"{dest} already exists — refusing to overwrite a pin")
    # Move rather than copy: same filesystem, so it is a rename, and it keeps
    # the one-off names out of the directory Trilium rotates.
    shutil.move(str(produced), str(dest))

    out: dict[str, Any] = {
        "kind": "database", "user": inst.user, "snapshot": dest.stem,
        "file": str(dest), "bytes": dest.stat().st_size,
        "restorable": dest.suffix == PLAIN_EXT,
    }
    if commit:
        out["git"] = _pin_and_commit(
            inst, f"trilium/{inst.user}: snapshot {dest.stem}", [])
    return out


def _describe(path: Path, kind: str) -> dict[str, Any]:
    st = path.stat()
    return {
        "name": path.stem, "file": str(path), "kind": kind,
        "bytes": st.st_size,
        "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc)
                            .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "restorable": path.suffix == PLAIN_EXT,
    }


def snapshots(inst: Instance) -> dict[str, Any]:
    """Every database copy this user has, newest first, by kind.

    Both kinds are listed because only one of them is durable. A rolling backup
    is overwritten on a schedule and pinned by nothing, so finding the state you
    want there is a race against the next rotation.
    """
    found: list[dict[str, Any]] = []
    for directory, kind in ((inst.snapshots_dir, "snapshot"),
                            (inst.rolling_dir, "rolling")):
        try:
            entries = list(directory.glob("backup-*.*"))
        except OSError:
            continue
        found += [_describe(p, kind) for p in entries if p.is_file()]
    found.sort(key=lambda d: d["modified"], reverse=True)
    return {
        "user": inst.user,
        "snapshots": found,
        "pinned_dir": str(inst.snapshots_dir),
        "rolling_dir": str(inst.rolling_dir),
    }


def resolve_snapshot(inst: Instance, name: str) -> Path:
    """The file a snapshot name refers to. Accepts the stem or the filename."""
    stem = name[:-len(PLAIN_EXT)] if name.endswith(PLAIN_EXT) else name
    stem = stem[:-len(CONTAINER_EXT)] if stem.endswith(CONTAINER_EXT) else stem
    bare = stem[len("backup-"):] if stem.startswith("backup-") else stem
    for directory in (inst.snapshots_dir, inst.rolling_dir):
        hit = _find_backup(directory, bare)
        if hit:
            return hit
    raise FileNotFoundError(
        f"no snapshot named {name!r} for {inst.user!r} — `trilium snapshots` lists them")


def restore_files(inst: Instance, source: Path) -> dict[str, Any]:
    """Put `source` in place as the live database. The server must be stopped.

    Nothing is deleted. The database being replaced, together with its
    write-ahead log and shared-memory file, is moved into a timestamped
    directory under `live/superseded/` — restoring the wrong snapshot is then
    undone by moving three files back.

    WARNING: this replaces the whole vault. Every note written since the
    snapshot was taken is in the superseded copy and nowhere else.
    """
    if source.suffix == CONTAINER_EXT:
        raise ValueError(
            f"{source.name} is a compressed or encrypted backup container, not a "
            f"plain database. Restore it through Trilium's own restore screen, "
            f"which holds the passphrase.")
    if instances.listening(inst.upstream_port):
        raise RuntimeError(
            f"{inst.user}'s server is still listening on {inst.upstream_port}. "
            f"Stop it before restoring — a swap under a live SQLite connection "
            f"leaves the process writing to a file nobody can see.")

    holding = inst.superseded_dir / _stamp()
    holding.mkdir(parents=True, exist_ok=True)
    moved = []
    for suffix in ("", "-wal", "-shm"):
        old = Path(str(inst.document_db) + suffix)
        if old.exists():
            shutil.move(str(old), str(holding / old.name))
            moved.append(old.name)

    inst.data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, inst.document_db)
    # A pinned file is a read-only hardlink into the shared cache. `copy2`
    # carries that mode across to a database the server has to write to, and
    # the mode is the only thing about the source that must not survive.
    os.chmod(inst.document_db, 0o600)

    return {"user": inst.user, "restored_from": str(source),
            "superseded": str(holding), "moved_aside": moved,
            "bytes": inst.document_db.stat().st_size}


# -- the markdown export ----------------------------------------------------


def _safe_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Entries that stay inside the destination. Absolute paths and `..` are
    dropped rather than sanitised, because a zip that contains one is not a
    Trilium export and guessing what it meant is how a traversal lands."""
    keep = []
    for info in zf.infolist():
        name = info.filename
        if name.startswith(("/", "\\")) or ".." in Path(name).parts:
            log.warning("trilium: dropping export entry %r", name)
            continue
        keep.append(info)
    return keep


def export(inst: Instance, *, note_id: str = "root",
           commit: bool = True) -> dict[str, Any]:
    """Write the vault out as markdown into the scope, and commit it.

    The tree is replaced rather than merged. It is a derived view of what
    Trilium holds, so a file that survived only because a previous export made
    it would be a lie about the vault's current contents.
    """
    api = etapi.client(inst)
    blob = api.export_zip(note_id=note_id, fmt="markdown")

    staging = inst.scope / ".notes.incoming"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            members = _safe_members(zf)
            zf.extractall(staging, members=members)
        files = [p for p in staging.rglob("*") if p.is_file()]
        if not files:
            raise RuntimeError(
                "the export contained no files — refusing to replace notes/ with "
                "nothing. An empty vault exports at least its metadata.")
        # Measured here, before the rename: these paths stop existing the moment
        # the staging tree becomes `notes/`.
        count, total = len(files), sum(p.stat().st_size for p in files)

        retired = inst.scope / ".notes.retired"
        shutil.rmtree(retired, ignore_errors=True)
        if inst.notes_dir.exists():
            inst.notes_dir.rename(retired)
        staging.rename(inst.notes_dir)
        shutil.rmtree(retired, ignore_errors=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    out: dict[str, Any] = {
        "user": inst.user, "note_id": note_id, "notes_dir": str(inst.notes_dir),
        "files": count, "bytes": total,
        "derived": "markdown converted from Trilium's HTML — read it, do not "
                   "restore from it",
    }
    if commit:
        out["git"] = _pin_and_commit(
            inst, f"trilium/{inst.user}: export {count} files", ["notes"])
    return out
