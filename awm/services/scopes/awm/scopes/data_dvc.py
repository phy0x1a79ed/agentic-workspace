"""DVC data layer — a commit records code and the data it was built against.

Code gets its concurrency model from git worktrees. Data used to get a parallel
one: a per-scope git-annex clone on its own branch, with its own history and its
own promote verb. Two histories meant two levers, and two levers drift — a scope
created from a parent branch inherited the parent's *code* and the project's
*canonical* data, because scope creation threads a base branch into the code
worktree and nothing analogous existed on the data side.

The deeper cost was not the drift but the workaround it forced. With no
versioning a superseded dataset cannot safely be deleted, so every variant stays
live side by side — tier 3 beside tier 4, `set2` beside `set4`, three null tables
in two days. The recurring "which one is current?" failure is a *symptom of not
being able to delete*. Versioning makes deletion safe, and deletion makes the
question unaskable.

DVC inverts the arrangement. The **pin** — a ~110-byte text file recording a
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
  ``<repo>/data/`` and is tracked, which is why ``.awm/`` goes back to being
  wholly gitignored. The annex layer hid data under ``.awm/`` because the clone
  had to be *invisible*; a DVC pin has to be *visible*, so the reason is gone.
  ``.awm/data`` survives only as a compatibility symlink to ``../data``.

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

import logging
import os
import shutil
import stat
import subprocess
from functools import lru_cache
from pathlib import Path

from awm import config as _config

log = logging.getLogger("awm.scopes.data_dvc")

# The data folder inside a project repo. Tracked, first-class, same name in
# every project — scripts hardcode `<repo>/data/...` and that is the point.
DATA_SUBDIR = "data"

# One cache for the whole workspace. Content-addressed, so N scopes holding the
# same chunk cost one physical copy (hardlinked), and two projects that happen
# to hold the same file store it once.
CACHE_DIRNAME = ".dvc_cache"

# Never hashed into the cache, therefore never carried off-site by the nightly
# mirror. This is the load-bearing exclusion list: anything that reaches the
# cache is in the backup, and a secret added once cannot be unadded.
_DVCIGNORE = """\
# AWM-managed. Paths here are never hashed into the DVC cache, so they never
# reach the shared store and never leave this machine via the nightly mirror.

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
rm -f "$SENTINEL"
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
if ! out=$("$DVC" checkout --quiet "${{targets[@]}}" 2>&1); then
  mkdir -p .awm
  {{ echo "dvc checkout FAILED after $1"; echo "$out"; }} > "$SENTINEL"
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
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env=_env(), timeout=timeout,
    )


def _dvc(repo: Path, *args: str, timeout: int | None = None) -> subprocess.CompletedProcess:
    binp = dvc_bin()
    if not binp:
        return subprocess.CompletedProcess([], 127, "", "dvc not found")
    return subprocess.run(
        [binp, *args], cwd=str(repo),
        capture_output=True, text=True, env=_env(), timeout=timeout,
    )


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


def cache_dir() -> Path:
    """The one shared content-addressed cache, for every project on this machine."""
    return _config.DATA_DIR / CACHE_DIRNAME


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

    It is also unnecessary for files. Unlike git-annex — which marks object files
    *and their parent directories* read-only — DVC leaves directories writable,
    and ``rm -rf`` only needs write permission on the *containing directory*. So
    the file loop bought nothing and cost the cache.

    Directories are still worth doing: a legacy git-annex clone may be sitting in
    this worktree mid-migration, and those directories genuinely are read-only.
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
    """
    dvc_dir = repo_dir / ".dvc"
    dvc_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir()
    cache.mkdir(parents=True, exist_ok=True)

    local = dvc_dir / "config.local"
    body = (
        "# AWM-managed, untracked. Absolute on purpose: a tracked relative path\n"
        "# would resolve differently in a non-scope checkout and silently start a\n"
        "# second cache there.\n"
        "[cache]\n"
        f"    dir = {cache}\n"
    )
    if not local.exists() or local.read_text() != body:
        local.write_text(body)

    # hardlink first, symlink as the fallback for a cross-device cache. Both
    # mean one physical copy; the default (`copy`) would mean N. Portable, so
    # it belongs in the tracked config where every checkout inherits it.
    have = _dvc(repo_dir, "config", "cache.type")
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
           "failed_file": CHECKOUT_FAILED_FILE}
    for name, template in (("post-merge", _POST_MERGE_HOOK),
                           ("post-commit", _POST_COMMIT_HOOK)):
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
    human put there. Same posture the annex layer took for an unrecognised
    ``.awm/data``.
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
        "cache": str(cache_dir()),
        "rev": _head_rev(repo_dir),
        "branch": _current_branch(repo_dir),
        "wiring": wiring.get("actions", {}),
    }

    pins = chunk_pins(repo_dir)
    report["chunks"] = len(pins)
    mounts = read_mounts(awm_dir)
    report["mounts"] = mounts if mounts is not None else "all"
    # Materialise only what this scope mounts. A bare `dvc checkout` would
    # materialise EVERY pin -- there is no "pinned but not materialised" flag in
    # DVC -- which on scadc means dragging ~65 GB of cold chunks and ~122k
    # inodes into a scope that asked for none of it.
    #
    # `mounts is None` (no list) means everything; an empty list means nothing.
    # Those must not collapse, or opting out of every chunk would opt you in.
    if pins and mounts != []:
        co = _dvc(repo_dir, "checkout", "--quiet", *(mounts or []), timeout=None)
        report["checkout"] = "ok" if co.returncode == 0 else "partial"
        if co.returncode != 0:
            report["checkout_detail"] = _out(co)[-400:]
    else:
        report["checkout"] = "skipped"

    # A sentinel left by the hook means a previous merge advanced a pin whose
    # content the cache could not supply -- and that `dvc checkout` deleted the
    # old files on its way to failing. Surface it every time until it clears.
    sentinel = awm_dir / CHECKOUT_FAILED_FILE
    if sentinel.exists():
        if report.get("checkout") == "ok":
            sentinel.unlink()
        else:
            report["stale_checkout"] = sentinel.read_text()[-400:]

    # `.awm/data` kept resolving for 125 scadc files and the WORKSPACE.md
    # contract, so it survives as a relative symlink to the real data folder
    # rather than as a second copy. This makes repointing those callers optional
    # cleanup instead of a blocking migration step.
    _link_compat_path(compat, repo_dir)
    report["compat_symlink"] = str(compat)
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


def _link_compat_path(compat: Path, repo_dir: Path) -> None:
    """Point ``.awm/data`` at ``../data`` — relative, so it survives a move."""
    target = Path("..") / DATA_SUBDIR
    if compat.is_symlink():
        if os.readlink(str(compat)) == str(target):
            return
        compat.unlink()
    elif compat.is_dir():
        # A real directory here is either the old annex clone or somebody's
        # files. Either way it is not ours to delete -- leave it and say so.
        log.warning("%s is a real directory; leaving it and skipping the compat "
                    "symlink. Move it aside once its content is migrated.", compat)
        return
    elif compat.exists():
        compat.unlink()
    data_dir(repo_dir).mkdir(parents=True, exist_ok=True)
    compat.symlink_to(target)


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


def collect_garbage(repos: list[Path], *, dry_run: bool = True,
                    keep: str = "all-commits") -> dict:
    """Reclaim cache objects no listed repository references. **Guarded.**

    This is the only operation in the workspace that can destroy data, and it is
    wrapped rather than exposed because every one of its defaults is wrong here.

    * The cache is shared by **every** project, so a collection that does not
      name all of them treats the others' content as garbage. Hence ``repos`` is
      required and plural, and DVC is told about each with ``-p``.
    * The safe revision set is ``--all-commits``, **not** ``--all-branches``.
      The latter keeps only branch *tips*, which would delete content referenced
      by historical commits — silently breaking the consistent-snapshot property
      that is the entire point of this layer. ``--workspace`` is worse still.
    * It fails **late and quietly**. Files already materialised survive, because
      the workspace hardlink keeps the inode alive; the loss only surfaces at
      the next fresh checkout, in some other scope, possibly weeks later.
    * ``dvc gc --dry`` prints "**Removed** N objects" rather than "would
      remove", so its output cannot be used to tell a dry run from a real one.
      The ``dry_run`` flag in the returned report is the only trustworthy
      statement of which one happened, which is why it is always reported.

    Defaults to a dry run on purpose: the caller must ask for deletion.
    """
    if not dvc_available():
        return {"result": "unavailable", "detail": "dvc not found"}
    if not repos:
        return {"result": "refused", "detail":
                "no repositories given — collecting against an incomplete set "
                "treats every other project's content as garbage"}
    if keep not in ("all-commits", "all-tags"):
        return {"result": "refused", "detail":
                f"keep={keep!r} refused. Only 'all-commits' (and 'all-tags') "
                f"preserve content referenced by historical commits; "
                f"'all-branches' keeps branch tips ONLY and 'workspace' keeps "
                f"just what is checked out right now."}

    args = ["gc", f"--{keep}", "-f"]
    for r in repos:
        args += ["-p", str(r)]
    if dry_run:
        args.append("--dry")
    out = _dvc(repos[0], *args, timeout=None)
    return {
        "result": "ok" if out.returncode == 0 else "error",
        # Stated explicitly because DVC's own output does NOT distinguish these.
        "dry_run": dry_run,
        "deleted_anything": (not dry_run) and out.returncode == 0,
        "keep": keep,
        "repos": [str(r) for r in repos],
        "cache": str(cache_dir()),
        "output": _out(out)[-2000:],
    }


def data_status(project: str, scope: str, worktree: Path) -> dict:
    """Describe a scope's data view: mode, the commit that pins it, and drift.

    Under the annex layer this had to report a *separate* data branch and how
    far it had drifted from canonical. There is no such thing now — the code
    revision **is** the data revision — so the interesting question changed from
    "how far apart are my two histories?" to "does my workspace match what my
    commit pins?", which is what ``dvc status`` answers.
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
        "cache": str(cache_dir()),
        "dvc_bin": dvc_bin(),
    }
    # The one genuinely broken state, and it is silent unless something says so:
    # a merge advanced a pin the cache could not satisfy, and the checkout that
    # failed had already removed the previous files.
    sentinel = awm_dir / CHECKOUT_FAILED_FILE
    if sentinel.exists():
        out["checkout_failed"] = sentinel.read_text()[-600:]
    if dvc_available():
        # Workspace-vs-pin, not branch-vs-branch. Empty output means the
        # materialised files are exactly what this commit says they are.
        st = _dvc(repo_dir, "status", "--quiet")
        out["in_sync"] = st.returncode == 0
        if st.returncode != 0:
            out["drift"] = _out(st)[-600:]
        # A pin whose content is absent from the cache is the one genuinely
        # broken state, and it is worth naming separately from ordinary drift.
        missing = _dvc(repo_dir, "data", "status", "--granular", "--json")
        if missing.returncode == 0 and missing.stdout:
            out["data_status"] = missing.stdout.strip()[:2000]
    return out
