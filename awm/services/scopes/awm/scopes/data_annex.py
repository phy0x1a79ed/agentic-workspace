"""git-annex data layer — per-scope isolated, versioned, transactional data.

Code already gets concurrency from git worktrees: every scope has its own
checkout and reconciling two scopes is an explicit merge. **Data got none of
that** — every scope's ``.awm/data`` was a symlink into one naked shared
directory, so two scopes writing the same path silently destroyed each other.

This module closes the gap without changing a single caller. ``.awm/data``
stays exactly where it is and keeps resolving; it just stops being a symlink to
a shared directory and becomes a **git-annex clone** of that directory, checked
out on a per-scope data branch. ``.awm/`` is already gitignored, so the clone is
invisible to the project's code repository: no ``.gitmodules``, no tracked data
path, no dirty worktree, no gitlink.

Layout::

    <workspace>/data/<project>/       canonical repo — non-bare, on `main`
                                      (unchanged path: every absolute path and
                                      symlink pointing here still resolves)
    projects/<project>/<scope>/.awm/data/
                                      clone of the above, on branch scope/<scope>

Why a clone and not a submodule: a submodule inside a *secondary* git worktree
makes git write a **relative** ``core.worktree`` that git-annex resolves from a
different base, so every annex command dies with ``changeWorkingDirectory: does
not exist``. A plain clone has an ordinary ``.git`` and is immune. (Preserved in
``projects/annex-poc/`` in case anyone reaches for the submodule form again.)

Three settings make isolation free:

* ``annex.hardlink`` — content is hardlinked from the same-filesystem canonical
  store instead of copied, so N scopes holding the same 5 GB dataset cost one
  copy.
* ``untrust here`` — mandatory once hardlinking: a hardlink is not an
  independent copy, so this clone must never be counted as one.
* ``annex.largefiles`` (in the canonical ``.gitattributes``, inherited by every
  clone) — small text stays in ordinary git; only bulk goes to the annex.

**Opt-in is the existence of the canonical annex repo.** A project whose
``data/<project>`` is still a naked directory keeps the old shared symlink,
byte for byte. :func:`init_project_data` is what flips a project over. There is
also a global kill switch, ``AWM_DATA_ANNEX=0``.

**Degrades, never fails.** git-annex is not on the daemon's PATH (it lives in
its own mamba env), so every entry point resolves the binary first and falls
back to the legacy symlink rather than breaking scope creation.
"""

from __future__ import annotations

import fcntl
import logging
import os
import shutil
import stat
import subprocess
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from awm import config as _config

log = logging.getLogger("awm.scopes.data_annex")

# The canonical data repo's branch. Scope branches merge into this, and this is
# what every non-scope consumer of <workspace>/data/<project> sees on disk.
CANONICAL_BRANCH = "main"

# Content is hardlinked from the canonical store, so "fetch everything" costs
# inodes rather than bytes and keeps a fresh scope looking exactly like today's
# shared directory. Above this many tracked files the hardlink pass is slow
# enough to be worth deferring, so the clone is left lazy and says so.
_AUTOGET_MAX = int(os.environ.get("AWM_DATA_ANNEX_AUTOGET_MAX", "20000"))

# Never annexed, therefore never pushed to the canonical repo and never carried
# off-site by the nightly mirror. `git annex add` skips gitignored paths, so an
# ignore rule is the whole mechanism: the file stays on local disk and leaves no
# trace in history. Secrets and vendored third-party checkouts only.
_CANONICAL_GITIGNORE = """\
# AWM-managed. Paths here are never annexed and never leave this machine.
# `git annex add` skips gitignored paths, so an entry here keeps a file out of
# both the repository and the off-site mirror while leaving it on local disk.

# --- secrets ---------------------------------------------------------------
secrets/
**/secrets/
.env
.env.*
**/.env
**/.env.*
**/.credentials.json
**/.nextflow/secrets/

# --- machine-local noise ---------------------------------------------------
.DS_Store
__pycache__/
*.pyc
"""

_VENDORED_HEADER = (
    "# AWM-managed. Third-party git checkouts found under this data directory.\n"
    "# Their trees are NOT annexed — you never annex a tree of git repos — so\n"
    "# they are pinned here by URL + commit and re-cloned on demand instead.\n"
    "# path\turl\tcommit\n"
)
_VENDORED_FILE = "VENDORED.tsv"

# Small text (configs, JSON, notes, manifests) stays in ordinary git so it
# diffs, merges and edits normally; only bulk becomes a read-only annex
# symlink. Lives in the canonical repo so every clone inherits it for free.
_CANONICAL_GITATTRIBUTES = "* annex.largefiles=largerthan=100kb\n"


# ---------------------------------------------------------------------------
# Binary resolution
# ---------------------------------------------------------------------------

_ANNEX_ENV_VAR = "AWM_ANNEX_BIN"

_ANNEX_FALLBACKS = (
    Path.home() / "lib/miniforge3/envs/annex/bin/git-annex",
    Path.home() / "lib/miniforge3/envs/awm/bin/git-annex",
    Path("/usr/bin/git-annex"),
    Path("/usr/local/bin/git-annex"),
)


@lru_cache(maxsize=1)
def annex_bin() -> str | None:
    """Absolute path to ``git-annex``, or None when it isn't installed.

    The scopes service runs under systemd with a minimal PATH and git-annex
    lives in its own mamba env, so ``shutil.which`` alone finds nothing. Order:
    explicit override, PATH, then the known env locations.
    """
    override = os.environ.get(_ANNEX_ENV_VAR)
    if override and Path(override).is_file() and os.access(override, os.X_OK):
        return override
    found = shutil.which("git-annex")
    if found:
        return found
    for cand in _ANNEX_FALLBACKS:
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def annex_available() -> bool:
    return annex_bin() is not None


def _env() -> dict:
    """Environment with git-annex's directory on PATH.

    ``git annex`` is dispatched by git as a subcommand, and git-annex installs
    hooks that shell out to ``git annex`` again — so *every* git call against an
    annex repo needs this, not just the ``annex`` ones.
    """
    env = dict(os.environ)
    binp = annex_bin()
    if binp:
        env["PATH"] = str(Path(binp).parent) + os.pathsep + env.get("PATH", "")
    # A daemon has no global git identity; without one every commit fails.
    env.setdefault("GIT_AUTHOR_NAME", "awm")
    env.setdefault("GIT_AUTHOR_EMAIL", "awm@localhost")
    env.setdefault("GIT_COMMITTER_NAME", "awm")
    env.setdefault("GIT_COMMITTER_EMAIL", "awm@localhost")
    return env


def _git(repo: Path, *args: str, timeout: int | None = None) -> subprocess.CompletedProcess:
    """Run git in ``repo`` with git-annex reachable."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env=_env(), timeout=timeout,
    )


def _annex(repo: Path, *args: str, timeout: int | None = None) -> subprocess.CompletedProcess:
    return _git(repo, "annex", *args, timeout=timeout)


def _out(r: subprocess.CompletedProcess) -> str:
    return ((r.stdout or "") + (r.stderr or "")).strip()


# ---------------------------------------------------------------------------
# Predicates + naming
# ---------------------------------------------------------------------------

def enabled() -> bool:
    """Global kill switch. ``AWM_DATA_ANNEX=0`` reverts every project to symlinks."""
    return os.environ.get("AWM_DATA_ANNEX", "1").lower() not in ("0", "off", "false", "no")


def canonical_repo(project: str) -> Path:
    """The project's canonical data repo — the same path it has always been.

    Read through the ``config`` module rather than binding ``DATA_DIR`` at
    import: tests redirect the workspace by monkeypatching the attribute, and a
    module-level copy would silently keep writing to the real ``data/``.
    """
    return _config.DATA_DIR / project


def data_branch(scope: str) -> str:
    return f"scope/{scope}"


def is_annex_repo(path: Path) -> bool:
    """True iff ``path`` is a git repo that has been ``git annex init``-ed."""
    git_dir = path / ".git"
    if not git_dir.exists():
        return False
    if git_dir.is_dir():
        return (git_dir / "annex").is_dir()
    # .git file (worktree/submodule form) — resolve it the slow way.
    r = _git(path, "rev-parse", "--git-dir")
    if r.returncode != 0:
        return False
    resolved = Path(r.stdout.strip())
    if not resolved.is_absolute():
        resolved = path / resolved
    return (resolved / "annex").is_dir()


def is_annex_project(project: str) -> bool:
    """Has this project been migrated? The repo's existence *is* the opt-in."""
    return enabled() and is_annex_repo(canonical_repo(project))


def current_branch(repo: Path) -> str:
    return (_git(repo, "branch", "--show-current").stdout or "").strip()


def head_rev(repo: Path) -> str:
    r = _git(repo, "rev-parse", "HEAD")
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def _is_clean(repo: Path) -> bool:
    r = _git(repo, "status", "--porcelain")
    return r.returncode == 0 and not (r.stdout or "").strip()


# ---------------------------------------------------------------------------
# Read-only protection
# ---------------------------------------------------------------------------

def chmod_writable(path: Path) -> None:
    """Restore write permission over a tree.

    git-annex marks object files **and their parent directories** read-only to
    protect content, so a plain ``rm -rf`` on a clone fails with *Permission
    denied* partway through — leaving a half-deleted worktree whose name then
    collides with the next ``worktree add``. Any teardown must call this first.
    """
    for root, dirs, files in os.walk(path, topdown=False):
        for name in dirs + files:
            p = Path(root) / name
            try:
                if not p.is_symlink():
                    p.chmod(p.stat().st_mode | stat.S_IWUSR)
            except OSError:
                pass
    try:
        path.chmod(path.stat().st_mode | stat.S_IWUSR)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Canonical repo
# ---------------------------------------------------------------------------

@contextmanager
def _promote_lock(project: str):
    """Serialise promotion into the canonical branch (advisory, per project).

    This is the *convenience* layer. The *correctness* layer is that promotion
    pushes fast-forward-only into a repo configured with
    ``receive.denyCurrentBranch=updateInstead``, so a racer that loses gets a
    rejected push rather than a clobber — with or without this lock.
    """
    repo = canonical_repo(project)
    lock_path = repo / ".git" / "awm-data-promote.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


def ensure_canonical_config(project: str) -> None:
    """Idempotently apply the settings the canonical repo must carry.

    ``updateInstead`` is the load-bearing one: the canonical repo keeps a
    working tree on ``main`` (so every existing absolute path into
    ``data/<project>`` still resolves), and pushing to a checked-out branch is
    refused by default. ``updateInstead`` accepts the push *and* updates the
    working tree — but only when that tree is clean, which is exactly the
    guarantee we want.
    """
    repo = canonical_repo(project)
    if not is_annex_repo(repo):
        return
    _git(repo, "config", "receive.denyCurrentBranch", "updateInstead")
    # Union merge driver for the location log; git-annex sets this itself on
    # init but a repo restored from a backup can be missing it.
    _git(repo, "config", "merge.git-annex.driver", "git-annex merge-driver %A %O %B")


# ---------------------------------------------------------------------------
# Migration: naked directory -> annex repo
# ---------------------------------------------------------------------------

def scan_vendored(repo: Path) -> list[tuple[str, str, str]]:
    """Find third-party git checkouts living inside the data directory.

    Annexing a tree of git repos is a category error — the inner ``.git``
    becomes thousands of annexed objects, and the checkout stops being a
    checkout. They are excluded and pinned by URL + commit instead, which is
    both smaller and more faithful: the repo is reproducible, the annexed copy
    would not have been.
    """
    found: list[tuple[str, str, str]] = []
    for root, dirs, _files in os.walk(repo):
        rootp = Path(root)
        if ".git" in dirs:
            if rootp != repo:
                rel = rootp.relative_to(repo)
                url = (_git(rootp, "remote", "get-url", "origin").stdout or "").strip()
                sha = (_git(rootp, "rev-parse", "HEAD").stdout or "").strip()
                found.append((str(rel), url or "-", sha or "-"))
                dirs[:] = []  # don't descend into a vendored checkout
                continue
            dirs.remove(".git")
    return sorted(found)


def _write_exclusions(repo: Path, vendored: list[tuple[str, str, str]]) -> None:
    """Regenerate the ignore rules + the vendored manifest, deterministically."""
    body = _CANONICAL_GITIGNORE
    if vendored:
        body += "\n# --- vendored third-party checkouts (see VENDORED.tsv) ---\n"
        body += "".join(f"/{path}/\n" for path, _u, _s in vendored)
    (repo / ".gitignore").write_text(body)
    (repo / ".gitattributes").write_text(_CANONICAL_GITATTRIBUTES)
    if vendored:
        (repo / _VENDORED_FILE).write_text(
            _VENDORED_HEADER
            + "".join(f"{p}\t{u}\t{s}\n" for p, u, s in vendored)
        )


def init_project_data(project: str, *, dry_run: bool = False,
                      description: str | None = None) -> dict:
    """Convert ``data/<project>`` from a naked directory into a git-annex repo.

    Idempotent: re-running against a converted directory only refreshes the
    ignore/attribute rules and commits anything new. The directory keeps its
    path and its working tree, so nothing that points at it breaks.

    Callers must ensure no job is mid-write: ``git annex add`` moves file
    content into the object store and replaces the original with a read-only
    symlink, which breaks any process holding that path open for writing.
    """
    repo = canonical_repo(project)
    report: dict = {"project": project, "path": str(repo), "dry_run": dry_run}

    if not annex_available():
        report["result"] = "unavailable"
        report["detail"] = (
            f"git-annex not found (set {_ANNEX_ENV_VAR} or install it); "
            f"project left as a plain directory"
        )
        return report

    already = is_annex_repo(repo)
    report["already_annex"] = already
    repo.mkdir(parents=True, exist_ok=True)

    if dry_run:
        vendored = scan_vendored(repo)
        report["result"] = "would-convert" if not already else "would-refresh"
        report["files"] = sum(len(f) for _r, _d, f in os.walk(repo))
        report["vendored"] = [p for p, _u, _s in vendored]
        return report

    if not (repo / ".git").exists():
        r = _git(repo, "init", "-q", "-b", CANONICAL_BRANCH)
        if r.returncode != 0:
            report["result"] = "error"
            report["detail"] = f"git init failed: {_out(r)}"
            return report
        # A repo created before this ran may be on `master`; normalise.
        _git(repo, "symbolic-ref", "HEAD", f"refs/heads/{CANONICAL_BRANCH}")

    if not already:
        r = _annex(repo, "init", description or f"canonical:{project}")
        if r.returncode != 0:
            report["result"] = "error"
            report["detail"] = f"git annex init failed: {_out(r)}"
            return report

    # Exclusion rules go in BEFORE the first add — a secret annexed once is in
    # history forever, and history is what the off-site mirror carries.
    vendored = scan_vendored(repo)
    _write_exclusions(repo, vendored)
    report["vendored"] = len(vendored)
    ensure_canonical_config(project)

    _git(repo, "add", "-f", ".gitignore", ".gitattributes")
    if vendored:
        _git(repo, "add", "-f", _VENDORED_FILE)
    r = _annex(repo, "add", ".", timeout=None)
    if r.returncode != 0:
        report["result"] = "error"
        report["detail"] = f"git annex add failed: {_out(r)}"
        return report
    _git(repo, "add", "-A")

    if _is_clean(repo):
        report["result"] = "up_to_date"
    else:
        msg = "awm: annex-init project data" if not already else "awm: refresh project data"
        c = _git(repo, "commit", "-q", "-m", msg)
        if c.returncode != 0 and not _is_clean(repo):
            report["result"] = "error"
            report["detail"] = f"commit failed: {_out(c)}"
            return report
        report["result"] = "converted" if not already else "refreshed"

    report["branch"] = current_branch(repo)
    report["rev"] = head_rev(repo)
    return report


# ---------------------------------------------------------------------------
# Per-scope clone
# ---------------------------------------------------------------------------

def _configure_clone(clone: Path, project: str, scope: str) -> None:
    # Hardlink content from the same-filesystem canonical store instead of
    # copying it — this is what makes N isolated scopes cost one copy.
    _git(clone, "config", "annex.hardlink", "true")
    # A hardlinked file is NOT an independent copy, so this clone must never be
    # counted as one when git-annex decides whether content is safe to drop.
    _annex(clone, "untrust", "here")
    _git(clone, "config", "merge.git-annex.driver", "git-annex merge-driver %A %O %B")


def _tracked_count(repo: Path) -> int:
    r = _git(repo, "ls-files")
    return len((r.stdout or "").splitlines()) if r.returncode == 0 else 0


def provision_scope_data(project: str, scope: str, awm_dir: Path) -> dict:
    """Materialise ``<awm_dir>/data`` for a scope. The single entry point.

    Returns ``{mode, ...}`` with ``mode`` ∈ ``annex | symlink``. Idempotent:
    safe to call on scope creation *and* from the repair path, which is what
    lets ``scope heal`` migrate the 180 scopes that pre-date this.

    Never destroys an existing directory it does not recognise — a
    ``.awm/data`` that is a real directory but not our clone is left alone and
    reported, rather than silently deleted.
    """
    dest = awm_dir / "data"
    canonical = canonical_repo(project)

    if not is_annex_project(project):
        return _provision_symlink(project, dest, reason="project not annex-backed")
    if not annex_available():
        # Degrade rather than break scope creation. The scope still sees the
        # data (annex symlinks resolve inside the canonical repo); it just
        # doesn't get isolation until git-annex is reachable.
        return _provision_symlink(
            project, dest,
            reason=f"git-annex not found (set {_ANNEX_ENV_VAR})",
        )

    branch = data_branch(scope)
    awm_dir.mkdir(parents=True, exist_ok=True)

    if dest.is_symlink():
        # Legacy shared symlink, or a stale one — replace it with the clone.
        dest.unlink()
    elif dest.exists() and not is_annex_repo(dest):
        if any(dest.iterdir()):
            return {
                "mode": "unknown",
                "path": str(dest),
                "detail": (
                    f"{dest} exists, is not a symlink and is not an annex clone — "
                    f"refusing to touch it. Move it aside and re-run."
                ),
            }
        dest.rmdir()

    if not dest.exists():
        r = _git(canonical, "clone", "--quiet", str(canonical), str(dest))
        if r.returncode != 0:
            log.warning("data clone failed for %s/%s: %s", project, scope, _out(r))
            return _provision_symlink(project, dest, reason=f"clone failed: {_out(r)}")
        ri = _annex(dest, "init", f"{project}/{scope}")
        if ri.returncode != 0:
            log.warning("annex init failed in %s: %s", dest, _out(ri))

    _configure_clone(dest, project, scope)

    # Put the clone on its own data branch: prefer a branch that already exists
    # (a re-created scope keeps its history) over branching from the canonical.
    _git(dest, "fetch", "--quiet", "origin")
    if current_branch(dest) != branch:
        if _git(dest, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode == 0:
            _git(dest, "checkout", "--quiet", branch)
        elif _git(dest, "rev-parse", "--verify", "-q",
                  f"refs/remotes/origin/{branch}").returncode == 0:
            _git(dest, "checkout", "--quiet", "-B", branch, f"origin/{branch}")
        else:
            _git(dest, "checkout", "--quiet", "-B", branch,
                 f"origin/{CANONICAL_BRANCH}")

    report = {
        "mode": "annex",
        "path": str(dest),
        "branch": current_branch(dest),
        "rev": head_rev(dest),
        "origin": str(canonical),
    }

    # Hardlink the content in so the directory looks exactly like today's
    # shared one. Costs inodes, not bytes.
    n = _tracked_count(dest)
    report["tracked_files"] = n
    if n <= _AUTOGET_MAX:
        g = _annex(dest, "get", ".")
        report["content"] = "fetched" if g.returncode == 0 else "partial"
        if g.returncode != 0:
            report["content_detail"] = _out(g)[-400:]
    else:
        report["content"] = "lazy"
        report["content_detail"] = (
            f"{n} tracked files exceeds AWM_DATA_ANNEX_AUTOGET_MAX={_AUTOGET_MAX}; "
            f"run `git annex get <path>` for what you need"
        )
    return report


def _provision_symlink(project: str, dest: Path, *, reason: str) -> dict:
    """The pre-annex behaviour, verbatim: one shared directory, symlinked in."""
    target = canonical_repo(project)
    if dest.is_symlink() or dest.exists():
        if dest.is_dir() and not dest.is_symlink():
            return {
                "mode": "unknown", "path": str(dest),
                "detail": f"{dest} is a real directory; refusing to replace it with a symlink",
            }
        dest.unlink()
    target.mkdir(parents=True, exist_ok=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.symlink_to(target)
    return {"mode": "symlink", "path": str(dest), "target": str(target), "detail": reason}


def scope_clone(project: str, scope: str, worktree: Path) -> Path | None:
    """The scope's data clone, or None when it isn't one (symlink/absent)."""
    dest = worktree / ".awm" / "data"
    if dest.is_symlink() or not dest.exists():
        return None
    return dest if is_annex_repo(dest) else None


# ---------------------------------------------------------------------------
# Snapshot / publish
# ---------------------------------------------------------------------------

def snapshot_data(clone: Path, message: str) -> dict:
    """Commit whatever the scope has written. The seamlessness bridge.

    Agents write files with ordinary tools; ``git annex add`` then routes each
    one by the ``annex.largefiles`` rule — bulk into the content store, small
    text into ordinary git — and ``git add -A`` picks up deletions.
    """
    if _is_clean(clone):
        return {"result": "up_to_date", "rev": head_rev(clone)}
    a = _annex(clone, "add", ".")
    if a.returncode != 0:
        return {"result": "error", "detail": f"annex add failed: {_out(a)}"}
    _git(clone, "add", "-A")
    if _is_clean(clone):
        return {"result": "up_to_date", "rev": head_rev(clone)}
    c = _git(clone, "commit", "-q", "-m", message)
    if c.returncode != 0 and not _is_clean(clone):
        return {"result": "error", "detail": f"commit failed: {_out(c)}"}
    return {"result": "committed", "rev": head_rev(clone)}


def publish_data(clone: Path, *, content: bool = True) -> dict:
    """Push this clone's branch, its **location log**, and its content to origin.

    The location log is the subtle part. It rides the ``git-annex`` branch, not
    the data branch, so ``git push origin <branch>`` alone leaves every peer
    reporting *0 copies* for content that is physically present on the remote —
    and ``git annex get`` then refuses to fetch it. The branch must be merged
    (union merge, always resolvable) and pushed explicitly.

    ``git annex sync`` would also do this, but sync is the machinery that
    auto-resolves conflicting content into ``file.variant-<key>`` files. We
    never want that to happen implicitly, so this does the three steps by hand.
    """
    branch = current_branch(clone)
    if not branch:
        return {"result": "error", "detail": "clone has no branch checked out"}

    if content:
        cp = _annex(clone, "copy", "--to", "origin", ".")
        if cp.returncode != 0:
            return {"result": "error", "detail": f"annex copy --to origin failed: {_out(cp)}"}

    _git(clone, "fetch", "--quiet", "origin")
    _annex(clone, "merge")  # union-merges origin/git-annex only; no synced/ branches exist

    p = _git(clone, "push", "--quiet", "origin",
             f"{branch}:{branch}", "git-annex:git-annex")
    if p.returncode != 0:
        return {"result": "error", "detail": f"push failed: {_out(p)}"}
    return {"result": "published", "branch": branch, "rev": head_rev(clone)}


# ---------------------------------------------------------------------------
# Transactional merge
# ---------------------------------------------------------------------------

def _annexed_missing_at_origin(clone: Path, rev: str) -> list[str]:
    """Annexed files in ``rev`` whose content no other repo holds.

    Advancing a branch onto content nobody has is the "which version is
    current?" failure in a new costume: the tree says the file exists, every
    read of it fails. Refuse the merge instead.
    """
    r = _annex(clone, "find", "--branch", rev, "--not", "--in", "origin")
    if r.returncode != 0:
        return []  # old annex or no such branch — preflight is best-effort
    return [ln for ln in (r.stdout or "").splitlines() if ln.strip()]


def merge_data(clone: Path, source_ref: str, message: str, *,
               label: str = "", preflight: bool = True, fetch: bool = True) -> dict:
    """All-or-nothing merge of ``source_ref`` into the branch at ``clone``.

    Mirrors the code-side ``_merge_one`` contract — ``result`` ∈
    ``merged | up_to_date | conflict | blocked | error`` — with three
    guarantees:

    1. **Preflight.** Content referenced by the incoming revision must be
       reachable, else the merge is refused whole (``blocked``).
    2. **Rollback.** Any non-success restores the exact pre-merge revision and
       leaves the working tree clean.
    3. **The location log is deliberately NOT rolled back.** It is an
       append-only record of where bytes live; merging it can only *add*
       knowledge, and reverting it is itself the data-loss operation — it
       produces exactly the "0 copies" symptom while the bytes sit on disk.
       The transaction boundary is the data branch ref, nothing else.

    Uses a **plain ``git merge``** on purpose. git-annex only auto-resolves
    conflicting content into ``file.variant-<key>`` when *its own* merge
    machinery runs; under plain merge a conflicting annexed file is just a
    symlink whose target differs, so git reports an ordinary unmerged path.
    Keeping both sides stays an explicit human decision instead of a silent
    default.

    On success the merged-in content is hardlinked from the canonical store
    (``fetch``). Skipping that would leave the incoming files as *broken*
    symlinks, which is precisely the "is this missing or just not fetched?"
    ambiguity this layer exists to remove — and it costs inodes, not bytes.
    """
    base = {"scope": label or clone.name, "branch": current_branch(clone)}

    if not _is_clean(clone):
        return {**base, "result": "error",
                "detail": "data clone has uncommitted changes — snapshot or discard first"}

    pre = head_rev(clone)

    if preflight:
        missing = _annexed_missing_at_origin(clone, source_ref)
        if missing:
            sample = ", ".join(missing[:3])
            return {**base, "result": "blocked",
                    "detail": (f"{len(missing)} annexed file(s) in {source_ref} have no "
                               f"reachable copy (e.g. {sample}) — refusing to merge")}

    r = _git(clone, "merge", source_ref, "-m", message)
    out = _out(r)
    if r.returncode == 0:
        if "Already up to date" in out:
            return {**base, "result": "up_to_date", "detail": "already up to date"}
        res = {**base, "result": "merged", "rev": head_rev(clone),
               "detail": out.splitlines()[-1] if out else "merged"}
        if fetch and _tracked_count(clone) <= _AUTOGET_MAX:
            g = _annex(clone, "get", ".")
            res["content"] = "fetched" if g.returncode == 0 else "partial"
        elif fetch:
            res["content"] = "lazy"
        return res

    conflicted = _git(clone, "rev-parse", "--verify", "-q", "MERGE_HEAD").returncode == 0
    if conflicted:
        _git(clone, "merge", "--abort")
    if head_rev(clone) != pre:
        _git(clone, "reset", "--hard", pre)
    _git(clone, "checkout", "--", ".")
    if conflicted:
        return {**base, "result": "conflict",
                "detail": "merge conflict — rolled back, data clone left clean"}
    return {**base, "result": "error", "detail": out or "merge failed"}


def promote_data(project: str, scope: str, worktree: Path, *,
                 message: str | None = None) -> dict:
    """Publish a scope's data and fast-forward the canonical branch onto it.

    The concurrency story, in order:

    * an advisory per-project lock serialises promotion;
    * the scope first merges the canonical branch into its own (transactionally
      — a conflict here aborts and changes nothing);
    * the push into ``main`` is therefore a fast-forward, and is rejected
      outright if another scope moved ``main`` in between.

    So two scopes promoting at once produce one winner and one clean
    rejection. The naked-overwrite path that destroyed the null-model cache
    does not exist in this mechanism at all.
    """
    clone = scope_clone(project, scope, worktree)
    if clone is None:
        return {"result": "skipped", "detail": f"{worktree}/.awm/data is not an annex clone"}
    if not annex_available():
        return {"result": "unavailable", "detail": "git-annex not found"}

    snap = snapshot_data(clone, message or f"awm: {project}/{scope} data snapshot")
    if snap["result"] == "error":
        return {"result": "error", "detail": snap["detail"]}

    with _promote_lock(project):
        ensure_canonical_config(project)
        pub = publish_data(clone, content=True)
        if pub["result"] != "published":
            return {"result": "error", "detail": pub["detail"], "stage": "publish"}

        _git(clone, "fetch", "--quiet", "origin")
        m = merge_data(
            clone, f"origin/{CANONICAL_BRANCH}",
            f"awm: bring {CANONICAL_BRANCH} into {data_branch(scope)} before promote",
            label=scope,
        )
        if m["result"] in ("conflict", "blocked", "error"):
            return {"result": m["result"], "detail": m["detail"], "stage": "reconcile"}

        branch = current_branch(clone)
        # Fast-forward only. A rejection here means another scope promoted
        # first — the correct outcome, not an error to paper over.
        p = _git(clone, "push", "--quiet", "origin", f"{branch}:{CANONICAL_BRANCH}",
                 "git-annex:git-annex")
        if p.returncode != 0:
            return {"result": "rejected", "stage": "push",
                    "detail": (f"canonical {CANONICAL_BRANCH} moved or its working tree is "
                               f"dirty — nothing was changed: {_out(p)[-400:]}")}

    return {"result": "promoted", "project": project, "scope": scope,
            "branch": branch, "rev": head_rev(clone),
            "snapshot": snap["result"]}


# ---------------------------------------------------------------------------
# Fan-in / fan-out
# ---------------------------------------------------------------------------

def gather_data(project: str, hub: str, hub_worktree: Path,
                peripherals: list[tuple[str, Path]]) -> list[dict]:
    """Merge each peripheral's data branch into the hub's data clone.

    Unlike the code side this **cannot** stay local-only: peripheral clones
    have their own object stores, so their content must reach the canonical
    repo before the hub can resolve it.
    """
    hub_clone = scope_clone(project, hub, hub_worktree)
    if hub_clone is None:
        return [{"scope": hub, "result": "skipped",
                 "detail": "hub has no annex data clone"}]

    results: list[dict] = []
    for name, wt in peripherals:
        clone = scope_clone(project, name, wt)
        if clone is None:
            results.append({"scope": name, "result": "skipped",
                            "detail": "no annex data clone"})
            continue
        snap = snapshot_data(clone, f"awm: {project}/{name} data snapshot (gather)")
        if snap["result"] == "error":
            results.append({"scope": name, "result": "error", "detail": snap["detail"]})
            continue
        pub = publish_data(clone, content=True)
        if pub["result"] != "published":
            results.append({"scope": name, "result": "error",
                            "detail": f"publish: {pub['detail']}"})
            continue
        _git(hub_clone, "fetch", "--quiet", "origin")
        _annex(hub_clone, "merge")
        results.append(merge_data(
            hub_clone, f"origin/{data_branch(name)}",
            f"awm: gather data {data_branch(name)} into {current_branch(hub_clone)}",
            label=name,
        ))
    return results


def scatter_data(project: str, hub: str, hub_worktree: Path,
                 peripherals: list[tuple[str, Path]]) -> list[dict]:
    """Merge the hub's data branch into each peripheral's data clone."""
    hub_clone = scope_clone(project, hub, hub_worktree)
    if hub_clone is None:
        return [{"scope": hub, "result": "skipped",
                 "detail": "hub has no annex data clone"}]

    snap = snapshot_data(hub_clone, f"awm: {project}/{hub} data snapshot (scatter)")
    if snap["result"] == "error":
        return [{"scope": hub, "result": "error", "detail": snap["detail"]}]
    pub = publish_data(hub_clone, content=True)
    if pub["result"] != "published":
        return [{"scope": hub, "result": "error", "detail": f"publish: {pub['detail']}"}]

    hub_branch = current_branch(hub_clone)
    results: list[dict] = []
    for name, wt in peripherals:
        clone = scope_clone(project, name, wt)
        if clone is None:
            results.append({"scope": name, "result": "skipped",
                            "detail": "no annex data clone"})
            continue
        if not _is_clean(clone):
            results.append({"scope": name, "result": "skipped",
                            "detail": "data clone dirty — skipped"})
            continue
        _git(clone, "fetch", "--quiet", "origin")
        _annex(clone, "merge")
        results.append(merge_data(
            clone, f"origin/{hub_branch}",
            f"awm: scatter data {hub_branch} into {current_branch(clone)}",
            label=name,
        ))
    return results


# ---------------------------------------------------------------------------
# Teardown safety
# ---------------------------------------------------------------------------

def prepare_teardown(worktree: Path, *, project: str | None = None,
                     scope: str | None = None, force: bool = False) -> dict:
    """Make a scope's data clone safe to delete — or refuse.

    Worktree removal is the irreversible direction. Against an annex clone the
    old blind ``rmtree`` would fail on read-only object files, leave a partial
    directory whose name then collides with the next worktree of the same name,
    and destroy any content that existed **only** in that scope.

    So: publish everything to the canonical repo first, then refuse if anything
    would still be lost. ``force`` overrides the refusal (and only that).
    Always chmods the tree writable so the caller's delete can complete.
    """
    dest = worktree / ".awm" / "data"
    if dest.is_symlink() or not dest.exists():
        return {"result": "n/a", "detail": "no data clone"}
    if not is_annex_repo(dest):
        return {"result": "n/a", "detail": "data dir is not an annex clone"}

    report: dict = {"result": "ok", "path": str(dest)}

    if annex_available():
        snap = snapshot_data(dest, f"awm: {project or '?'}/{scope or dest.parent.parent.name} "
                                   f"data snapshot before teardown")
        report["snapshot"] = snap["result"]
        pub = publish_data(dest, content=True)
        report["publish"] = pub["result"]
        if pub["result"] != "published":
            report["publish_detail"] = pub.get("detail", "")

        # Anything still held only here is about to be destroyed.
        r = _annex(dest, "find", "--not", "--in", "origin")
        orphans = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
        if orphans:
            report["orphans"] = len(orphans)
            report["orphan_sample"] = orphans[:5]
            if not force:
                report["result"] = "refused"
                report["detail"] = (
                    f"{len(orphans)} annexed file(s) exist only in this scope and could "
                    f"not be published (e.g. {', '.join(orphans[:3])}). Refusing to "
                    f"delete. Fix the publish error or pass force=true."
                )
                return report
            report["detail"] = f"forced: {len(orphans)} unpublished file(s) will be lost"
    else:
        report["result"] = "unchecked"
        report["detail"] = "git-annex not found — could not verify content is published"
        if not force:
            report["result"] = "refused"
            return report

    chmod_writable(dest)
    return report


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def data_status(project: str, scope: str, worktree: Path) -> dict:
    """Describe a scope's data view — mode, branch, revision, drift, dirt."""
    dest = worktree / ".awm" / "data"
    if dest.is_symlink():
        return {"project": project, "scope": scope, "mode": "symlink",
                "target": os.readlink(str(dest))}
    if not dest.exists():
        return {"project": project, "scope": scope, "mode": "missing"}
    if not is_annex_repo(dest):
        return {"project": project, "scope": scope, "mode": "plain-dir",
                "path": str(dest)}

    branch = current_branch(dest)
    dirty = not _is_clean(dest)
    _git(dest, "fetch", "--quiet", "origin")
    ahead = behind = None
    cnt = _git(dest, "rev-list", "--left-right", "--count",
               f"{branch}...origin/{CANONICAL_BRANCH}")
    if cnt.returncode == 0 and cnt.stdout.strip():
        parts = cnt.stdout.split()
        if len(parts) == 2:
            ahead, behind = int(parts[0]), int(parts[1])
    present = _annex(dest, "find", "--in", "here")
    return {
        "project": project, "scope": scope, "mode": "annex",
        "path": str(dest), "branch": branch, "rev": head_rev(dest),
        "dirty": dirty,
        "ahead_of_canonical": ahead, "behind_canonical": behind,
        "content_present": len((present.stdout or "").splitlines()),
        "annex_bin": annex_bin(),
    }
