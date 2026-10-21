"""DVC data layer — a commit records code and the data it was built against.

Code gets its concurrency model from git worktrees. Giving data a parallel one —
its own history, its own promote — means two levers, and two levers drift: a
scope created from a parent branch would get the parent's *code* and the
project's *canonical* data, because scope creation threads a base branch into the
code worktree and nothing analogous exists on the data side.

The deeper cost is not the drift but the workaround it forces. With no
versioning a superseded dataset cannot safely be deleted, so every variant stays
live side by side — tier 3 beside tier 4, `set2` beside `set4`, three null tables
in two days. The recurring "which one is current?" failure is a *symptom of not
being able to delete*. Versioning makes deletion safe, and deletion makes the
question unaskable.

DVC removes the second lever. The **pin** — a ~110-byte text file recording a
content hash — is tracked in the code repository beside the code, and the bulk
lives in a content-addressed cache shared by every scope on the machine. So::

    projects/<project>/<scope>/data/<chunk>        the files (links into the cache)
    projects/<project>/<scope>/data/<chunk>.dvc    the pin -- TRACKED, ~110 bytes
    <workspace>/data/.dvc_cache/                   the bytes, once, for everyone

There is no data branch, no data history, and no promote verb, because a code
commit *is* the data commit and a code merge *is* the promote. That is the whole
design: one lever instead of two.

Three consequences worth internalising before changing anything here:

* **Data is a first-class repo folder, not scope metadata.** It lives at
  ``<repo>/data/`` and is tracked, which is why ``.awm/`` is wholly gitignored:
  a pin has to be *visible* to be committed alongside the code it belongs to.
  ``.awm/data`` survives only as a compatibility symlink to ``../data``, for the
  125+ call sites that still name that path.

* **Chunks are declared anywhere.** ``dvc add`` works on any path and drops the
  pin beside its target, so there is no single mount point to defend. Chunk
  granularity is a real decision: a repeat ``dvc add`` walks the whole chunk, so
  a hot chunk should be small (thousands of files, not a hundred thousand) while
  cold bulk belongs in its own chunk that is pinned and backed up but never
  materialised.

* **Nothing here can lose data, and one thing outside here can.** Every verb in
  this module is idempotent and additive. ``dvc gc`` is the exception: the cache
  is shared across *all* projects, so a ``--workspace`` collection run from one
  worktree deletes objects another worktree's checkout depends on. Already-
  materialised files survive (the workspace link keeps the inode alive), so it
  fails silently at the next fresh checkout rather than loudly at the time. This
  module therefore never exposes a bare ``gc``.

**Degrades, never fails.** dvc is not on the daemon's PATH (it lives in its own
mamba env), so every entry point resolves the binary first and falls back to the
legacy shared symlink rather than breaking scope creation.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from awm import config as _config
from awm.dvc.backup import has_cache_tag
from awm.gatewayclient import cancel

log = logging.getLogger("awm.dvc.data_dvc")

# The data folder inside a project repo. Tracked, first-class, same name in
# every project — scripts hardcode `<repo>/data/...` and that is the point.
DATA_SUBDIR = "data"

# One cache for the whole workspace. Content-addressed, so N scopes holding the
# same chunk cost one physical copy (hardlinked), and two projects that happen
# to hold the same file store it once.
CACHE_DIRNAME = ".dvc_cache"

# Default exclusions, written once at conversion. The structural entries earn
# their place -- `**/.git/` stops a vendored checkout becoming thousands of
# objects, `.awm/` keeps scope metadata out of the cache. Everything reaching
# the cache is in the sync, and the sync is append-only, so an object added
# once cannot be unadded.
_DVCIGNORE = """\
# AWM-managed defaults. Paths here are never hashed into the cache, and an
# exclusion has to be in place BEFORE the first `dvc add` -- content added once
# is in the shared cache, and the cache is what the nightly sync carries.
#
# The secrets block is a convenience, not a control: a scope that chooses to
# track a secret gets it cached and synced like anything else. What a scope
# tracks is the scope's decision.

# --- secrets ---------------------------------------------------------------
secrets/
**/secrets/
.env
.env.*
**/.env
**/.env.*
**/.credentials.json
**/.nextflow/secrets/

# --- nested third-party checkouts ------------------------------------------
# You never hash a tree of git repos as data: the inner .git becomes thousands
# of cache objects and the checkout stops being a checkout. Pin it by URL and
# commit instead (VENDORED.tsv), and re-clone on demand.
**/.git/

# --- awm's own metadata ----------------------------------------------------
.awm/
**/.awm/

# --- machine-local noise ---------------------------------------------------
.DS_Store
__pycache__/
*.pyc
"""

# The merge driver is what makes a code merge carry the data. Without it a `.dvc`
# pin is an ordinary text file and two branches touching one chunk conflict on a
# hash line; with it, DVC combines the two directory listings.
_MERGE_DRIVER_NAME = "dvc"
_GIT_ATTRIBUTES_LINE = "*.dvc merge=dvc"

# Per-scope, gitignored, one chunk path per line. Mounting is a *local* choice
# ("which of the chunks my commit pins do I want on disk?"), not a property of
# the commit, which is exactly why it lives under `.awm/` and does not travel.
MOUNTS_FILE = "data-mounts"

# Written by the hook when a checkout could not be satisfied. See _CHECKOUT_SH.
CHECKOUT_FAILED_FILE = "data-checkout-failed"

# The shared body both hooks run.
#
# Three things here are load-bearing and each cost a real bug to learn:
#
# 1. **The absolute dvc path.** Hooks inherit the environment of whatever drove
#    the merge, which for gather/scatter is the daemon's minimal systemd PATH.
#    A bare `dvc` finds nothing there.
# 2. **The mount list.** A bare `dvc checkout` materialises EVERY pin in the
#    repo -- DVC has no "pinned but not materialised" flag -- so on scadc the
#    first merge in any scope would drag in ~65 GB of cold chunks and ~122k
#    inodes. Checking out an explicit target list is what keeps a cold chunk
#    pinned and backed up without ever landing on disk.
# 3. **The failure sentinel.** git ignores a hook's exit status, and a failing
#    `dvc checkout` REMOVES the files it is replacing before it discovers it
#    cannot install the new ones. So a merge onto a pin whose content was
#    collected leaves the scope with neither version, exit 0, and the error
#    buried in merge output. The hook cannot fail the merge, so instead it
#    leaves a sentinel that `data_status` and provisioning both surface.
_CHECKOUT_SH = """\
DVC={dvc_bin}
SENTINEL=".awm/{failed_file}"
MOUNTS=".awm/{mounts_file}"
SHARED="data/{shared_file}"
# Hooks live in the COMMON git dir, so they fire in every worktree of the
# project -- including the ones whose branch predates the conversion and has no
# `.dvc/` at all. There, `dvc checkout` exits 253 ("not inside of a DVC
# repository") and we would cry wolf on every merge. Not converted, nothing to
# do.
[ -d .dvc ] || exit 0
[ "${{LINKS_ONLY:-0}}" = 1 ] || rm -f "$SENTINEL"
targets=()
have_list=0
if [ -f "$MOUNTS" ]; then
  have_list=1
  while IFS= read -r line; do
    case "$line" in ''|'#'*) continue ;; esac
    targets+=("$line")
  done < "$MOUNTS"
fi
# An ABSENT list means "materialise everything"; a list that exists but selects
# nothing means "materialise nothing". Collapsing those two would turn an
# opted-out scope into one that checks out every cold chunk in the project.
if [ "$have_list" = 1 ] && [ "${{#targets[@]}}" -eq 0 ]; then exit 0; fi
# A shared chunk is a symlink into the project's store, repointed here to the
# copy for its new md5. Building a missing copy is provisioning's job. A real
# tree at a shared path is a private copy and checks out like any chunk.
missing=()
common=$(cd "$(git rev-parse --git-common-dir)" && pwd)
if [ -f "$SHARED" ] && [ "${{common##*/}}" = .bare ]; then
  store="${{common%/*}}/data"
  if [ "$have_list" = 0 ]; then
    have_list=1
    while IFS= read -r pin; do targets+=("${{pin%.dvc}}"); done < <(git ls-files '*.dvc')
  fi
  while IFS= read -r c; do
    c="${{c#data/}}"; c="${{c%/}}"
    case "$c" in ''|'#'*) continue ;; esac
    [ -f "data/$c.dvc" ] || continue
    if [ -d "data/$c" ] && [ ! -L "data/$c" ]; then continue; fi
    keep=(); inview=0
    for t in "${{targets[@]}}"; do
      if [ "${{t%/}}" = "data/$c" ]; then inview=1; else keep+=("$t"); fi
    done
    targets=("${{keep[@]}}")
    [ "$inview" = 1 ] || continue
    md5=$(grep -m1 -oE 'md5: [0-9a-f]{{32}}' "data/$c.dvc" | cut -c6-)
    if [ -n "$md5" ] && [ -d "$store/$c/$md5" ]; then
      dir=$(dirname "data/$c")
      mkdir -p "$dir"
      ln -sfn "$(realpath -ms --relative-to="$PWD/$dir" "$store/$c/$md5")" "data/$c"
    else
      # Never leave a link to another md5: a bare `dvc checkout` would write
      # this pin's content through it, into a copy other scopes read.
      [ -L "data/$c" ] && rm -f "data/$c"
      missing+=("data/$c@$md5")
    fi
  done < "$SHARED"
fi
out=""
failed=0
if [ "${{LINKS_ONLY:-0}}" = 1 ]; then
  :
elif [ "$have_list" = 0 ] || [ "${{#targets[@]}}" -gt 0 ]; then
  # NOT --quiet: the sentinel exists to say WHY the checkout failed, and --quiet
  # silences the very message it is supposed to record, leaving an empty file.
  out=$("$DVC" checkout "${{targets[@]}}" 2>&1) || failed=1
fi
if [ "${{#missing[@]}}" -gt 0 ]; then
  failed=1
  out="$out
no shared copy on this node for: ${{missing[*]}}. Run 'scope heal' to build it."
fi
if [ "$failed" = 1 ]; then
  mkdir -p .awm
  {{ echo "dvc checkout FAILED after $1"; echo "$out"; }} >> "$SENTINEL"
  echo "!! awm: dvc checkout failed -- your data does NOT match this commit." >&2
  echo "!! $out" >&2
  echo "!! recorded in $SENTINEL; run 'dvc checkout' after restoring the cache." >&2
fi
"""

# DVC ships pre-commit / post-checkout / pre-push hooks but **no post-merge**,
# and post-merge is the one that cannot be skipped: without it a merge advances
# the pin over a stale workspace -- the two-lever failure this layer exists to
# remove. (`dvc install` would not help even if it shipped one: it builds its
# hooks path as `<root>/.git/hooks`, and in a secondary worktree `.git` is a
# file, so it dies with `Not a directory`. Every awm scope is a secondary
# worktree, so these are written by hand.)
_POST_MERGE_HOOK = """\
#!/usr/bin/env bash
# AWM-managed. Materialise the data this commit pins, after a merge moved it.
""" + _CHECKOUT_SH.replace('$1', 'merge')

# A *conflicted* merge fires no post-merge hook at all -- you resolve, then
# `git commit`, and git treats that as an ordinary commit. That is precisely the
# case where a human just hand-edited a `.dvc` pin, so it is the last place the
# workspace should be left stale. post-commit closes it, no-oping on anything
# that is not a merge commit.
_POST_COMMIT_HOOK = """\
#!/usr/bin/env bash
# AWM-managed. A conflicted merge is resolved with `git commit`, which fires no
# post-merge hook -- so catch merge commits here. Non-merge commits no-op.
[ "$(git rev-list --parents -n 1 HEAD | wc -w)" -ge 3 ] || exit 0
""" + _CHECKOUT_SH.replace('$1', 'a conflicted merge')

# A branch switch moves pins with no merge, so it repoints shared links too. It
# runs no `dvc checkout`: switching branches has never moved private data.
_POST_CHECKOUT_HOOK = """\
#!/usr/bin/env bash
# AWM-managed. Repoint shared-chunk links after a branch switch.
[ "$3" = 1 ] || exit 0
LINKS_ONLY=1
""" + _CHECKOUT_SH.replace('$1', 'a branch switch')

_AWM_HOOK_MARKER = "# AWM-managed."


# ---------------------------------------------------------------------------
# Binary resolution
# ---------------------------------------------------------------------------

_DVC_ENV_VAR = "AWM_DVC_BIN"

_DVC_FALLBACKS = (
    Path.home() / "lib/miniforge3/envs/dvc/bin/dvc",
    Path.home() / "lib/miniforge3/envs/awm/bin/dvc",
    Path("/usr/local/bin/dvc"),
    Path("/usr/bin/dvc"),
)


@lru_cache(maxsize=1)
def dvc_bin() -> str | None:
    """Absolute path to ``dvc``, or None when it isn't installed.

    The scopes service runs under systemd with a minimal PATH and dvc lives in
    its own mamba env, so ``shutil.which`` alone finds nothing. Order: explicit
    override, PATH, then the known env locations.
    """
    override = os.environ.get(_DVC_ENV_VAR)
    if override and Path(override).is_file() and os.access(override, os.X_OK):
        return override
    found = shutil.which("dvc")
    if found:
        return found
    for cand in _DVC_FALLBACKS:
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def dvc_available() -> bool:
    return dvc_bin() is not None


def enabled() -> bool:
    """Global kill switch. ``AWM_DATA_DVC=0`` reverts every project to symlinks."""
    return os.environ.get("AWM_DATA_DVC", "1").lower() not in ("0", "off", "false", "no")


def _env() -> dict:
    """Environment with dvc's directory on PATH.

    The post-merge hook calls a bare ``dvc``, and git runs hooks with the
    environment of whatever invoked the merge — often the daemon's minimal PATH.
    Putting dvc's directory on PATH here covers every git call this module makes;
    the hook itself carries an absolute fallback for the calls it does not.
    """
    env = dict(os.environ)
    binp = dvc_bin()
    if binp:
        env["PATH"] = str(Path(binp).parent) + os.pathsep + env.get("PATH", "")
    # A daemon has no global git identity; without one every commit fails.
    env.setdefault("GIT_AUTHOR_NAME", "awm")
    env.setdefault("GIT_AUTHOR_EMAIL", "awm@localhost")
    env.setdefault("GIT_COMMITTER_NAME", "awm")
    env.setdefault("GIT_COMMITTER_EMAIL", "awm@localhost")
    return env


def _git(repo: Path, *args: str, timeout: int | None = None) -> subprocess.CompletedProcess:
    return cancel.run(["git", "-C", str(repo), *args], env=_env(), timeout=timeout)


def _dvc(repo: Path, *args: str, timeout: int | None = None) -> subprocess.CompletedProcess:
    binp = dvc_bin()
    if not binp:
        return subprocess.CompletedProcess([], 127, "", "dvc not found")
    return cancel.run([binp, *args], cwd=str(repo), env=_env(), timeout=timeout)


def _out(r: subprocess.CompletedProcess) -> str:
    return ((r.stdout or "") + (r.stderr or "")).strip()


# ---------------------------------------------------------------------------
# Paths + predicates
# ---------------------------------------------------------------------------

def workspace_root() -> Path:
    """The workspace root.

    Read through the ``config`` module rather than binding at import: tests
    redirect the workspace by monkeypatching the attribute, and a module-level
    copy would silently keep pointing at the real one.
    """
    return _config.DATA_DIR.parent


def cache_dir(repo_dir: Path | None = None) -> Path:
    """The content-addressed cache a worktree's objects live in.

    Every project shares one, except a no-remote project, which keeps its own
    beside its worktrees so that the cache sync never carries its bytes.
    """
    if repo_dir is not None and no_remote(repo_dir):
        return project_dir(repo_dir) / CACHE_DIRNAME
    return _config.DATA_DIR / CACHE_DIRNAME


def project_dir(repo_dir: Path) -> Path | None:
    """``projects/<p>/`` for a scope worktree: the directory holding its common git dir."""
    common = _common_git_dir(repo_dir)
    return common.parent if common is not None else None


def no_remote(repo_dir: Path) -> bool:
    """Whether this worktree's project opted out of every remote.

    A ``CACHEDIR.TAG`` at ``projects/<p>/`` is the one declaration: the workspace
    mirror skips the tagged tree, the project's DVC cache lives inside it, and
    push and pull refuse it.
    """
    project = project_dir(repo_dir)
    return project is not None and has_cache_tag(project)


TMPFS = Path("/dev/shm")


def site_cache_dir() -> Path | None:
    """Where DVC keeps its per-worktree index, or ``None`` for DVC's default.

    DVC rebuilds that SQLite index for every new worktree and commits it row by
    row. On a busy disk each commit waits on a journal flush, which made one
    68-pin checkout take 400 s against 2 s on tmpfs. The index is rebuildable,
    so losing it on reboot costs one rebuild.
    """
    if not TMPFS.is_dir() or not os.access(TMPFS, os.W_OK):
        return None
    return TMPFS / f"awm-dvc-{os.getuid()}"


def legacy_data_dir(project: str) -> Path:
    """``<workspace>/data/<project>`` — the pre-DVC shared directory.

    Still the symlink target for any project that has not been converted, and
    still the thing ~949 absolute paths point at, so it does not go away.
    """
    return _config.DATA_DIR / project


def data_dir(repo_dir: Path) -> Path:
    """The tracked data folder inside a project worktree."""
    return repo_dir / DATA_SUBDIR


def is_dvc_repo(repo_dir: Path) -> bool:
    """True iff this worktree's checkout carries a tracked ``.dvc/config``.

    **The opt-in is the checkout itself.** ``.dvc/config`` is tracked, so it
    travels with the branch — which means "is this project on DVC?" is answered
    by the commit, not by a config table that has to be kept in sync, and a
    branch predating the conversion correctly reports False.

    Deliberately ``config``, not the ``.dvc/`` directory. ``.dvc/tmp`` and
    ``.dvc/cache`` are gitignored, so checking out a pre-conversion branch strips
    the tracked files but leaves the directory standing — and a bare ``is_dir``
    would then report a converted project on a branch that has no pins at all.
    """
    return (repo_dir / ".dvc" / "config").is_file()


def is_dvc_project(repo_dir: Path) -> bool:
    return enabled() and dvc_available() and is_dvc_repo(repo_dir)


def chunk_pins(repo_dir: Path) -> list[str]:
    """Every ``.dvc`` pin tracked in this worktree, repo-relative.

    Read from git rather than the filesystem: an untracked ``.dvc`` file beside
    a chunk is somebody's work in progress, not part of what this commit pins.
    """
    r = _git(repo_dir, "ls-files", "*.dvc")
    if r.returncode != 0:
        return []
    return sorted(ln for ln in (r.stdout or "").splitlines() if ln.strip())


# ---------------------------------------------------------------------------
# Shared chunks
# ---------------------------------------------------------------------------

# A big reference chunk costs one hardlink per file in every scope that checks
# it out, about 200 files/s. A chunk listed in this tracked file is instead
# materialised once per node per pin md5, under the project directory, and each
# scope holds one symlink to it. The pin itself stays in the commit unchanged,
# so the commit still names its data exactly.
SHARED_DECLARATION = ".awm-shared"
SHARED_STORE_DIRNAME = DATA_SUBDIR


def shared_store_root(repo_dir: Path) -> Path | None:
    """``projects/<project>/data``, or None when the repo is not an awm bare-repo layout."""
    common = _common_git_dir(repo_dir)
    if common is None or common.name != ".bare":
        return None
    return common.parent / SHARED_STORE_DIRNAME


def pin_md5(pin: Path) -> str | None:
    try:
        outs = (yaml.safe_load(pin.read_text()) or {}).get("outs") or []
    except (OSError, yaml.YAMLError):
        return None
    md5 = outs[0].get("md5") if len(outs) == 1 else None
    return md5.removesuffix(".dir") if isinstance(md5, str) and md5 else None


def shared_chunks(repo_dir: Path) -> dict[str, str]:
    """The chunks this checkout declares shared, as ``{data/<chunk>: md5}``.

    Entries are relative to ``data/``. An entry with no tracked pin, or a pin
    with no single hashed output, is skipped: it has nothing to share.
    """
    decl = data_dir(repo_dir) / SHARED_DECLARATION
    if not decl.is_file():
        return {}
    tracked = set(chunk_pins(repo_dir))
    out: dict[str, str] = {}
    for line in decl.read_text().splitlines():
        rel = line.strip().removeprefix(f"{DATA_SUBDIR}/").strip("/")
        if not rel or rel.startswith("#") or ".." in rel.split("/"):
            continue
        target = f"{DATA_SUBDIR}/{rel}"
        if f"{target}.dvc" not in tracked:
            continue
        md5 = pin_md5(repo_dir / f"{target}.dvc")
        if md5:
            out[target] = md5
    return out


def _shared_copy(store_root: Path, target: str, md5: str) -> Path:
    return store_root / target.removeprefix(f"{DATA_SUBDIR}/") / md5


def _relative_link_target(link: Path, dest: Path) -> str:
    return os.path.relpath(dest, link.parent)


def _links_to(link: Path, copy: Path) -> bool:
    return copy.is_dir() and link.resolve() == copy.resolve()


def _remove_tree(path: Path) -> None:
    """Unlink a materialised tree. Never chmods a file: see ``chmod_dirs_writable``."""
    chmod_dirs_writable(path)
    shutil.rmtree(path)


def _seal_dirs(copy: Path) -> None:
    """Make a shared copy's directories read-only. Files are cache inodes and stay untouched.

    A scope whose pin has moved off its link's md5 would otherwise let a bare
    ``dvc checkout`` rewrite the shared copy, through the link, for every scope
    on that md5. Sealed, the checkout fails instead.
    """
    for root, dirs, _files in os.walk(copy):
        for name in dirs:
            p = Path(root) / name
            p.chmod(p.stat().st_mode & ~0o222)
    copy.chmod(copy.stat().st_mode & ~0o222)


def _reprotect_files(copy: Path) -> None:
    """Clear write bits a failed ``dvc checkout`` set on a shared copy's files.

    DVC answers a refused unlink by making the file writable and retrying, and
    that file is a cache object. Removing write bits is always safe.
    """
    for root, _dirs, files in os.walk(copy):
        for name in files:
            p = Path(root) / name
            mode = p.lstat().st_mode
            if stat.S_ISREG(mode) and mode & 0o222:
                p.chmod(mode & ~0o222)


def _tree_matches_pin(repo_dir: Path, target: str) -> bool:
    return _dvc(repo_dir, "status", "--quiet", target, timeout=None).returncode == 0


def _link_shared_chunk(repo_dir: Path, store_root: Path, target: str, md5: str) -> str:
    """Make ``<repo>/<target>`` a symlink to the node's shared copy of ``md5``.

    Builds the shared copy on first use: ``dvc checkout`` the chunk here once,
    then rename the linked tree into the store. A clean private tree already in
    place is adopted the same way, or removed when the copy exists. A modified
    one is left alone.
    """
    path = repo_dir / target
    copy = _shared_copy(store_root, target, md5)
    link_to = _relative_link_target(path, copy)

    if path.is_symlink():
        if _links_to(path, copy):
            _reprotect_files(copy)
            return "linked"
        path.unlink()
    elif path.exists():
        if not _tree_matches_pin(repo_dir, target):
            return "private:modified"
        if copy.is_dir():
            _remove_tree(path)

    copy.parent.mkdir(parents=True, exist_ok=True)
    with open(copy.parent / f".{md5}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not copy.is_dir():
            if not path.exists():
                co = _dvc(repo_dir, "checkout", "--quiet", target, timeout=None)
                if co.returncode != 0:
                    return f"missing-copy: {_out(co)[-300:]}"
            try:
                os.rename(path, copy)
            except OSError as exc:
                return f"private: could not move into {copy}: {exc}"
            _seal_dirs(copy)
        elif path.is_dir() and not path.is_symlink():
            _remove_tree(path)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(link_to)
    return "linked"


def shared_link_state(repo_dir: Path, store_root: Path, target: str, md5: str) -> str:
    """``linked``, ``stale-link``, ``missing-copy``, ``private`` or ``absent``, without hashing."""
    path = repo_dir / target
    copy = _shared_copy(store_root, target, md5)
    if path.is_symlink():
        if _links_to(path, copy):
            return "linked"
        return "stale-link" if copy.is_dir() else "missing-copy"
    if path.exists():
        return "private"
    return "absent" if copy.is_dir() else "missing-copy"


def _in_view(target: str, mounts: list[str] | None) -> bool:
    return mounts is None or target in {m.rstrip("/") for m in mounts}


# ---------------------------------------------------------------------------
# Read-only protection
# ---------------------------------------------------------------------------

def chmod_dirs_writable(path: Path) -> None:
    """Restore write permission on **directories only**, so a teardown can finish.

    Directories, never files, and the distinction is not fussiness — it is the
    difference between a safe teardown and silent corruption of every project on
    the machine.

    A DVC-materialised file is a *hardlink to the cache object*: same inode, and
    the read-only bit belongs to that inode. ``chmod +w`` on the workspace copy
    therefore unprotects the **shared cache object** that every other scope,
    every other project, and every historical commit referencing that hash reads
    through. One scope's teardown would leave the whole cache writable, and the
    next careless redirect into a "workspace" file would rewrite content under
    all of them. Measured: strip the bit in one worktree and a sibling worktree's
    copy becomes writable and corruptible.

    It is also unnecessary for files. DVC leaves *directories* writable and only
    the files read-only, and ``rm -rf`` needs write permission on the
    *containing* directory — so the file loop bought nothing and cost the cache.
    Directories are swept anyway: this runs as the fallback after ``worktree
    remove`` has already failed, which is exactly when the tree is in a shape
    nobody predicted.
    """
    for root, dirs, _files in os.walk(path, topdown=False):
        for name in dirs:
            p = Path(root) / name
            try:
                if not p.is_symlink() and p.is_dir():
                    p.chmod(p.stat().st_mode | stat.S_IWUSR)
            except OSError:
                pass
    try:
        if path.is_dir():
            path.chmod(path.stat().st_mode | stat.S_IWUSR)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Per-repo wiring
# ---------------------------------------------------------------------------

def _common_git_dir(repo_dir: Path) -> Path | None:
    r = _git(repo_dir, "rev-parse", "--git-common-dir")
    if r.returncode != 0:
        return None
    common = Path(r.stdout.strip())
    if not common.is_absolute():
        common = (repo_dir / common).resolve()
    return common


def _local_cache_type(local: Path) -> str | None:
    """The ``[cache] type`` a worktree's ``config.local`` sets, if any."""
    if not local.is_file():
        return None
    section = None
    for raw in local.read_text().splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().strip('"')
        elif section == "cache" and line.partition("=")[0].strip() == "type":
            return line.partition("=")[2].strip().strip('"') or None
    return None


def ensure_cache_config(repo_dir: Path) -> None:
    """Point this worktree at the shared cache, via the **untracked** local config.

    The cache path goes in ``.dvc/config.local`` (gitignored by ``dvc init``),
    never in the tracked ``.dvc/config``, and it is **absolute**.

    A tracked relative path looks tempting — scope worktrees are always at
    ``projects/<p>/<s>/`` so the depth to the workspace root is constant — but
    the tracked file is shared by *every* checkout of the repo, and the depth is
    only constant for scope worktrees. Any other checkout resolves the same
    relative path somewhere else and DVC silently starts a **second cache**
    there rather than erroring, duplicating every byte. Silent divergence is the
    one failure mode worth spending a file to avoid.

    The cost is that a checkout awm did not provision has no cache config and
    falls back to a repo-local ``.dvc/cache``. That is correct-but-unshared,
    which is the right direction to fail in.

    DVC agrees with the split: ``dvc init`` writes ``/config.local`` into
    ``.dvc/.gitignore`` itself, so this is the tool's own intended home for a
    machine-specific setting rather than a convention invented here. The *link
    type* is portable and stays in the tracked config; only the path is local.
    A ``type`` already in ``config.local`` survives the rewrite: a worktree whose
    files are edited in place, such as a live data root, sets ``copy`` there.
    """
    dvc_dir = repo_dir / ".dvc"
    dvc_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir(repo_dir)
    cache.mkdir(parents=True, exist_ok=True)

    local = dvc_dir / "config.local"
    body = (
        "# AWM-managed, untracked. Absolute on purpose: a tracked relative path\n"
        "# would resolve differently in a non-scope checkout and silently start a\n"
        "# second cache there.\n"
        "[cache]\n"
        f"    dir = {cache}\n"
    )
    link = _local_cache_type(local)
    if link:
        body += f"    type = {link}\n"
    site = site_cache_dir()
    if site is not None:
        body += f"[core]\n    site_cache_dir = {site}\n"
    if not local.exists() or local.read_text() != body:
        local.write_text(body)

    # hardlink first, symlink as the fallback for a cross-device cache. Both
    # mean one physical copy; the default (`copy`) would mean N. Portable, so
    # it belongs in the tracked config where every checkout inherits it.
    have = _dvc(repo_dir, "config", "--project", "cache.type")
    if (have.stdout or "").strip().strip('"') != "hardlink,symlink":
        _dvc(repo_dir, "config", "cache.type", "hardlink,symlink")


def ensure_repo_wiring(repo_dir: Path) -> dict:
    """Install the git-side machinery that makes a code merge move the data.

    Everything lands in the **common git dir**, which every worktree of the
    project shares and which is never committed — the same trick
    ``_ensure_awm_gitignored`` already uses for ``info/exclude``. So this costs
    no commit to any project repo and cannot conflict with anything a user tracks.

    Three pieces, and all three are required:

    1. ``merge.dvc.driver`` — teaches git how to merge two ``.dvc`` pins by
       combining their directory listings instead of conflicting on a hash line.
    2. ``*.dvc merge=dvc`` in ``info/attributes`` — the driver is inert without
       an attribute selecting it, and ``info/attributes`` is the untracked,
       worktree-shared home for it.
    3. ``post-merge`` and ``post-commit`` hooks running ``dvc checkout`` — DVC
       ships pre-commit, post-checkout and pre-push hooks but **not** these two,
       and between them they are what make a merge a single lever. Hooks in the
       common dir do fire for operations run inside a secondary worktree, with
       cwd set to that worktree; verified, and the whole design rests on it.

    Existing hooks this function did not write are **never overwritten** — it
    reports a conflict and leaves them alone.
    """
    actions: dict[str, str] = {}
    binp = dvc_bin()
    if not binp:
        return {"result": "unavailable", "detail": "dvc not found"}

    common = _common_git_dir(repo_dir)
    if common is None:
        return {"result": "error", "detail": f"{repo_dir} is not a git worktree"}

    # 1. the merge driver
    want_driver = f"{binp} git-hook merge-driver --ancestor %O --our %A --their %B"
    have = _git(repo_dir, "config", "--get", f"merge.{_MERGE_DRIVER_NAME}.driver")
    if (have.stdout or "").strip() != want_driver:
        _git(repo_dir, "config", f"merge.{_MERGE_DRIVER_NAME}.name", "DVC merge driver")
        _git(repo_dir, "config", f"merge.{_MERGE_DRIVER_NAME}.driver", want_driver)
        actions["merge_driver"] = "installed"

    # 2. the attribute that selects it. Inert driver without this.
    info = common / "info"
    info.mkdir(parents=True, exist_ok=True)
    attrs = info / "attributes"
    prior = attrs.read_text() if attrs.exists() else ""
    if _GIT_ATTRIBUTES_LINE not in prior.splitlines():
        with attrs.open("a") as fh:
            if prior and not prior.endswith("\n"):
                fh.write("\n")
            fh.write(_GIT_ATTRIBUTES_LINE + "\n")
        actions["attributes"] = "installed"

    # 3. the two hooks DVC does not ship
    hooks = common / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    fmt = {"dvc_bin": binp, "mounts_file": MOUNTS_FILE,
           "failed_file": CHECKOUT_FAILED_FILE, "shared_file": SHARED_DECLARATION}
    for name, template in (("post-merge", _POST_MERGE_HOOK),
                           ("post-commit", _POST_COMMIT_HOOK),
                           ("post-checkout", _POST_CHECKOUT_HOOK)):
        hook = hooks / name
        body = template.format(**fmt)
        if hook.exists():
            existing = hook.read_text()
            if existing == body:
                continue
            if _AWM_HOOK_MARKER not in existing:
                # Somebody else's hook. Refuse rather than clobber -- and say so,
                # because the one-lever property silently does not hold here.
                actions[f"{name}_hook"] = "conflict:foreign-hook-left-in-place"
                continue
        hook.write_text(body)
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        actions[f"{name}_hook"] = "installed"

    return {"result": "ok", "actions": actions, "git_common_dir": str(common)}


# ---------------------------------------------------------------------------
# The single entry point
# ---------------------------------------------------------------------------

def pin_would_be_ignored(repo_dir: Path) -> str | None:
    """The ignore rule that would swallow this repo's ``.dvc`` pins, if any.

    This is the trap that makes the whole scheme a silent no-op, and it is
    already armed in several repos. scadc's tracked ``.gitignore`` carries
    ``/data`` and ``/data/**/*`` (``data/`` used to be a runtime symlink), and
    awm's carries ``data/``. Under those rules ``dvc add`` writes
    ``data/<chunk>.dvc``, ``git add -A`` skips it, ``git status`` is clean, and
    the pin — the entire mechanism by which a commit records its data — is never
    committed. Nothing errors. You discover it when a colleague's checkout has
    no data and no explanation.

    So: probe the real thing. A ``.dvc`` path under ``data/`` that git ignores is
    a hard refusal, not a warning.
    """
    probe = f"{DATA_SUBDIR}/.awm-pin-probe.dvc"
    r = _git(repo_dir, "check-ignore", "-v", probe)
    if r.returncode != 0:
        return None
    return (r.stdout or "").strip().split("\t")[0] or "an ignore rule"


def data_path_conflict(repo_dir: Path) -> str | None:
    """Why ``<repo>/data`` cannot become the tracked data folder, if it cannot.

    Several worktrees already have a ``data`` **symlink** at the repo root — a
    hand-rolled convenience pointing at the shared data dir or at ``.awm/data``
    — and a few projects track a real ``data/`` of their own. ``dvc add``
    refuses a git-tracked path outright (*output … is already tracked by SCM*),
    and a stray symlink would be walked into rather than replaced.

    Either way the answer is to stop and report, never to delete something a
    human put there.
    """
    data = data_dir(repo_dir)
    if data.is_symlink():
        return (f"{data} is a symlink to {os.readlink(str(data))!r}. It predates "
                f"this layer; move it aside so `data/` can become the tracked "
                f"data folder.")
    if data.is_dir():
        tracked = _git(repo_dir, "ls-files", "--error-unmatch", DATA_SUBDIR)
        if tracked.returncode == 0 and (tracked.stdout or "").strip():
            # Tracked *and* not pinned by us == somebody else's data folder.
            if not any(data.glob("*.dvc")) and not chunk_pins(repo_dir):
                return (f"{data} is already tracked by git and holds no DVC pins. "
                        f"`dvc add` refuses an SCM-tracked output; migrate or move "
                        f"it before converting this project.")
    return None


def provision_scope_data(project: str, scope: str, awm_dir: Path) -> dict:
    """Materialise a scope's data view. The single entry point.

    Returns ``{mode, ...}`` with ``mode`` ∈ ``dvc | symlink | unknown``.
    Idempotent, so it is safe from scope creation *and* from the repair path —
    which is what lets ``scope heal`` migrate every scope that pre-dates this.

    Note what this does **not** do, and why. It does not create, clone, branch or
    fetch anything: a scope's data is whatever its commit pins, and the commit
    arrived with the git checkout. Provisioning is therefore only ever *wiring
    plus checkout*, which is the entire reason the pin-in-the-code-repo design
    removes so much machinery.
    """
    if not awm_dir.is_absolute():
        # A relative awm_dir is a caller bug that fails destructively rather
        # than merely wrongly, so it is refused rather than normalised: there is
        # no correct anchor to normalise against, only a wrong one.
        return {
            "mode": "unknown",
            "path": str(awm_dir / "data"),
            "detail": (
                f"refusing a relative .awm path ({awm_dir}): it would resolve "
                f"against the wrong directory. Pass an absolute worktree path."
            ),
        }

    repo_dir = awm_dir.parent
    compat = awm_dir / "data"

    if not is_dvc_project(repo_dir):
        reason = (
            "dvc not found (set AWM_DVC_BIN)" if enabled() and not dvc_available()
            else "project not DVC-backed"
        )
        return _provision_symlink(project, compat, reason=reason)

    # Two gates, both hard refusals, both for silent failures rather than loud
    # ones -- which is exactly why they are worth a round trip each.
    ignored_by = pin_would_be_ignored(repo_dir)
    if ignored_by:
        return {
            "mode": "unknown", "path": str(data_dir(repo_dir)),
            "detail": (
                f"refusing: {ignored_by} makes git ignore `{DATA_SUBDIR}/*.dvc`, so "
                f"every data pin would be written and then silently never committed. "
                f"Remove the rule covering `{DATA_SUBDIR}/` before converting."
            ),
        }
    conflict = data_path_conflict(repo_dir)
    if conflict:
        return {"mode": "unknown", "path": str(data_dir(repo_dir)), "detail": conflict}

    awm_dir.mkdir(parents=True, exist_ok=True)
    # Cache config first: it is a precondition for the checkout below, and a
    # checkout against the wrong cache is how a second cache gets built.
    ensure_cache_config(repo_dir)
    wiring = ensure_repo_wiring(repo_dir)

    report: dict = {
        "mode": "dvc",
        "path": str(data_dir(repo_dir)),
        "cache": str(cache_dir(repo_dir)),
        "rev": _head_rev(repo_dir),
        "branch": _current_branch(repo_dir),
        "wiring": wiring.get("actions", {}),
    }

    pins = chunk_pins(repo_dir)
    report["chunks"] = len(pins)
    mounts = read_mounts(awm_dir)
    report["mounts"] = mounts if mounts is not None else "all"
    store_root = shared_store_root(repo_dir)
    shared = shared_chunks(repo_dir) if store_root else {}
    # Materialise only what this scope mounts. A bare `dvc checkout` would
    # materialise EVERY pin -- there is no "pinned but not materialised" flag in
    # DVC -- which on scadc means dragging ~65 GB of cold chunks and ~122k
    # inodes into a scope that asked for none of it.
    #
    # `mounts is None` (no list) means everything; an empty list means nothing.
    # Those must not collapse, or opting out of every chunk would opt you in.
    #
    # Shared chunks never go through the per-scope checkout, so once a project
    # declares any, the target list is always explicit.
    targets = mounts
    if shared and mounts != []:
        base = mounts if mounts is not None else [p.removesuffix(".dvc") for p in pins]
        targets = [t for t in base if t.rstrip("/") not in shared]
    if pins and targets != [] and mounts != []:
        co = _dvc(repo_dir, "checkout", "--quiet", *(targets or []), timeout=None)
        report["checkout"] = "ok" if co.returncode == 0 else "partial"
        if co.returncode != 0:
            report["checkout_detail"] = _out(co)[-400:]
    else:
        report["checkout"] = "skipped"

    if shared:
        report["shared"] = {
            t: _link_shared_chunk(repo_dir, store_root, t, md5)
            for t, md5 in shared.items() if _in_view(t, mounts)
        }

    # A sentinel left by the hook means a previous merge advanced a pin whose
    # content the cache could not supply -- and that `dvc checkout` deleted the
    # old files on its way to failing. Surface it every time until it clears.
    sentinel = awm_dir / CHECKOUT_FAILED_FILE
    if sentinel.exists():
        checked_out = report["checkout"] == "ok" or (shared and report["checkout"] == "skipped")
        linked = all(v == "linked" or v.startswith("private")
                     for v in report.get("shared", {}).values())
        if checked_out and linked:
            sentinel.unlink()
        else:
            report["stale_checkout"] = sentinel.read_text()[-400:]

    # `.awm/data` kept resolving for 125 scadc files and the documented
    # layout, so it survives as a relative symlink to the real data folder
    # rather than as a second copy. This makes repointing those callers optional
    # cleanup instead of a blocking migration step.
    linked = _link_compat_path(compat, repo_dir)
    report["compat_symlink"] = str(compat) if linked == "ok" else linked
    return report


def read_mounts(awm_dir: Path) -> list[str] | None:
    """Which chunks this scope wants on disk, or None for "all of them".

    Mounting is a **local** decision, not a property of the commit: fig-resolve
    pins `_old` and `uniref50` exactly as every other scope does — same hashes,
    same backup coverage, same reproducibility — it just does not want 65 GB of
    them materialised. So the list lives under gitignored ``.awm/`` and does not
    travel, while the *pins* live in the commit and do.

    Absent file means every chunk, which is the right default: a scope that has
    not thought about it should see all its data.
    """
    path = awm_dir / MOUNTS_FILE
    if not path.is_file():
        return None
    out: list[str] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def write_mounts(awm_dir: Path, chunks: list[str]) -> Path:
    """Declare the chunks this scope wants materialised."""
    awm_dir.mkdir(parents=True, exist_ok=True)
    path = awm_dir / MOUNTS_FILE
    path.write_text(
        "# AWM-managed, scope-local, gitignored. One chunk path per line.\n"
        "# These are the chunks materialised on disk here. Every chunk this\n"
        "# commit pins stays pinned and backed up whether or not it is listed --\n"
        "# this file only decides what costs you inodes and checkout time.\n"
        "# Delete this file to materialise everything.\n"
        + "".join(f"{c}\n" for c in chunks)
    )
    return path


def _link_compat_path(compat: Path, repo_dir: Path) -> str:
    """Point ``.awm/data`` at ``../data`` — relative, so it survives a move.

    Returns what actually happened, because the refusal below is the more
    dangerous outcome and the caller must be able to report it. A scope with a
    real directory left at ``.awm/data`` reads *that* through every one of the
    125+ call sites naming the path, while its code expects ``../data`` — and
    there is no error anywhere. It must not be reported as success.
    """
    target = Path("..") / DATA_SUBDIR
    if compat.is_symlink():
        if os.readlink(str(compat)) == str(target):
            return "ok"
        compat.unlink()
    elif compat.is_dir():
        # Somebody's files, not ours to delete -- leave them and say so.
        log.warning("%s is a real directory; leaving it and skipping the compat "
                    "symlink. Move it aside once its content is migrated.", compat)
        return "refused:real-directory"
    elif compat.exists():
        compat.unlink()
    data_dir(repo_dir).mkdir(parents=True, exist_ok=True)
    compat.symlink_to(target)
    return "ok"


def _provision_symlink(project: str, dest: Path, *, reason: str) -> dict:
    """The pre-DVC behaviour, verbatim: one shared directory, symlinked in."""
    target = legacy_data_dir(project)
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


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def _current_branch(repo: Path) -> str:
    return (_git(repo, "branch", "--show-current").stdout or "").strip()


def _head_rev(repo: Path) -> str:
    r = _git(repo, "rev-parse", "HEAD")
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def data_status(project: str, scope: str, worktree: Path) -> dict:
    """Describe a scope's data view: mode, the commit that pins it, and drift.

    There is no separate data branch to be behind — the code revision **is** the
    data revision — so the question is not "how far apart are my two histories?"
    but "does my workspace match what my commit pins?", which is what
    ``dvc status`` answers.
    """
    repo_dir = worktree
    compat = worktree / ".awm" / "data"

    if not is_dvc_repo(repo_dir):
        if compat.is_symlink():
            return {"project": project, "scope": scope, "mode": "symlink",
                    "target": os.readlink(str(compat))}
        if not compat.exists():
            return {"project": project, "scope": scope, "mode": "missing"}
        return {"project": project, "scope": scope, "mode": "plain-dir",
                "path": str(compat)}

    pins = chunk_pins(repo_dir)
    awm_dir = worktree / ".awm"
    mounts = read_mounts(awm_dir)
    out: dict = {
        "project": project, "scope": scope, "mode": "dvc",
        "path": str(data_dir(repo_dir)),
        "branch": _current_branch(repo_dir),
        "rev": _head_rev(repo_dir),
        "chunks": pins,
        "mounts": mounts if mounts is not None else "all",
        "cache": str(cache_dir(repo_dir)),
        "dvc_bin": dvc_bin(),
    }
    # The one genuinely broken state, and it is silent unless something says so:
    # a merge advanced a pin the cache could not satisfy, and the checkout that
    # failed had already removed the previous files.
    sentinel = awm_dir / CHECKOUT_FAILED_FILE
    if sentinel.exists():
        out["checkout_failed"] = sentinel.read_text()[-600:]
    store_root = shared_store_root(repo_dir)
    shared = shared_chunks(repo_dir) if store_root else {}
    targets: list[str] = []
    if shared:
        out["shared"] = {t: shared_link_state(repo_dir, store_root, t, md5)
                         for t, md5 in shared.items()}
        # A shared copy is checked once, when it is built. Hashing it from
        # every scope would cost what sharing it saves.
        targets = [p.removesuffix(".dvc") for p in pins
                   if p.removesuffix(".dvc") not in shared
                   or out["shared"][p.removesuffix(".dvc")] == "private"]
    if dvc_available() and (targets or not shared):
        # Workspace-vs-pin, not branch-vs-branch. Empty output means the
        # materialised files are exactly what this commit says they are.
        st = _dvc(repo_dir, "status", "--quiet", *targets)
        out["in_sync"] = st.returncode == 0
        if st.returncode != 0:
            out["drift"] = _out(st)[-600:]
        # A pin whose content is absent from the cache is the one genuinely
        # broken state, and it is worth naming separately from ordinary drift.
        missing = _dvc(repo_dir, "data", "status", "--granular", "--json", *targets)
        if missing.returncode == 0 and missing.stdout:
            out["data_status"] = missing.stdout.strip()[:2000]
    return out


# ---------------------------------------------------------------------------
# Garbage collection
# ---------------------------------------------------------------------------

# An unreferenced object younger than this is never collected. It is the race
# guard: a `dvc add` writes its objects before its pin exists anywhere git can
# see, and this engine takes no repo locks to wait it out.
GC_GRACE_DAYS = 14

_PIN_PATHSPECS = ("*.dvc", "*dvc.lock")
_BLOB_MODES = ("100644", "100755")
_NULL_SHA = "0" * 40


class GcRefused(Exception):
    """A condition under which no object may be deleted."""


@dataclass
class GcProject:
    name: str
    bare: Path
    worktrees: list[Path] = field(default_factory=list)


def _git_z(repo: Path, *args: str) -> list[str]:
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, env=_env())
    if r.returncode != 0:
        raise GcRefused(f"git {' '.join(args[:2])} failed in {repo}: "
                        f"{r.stderr.decode(errors='replace').strip()[-400:]}")
    return r.stdout.decode(errors="surrogateescape").split("\0")


def _is_pin_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name == "dvc.lock" or (name.endswith(".dvc") and name != ".dvc")


def _history_pin_blobs(bare: Path) -> dict[str, str]:
    """Every pin blob any commit reachable from any ref ever introduced.

    The first-parent diff of every commit, with ``--root``, visits every blob
    of every reachable tree at least once: a blob either differs from the
    first parent's at its path, or it was already introduced further back. A
    merge's conflict resolution shows up as its own first-parent change.
    """
    toks = _git_z(bare, "log", "--all", "--root", "--diff-merges=first-parent",
                  "--no-renames", "--raw", "--no-abbrev", "-z", "--format=",
                  "--", *_PIN_PATHSPECS)
    blobs: dict[str, str] = {}
    i = 0
    while i < len(toks):
        meta = toks[i].lstrip("\n")
        if not meta.startswith(":") or i + 1 >= len(toks):
            i += 1
            continue
        path = toks[i + 1]
        i += 2
        _old_mode, new_mode, _old, new, *_ = meta[1:].split()
        if new_mode in _BLOB_MODES and new != _NULL_SHA and _is_pin_path(path):
            blobs.setdefault(new, path)
    return blobs


def _staged_pin_blobs(worktree: Path) -> dict[str, str]:
    blobs: dict[str, str] = {}
    for rec in _git_z(worktree, "ls-files", "-s", "-z", "--", *_PIN_PATHSPECS):
        if not rec:
            continue
        info, _, path = rec.partition("\t")
        mode, sha, _stage = info.split()
        if mode in _BLOB_MODES and _is_pin_path(path):
            blobs.setdefault(sha, path)
    return blobs


def _unstaged_pin_files(worktree: Path) -> list[Path]:
    """Modified and untracked pins on disk. Gitignored pins are not listed:
    walking ignored trees costs minutes per worktree, and the grace window
    covers the pin a ``dvc add`` has written but nobody has staged yet."""
    paths = _git_z(worktree, "ls-files", "-z", "-m", "-o", "--exclude-standard",
                   "--", *_PIN_PATHSPECS)
    return [worktree / p for p in dict.fromkeys(paths) if p and _is_pin_path(p)]


def _read_blobs(bare: Path, shas: set[str]) -> dict[str, bytes]:
    if not shas:
        return {}
    r = subprocess.run(["git", "-C", str(bare), "cat-file", "--batch"],
                       input="".join(f"{s}\n" for s in shas).encode(),
                       capture_output=True, env=_env())
    if r.returncode != 0:
        raise GcRefused(f"git cat-file failed in {bare}: "
                        f"{r.stderr.decode(errors='replace').strip()[-400:]}")
    out, i, blobs = r.stdout, 0, {}
    while i < len(out):
        nl = out.index(b"\n", i)
        header = out[i:nl].decode().split()
        if len(header) < 3:
            raise GcRefused(f"pin blob {header[0]} is {header[1]} in {bare}")
        size = int(header[2])
        blobs[header[0]] = out[nl + 1:nl + 1 + size]
        i = nl + 1 + size + 1
    return blobs


def _pin_hashes(text: bytes | str, origin: str) -> set[str]:
    """The md5 of every output a ``.dvc`` file or ``dvc.lock`` names.

    Outputs only, as DVC's own gc does: a dependency hash records what a stage
    read, and that content is kept, or not, by whichever pin outputs it.
    """
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise GcRefused(f"cannot parse pin {origin}: {e}") from e
    if doc is None:
        return set()
    if not isinstance(doc, dict):
        raise GcRefused(f"pin {origin} is not a mapping")
    outs = list(doc.get("outs") or [])
    if origin.endswith("dvc.lock"):
        # schema 2 nests stages under `stages`; schema 1 put them at top level.
        stages = doc.get("stages") if "schema" in doc else doc
        for stage in (stages or {}).values():
            if isinstance(stage, dict):
                outs += stage.get("outs") or []
    hashes: set[str] = set()
    for out in outs:
        if not isinstance(out, dict):
            raise GcRefused(f"pin {origin} has a malformed output: {out!r}")
        for entry in [out, *(out.get("files") or [])]:
            md5 = entry.get("md5") if isinstance(entry, dict) else None
            if md5:
                hashes.add(str(md5).lower())
    return hashes


def _object_path(cache: Path, oid: str) -> Path:
    return cache / "files" / "md5" / oid[:2] / oid[2:]


def _expand_dirs(cache: Path, hashes: dict[str, str],
                 missing: dict[str, str]) -> set[str]:
    """Add every file a ``.dir`` manifest lists. A manifest absent from the
    cache goes into ``missing``: its children are unknown, so nothing is safe."""
    expanded = set(hashes)
    for oid, origin in hashes.items():
        if not oid.endswith(".dir"):
            continue
        try:
            entries = json.loads(_object_path(cache, oid).read_bytes())
        except FileNotFoundError:
            missing[oid] = origin
            continue
        except (OSError, ValueError) as e:
            raise GcRefused(f"cannot read .dir manifest {oid}: {e}") from e
        for entry in entries:
            if entry.get("md5"):
                expanded.add(str(entry["md5"]).lower())
    return expanded


def project_keep_set(project: GcProject, cache: Path) -> tuple[set[str], dict]:
    """Every cache object one project still references, read without a lock.

    Read once per bare repo, not once per worktree: history is shared by every
    worktree, so only the index and the unstaged files differ between them.
    """
    started = time.monotonic()
    blobs = _history_pin_blobs(project.bare)
    history_blobs = len(blobs)
    staged: dict[str, str] = {}
    on_disk: list[Path] = []
    missing_worktrees: list[str] = []
    for wt in project.worktrees:
        if not wt.is_dir():
            missing_worktrees.append(str(wt))
            continue
        staged |= _staged_pin_blobs(wt)
        # `dvc add` cannot run outside a DVC repo, so only a DVC worktree can
        # hold an unstaged pin, and the untracked walk is the slow part.
        if is_dvc_repo(wt):
            on_disk += _unstaged_pin_files(wt)

    blobs = staged | blobs
    hashes: dict[str, str] = {}
    for sha, body in _read_blobs(project.bare, set(blobs)).items():
        origin = f"{project.name}:{blobs[sha]}"
        for oid in _pin_hashes(body, origin):
            hashes.setdefault(oid, origin)
    for pin in on_disk:
        try:
            body = pin.read_bytes()
        except FileNotFoundError:
            continue  # `ls-files -m` lists a deleted pin too
        for oid in _pin_hashes(body, str(pin)):
            hashes.setdefault(oid, str(pin))

    missing: dict[str, str] = {}
    keep = _expand_dirs(cache, hashes, missing)
    return keep, {
        "worktrees": len(project.worktrees),
        "missing_worktrees": missing_worktrees,
        "pin_blobs": len(blobs),
        "history_pin_blobs": history_blobs,
        "unstaged_pins": len(on_disk),
        "referenced": len(keep),
        "missing_dir_manifests": dict(sorted(missing.items())),
        "seconds": round(time.monotonic() - started, 2),
    }


def _check_cache_layout(cache: Path) -> None:
    """Refuse a cache this engine does not understand.

    Only DVC 3's ``files/md5`` layout is walked. A legacy 2.x top-level object
    or a run-cache would be invisible to the sweep and to the keep-set alike.
    """
    if not cache.is_dir():
        raise GcRefused(f"no cache at {cache}")
    extra = sorted(p.name for p in cache.iterdir() if p.name != "files")
    extra += sorted(f"files/{p.name}" for p in (cache / "files").iterdir()
                    if p.name != "md5")
    if extra:
        raise GcRefused(f"unknown cache layout at {cache}: {', '.join(extra[:10])}")


def _gc_lock_path(cache: Path) -> Path:
    return cache.parent / f"{cache.name}.gc.lock"


def _acquire_gc_lock(cache: Path):
    path = _gc_lock_path(cache)
    fh = path.open("a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.seek(0)
        holder = fh.read().strip() or "unknown holder"
        fh.close()
        raise GcRefused(f"another gc is running ({holder})") from None
    fh.seek(0)
    fh.truncate()
    fh.write(f"pid {os.getpid()} since {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n")
    fh.flush()
    return fh


def _sweep(cache: Path, keep: set[str], *, dry_run: bool, grace_s: float) -> dict:
    cutoff = time.time() - grace_s
    stats = {"objects": 0, "referenced": 0, "grace_protected": 0,
             "collected": 0, "collected_bytes": 0, "bytes_freed": 0,
             "still_linked": 0, "errors": []}
    md5_root = cache / "files" / "md5"
    for shard in sorted(os.scandir(md5_root), key=lambda e: e.name):
        if shard.is_dir(follow_symlinks=False):
            entries = [(shard.name + obj.name, obj) for obj in os.scandir(shard.path)]
        elif shard.name.startswith(".") and shard.name.endswith(".tmp"):
            # An interrupted transfer's partial file. Never referenced, so the
            # grace window alone decides whether it is still in flight.
            entries = [(shard.name, shard)]
        else:
            raise GcRefused(f"unknown cache layout: {shard.path} is not a shard dir")
        for oid, obj in entries:
            _sweep_one(oid, obj, keep, cutoff, dry_run, stats)
    stats["errors"] = stats["errors"][:20]
    return stats


def _sweep_one(oid: str, obj: os.DirEntry, keep: set[str], cutoff: float,
               dry_run: bool, stats: dict) -> None:
    stats["objects"] += 1
    if oid in keep:
        stats["referenced"] += 1
        return
    st = obj.stat(follow_symlinks=False)
    if max(st.st_mtime, st.st_ctime) > cutoff:
        stats["grace_protected"] += 1
        return
    stats["collected"] += 1
    stats["collected_bytes"] += st.st_size
    # A second link means a worktree still holds the inode, so deleting the
    # cache entry frees nothing yet.
    if st.st_nlink > 1:
        stats["still_linked"] += 1
    else:
        stats["bytes_freed"] += st.st_size
    if dry_run:
        return
    try:
        os.unlink(obj.path)
    except PermissionError:
        chmod_dirs_writable(Path(obj.path).parent)
        try:
            os.unlink(obj.path)
        except OSError as e:
            stats["errors"].append(f"{obj.path}: {e}")
    except OSError as e:
        stats["errors"].append(f"{obj.path}: {e}")


def collect_garbage(projects: list[GcProject], *, wired: set[str],
                    exclude: list[str] | None = None,
                    accept_missing: list[str] | None = None, dry_run: bool = True,
                    keep: str = "all-commits",
                    grace_days: float = GC_GRACE_DAYS) -> dict:
    """Delete cache objects no listed project references. **Guarded**, dry by default.

    Takes no DVC repo lock and never runs ``dvc gc``, which locks every listed
    worktree for its whole run and re-walks the shared history once per
    worktree. Refuses, deleting nothing, unless every wired project is either
    listed or deliberately excluded, every referenced ``.dir`` manifest is in
    the cache or named in ``accept_missing``, and no other gc is running.
    """
    if keep != "all-commits":
        return {"result": "refused", "detail":
                f"keep={keep!r} refused. Only 'all-commits' is supported: it keeps "
                f"every output any commit, staged index or worktree file pins."}
    if not projects:
        return {"result": "refused", "detail":
                "no projects given — collecting against an incomplete set "
                "treats every other project's content as garbage"}
    listed = {p.name for p in projects}
    excluded = set(exclude or [])
    if listed & excluded:
        return {"result": "refused", "detail":
                f"projects both listed and excluded: {sorted(listed & excluded)}"}
    unlisted = sorted(wired - listed - excluded)
    if unlisted:
        return {"result": "refused", "detail":
                f"DVC-wired projects neither listed nor excluded: {unlisted}. "
                f"Their objects would be collected. List them in `projects` to "
                f"keep their data, or in `exclude` to drop it deliberately."}

    cache = cache_dir()
    report: dict = {"dry_run": dry_run, "deleted_anything": False, "keep": keep,
                    "cache": str(cache), "grace_days": grace_days,
                    "excluded": sorted(excluded),
                    "accepted_missing": sorted(accept_missing or [])}
    started = time.monotonic()
    try:
        _check_cache_layout(cache)
        lock = _acquire_gc_lock(cache)
    except GcRefused as e:
        return {"result": "refused", "detail": str(e), **report}
    try:
        keep_set: set[str] = set()
        per_project: dict[str, dict] = {}
        for proj in projects:
            refs, info = project_keep_set(proj, cache)
            keep_set |= refs
            per_project[proj.name] = info
        report["projects"] = per_project
        accepted = set(accept_missing or [])
        missing = {oid: origin for info in per_project.values()
                   for oid, origin in info["missing_dir_manifests"].items()
                   if oid not in accepted}
        if missing:
            shown = dict(list(missing.items())[:10])
            return {"result": "refused", **report, "detail":
                    f"{len(missing)} referenced .dir manifests are absent from "
                    f"the cache, so their files cannot be told apart from "
                    f"garbage: {shown}. Restore them from the archive, or pass "
                    f"their ids in `accept_missing` once you know the files "
                    f"are gone too."}
        report["sweep"] = _sweep(cache, keep_set, dry_run=dry_run,
                                 grace_s=grace_days * 86400)
    except GcRefused as e:
        return {"result": "refused", "detail": str(e), **report}
    finally:
        lock.close()
    report["deleted_anything"] = not dry_run and report["sweep"]["collected"] > 0
    report["seconds"] = round(time.monotonic() - started, 2)
    return {"result": "ok", **report}
