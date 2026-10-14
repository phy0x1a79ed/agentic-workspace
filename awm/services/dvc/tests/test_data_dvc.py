"""Tests for the DVC data layer.

Two things are being asserted here, and they are different in kind.

The **degradation** tests need no dvc: a project that was never converted, or a
machine with no dvc, must keep behaving exactly as it always has. Those are the
tests that protect the 186 scopes across 25 projects which this migration
deliberately does not touch.

The **property** tests need real dvc and a real bare-repo-plus-worktree layout,
because every property worth having here is a property of the interaction — that
a code merge carries data, that a pin cannot be silently gitignored, that
unlinking a workspace file cannot harm the shared cache. Mocking any of that
would only assert that the mock behaves like the mock.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from awm.dvc import data_dvc as dd
from awm.dvc import scopedata

needs_dvc = pytest.mark.skipif(not dd.dvc_available(), reason="dvc not installed")


def _sh(*args, cwd=None) -> str:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    return (r.stdout or "") + (r.stderr or "")


def _git(repo: Path, *args: str) -> str:
    return _sh("git", "-C", str(repo), *args)


@pytest.fixture()
def dvc_project(dvc_workspace, tmp_path):
    """A bare repo + one worktree, DVC-initialised against a shared cache.

    Deliberately the real awm layout — a *secondary* worktree off a bare repo,
    where `.git` is a file rather than a directory — because that is the shape
    every hook and cache-path claim here depends on.
    """
    ws = dvc_workspace["workspace"]
    projects = ws / "projects"
    bare = projects / "p" / ".bare"
    bare.parent.mkdir(parents=True, exist_ok=True)
    _sh("git", "init", "-q", "--bare", "-b", "main", str(bare))

    seed = tmp_path / "seed"
    _sh("git", "clone", "-q", str(bare), str(seed))
    _git(seed, "config", "user.email", "t@t")
    _git(seed, "config", "user.name", "t")
    (seed / "README.md").write_text("seed\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "seed")
    _git(seed, "push", "-q", "origin", "main")

    wt = projects / "p" / "main"
    _sh("git", "-C", str(bare), "worktree", "add", "-q", str(wt), "main")
    _git(wt, "config", "user.email", "t@t")
    _git(wt, "config", "user.name", "t")
    if dd.dvc_available():
        _sh(dd.dvc_bin(), "init", "-q", cwd=str(wt))
        _git(wt, "add", "-A")
        _git(wt, "commit", "-q", "-m", "dvc init")
    return {"bare": bare, "worktree": wt, "workspace": ws}


# ---------------------------------------------------------------------------
# Degradation — the 186 scopes this migration does not touch
# ---------------------------------------------------------------------------

class TestDegradation:
    def test_unconverted_project_keeps_the_shared_symlink(self, dvc_workspace):
        """No `.dvc/` in the checkout means nothing changes for that project."""
        ws = dvc_workspace["workspace"]
        wt = ws / "projects" / "plain" / "main"
        (wt / ".awm").mkdir(parents=True)
        rep = dd.provision_scope_data("plain", "main", wt / ".awm")
        assert rep["mode"] == "symlink"
        assert (wt / ".awm" / "data").is_symlink()
        assert Path(os.readlink(str(wt / ".awm" / "data"))) == ws / "data" / "plain"

    def test_missing_dvc_binary_degrades_rather_than_failing(
        self, dvc_workspace, monkeypatch):
        """Scope creation must never break because a tool is absent."""
        monkeypatch.setattr(dd, "dvc_available", lambda: False)
        ws = dvc_workspace["workspace"]
        wt = ws / "projects" / "p" / "s"
        (wt / ".awm").mkdir(parents=True)
        rep = dd.provision_scope_data("p", "s", wt / ".awm")
        assert rep["mode"] == "symlink"
        assert "dvc not found" in rep["detail"]

    def test_relative_awm_dir_is_refused_not_normalised(self, dvc_workspace):
        """There is no correct anchor to normalise against, only a wrong one."""
        rep = dd.provision_scope_data("p", "s", Path(".awm"))
        assert rep["mode"] == "unknown"
        assert "relative" in rep["detail"]


# ---------------------------------------------------------------------------
# The gates — silent failures, which is why they are worth a hard refusal
# ---------------------------------------------------------------------------

@needs_dvc
class TestGates:
    def test_gitignored_pin_is_refused(self, dvc_project):
        """The failure this catches is SILENT, which is what makes it the worst.

        With `/data` in .gitignore -- as scadc, awm and avarice all have today --
        `dvc add` writes the pin, `git add -A` skips it, `git status` is clean,
        and the commit records no data at all. Nothing errors.
        """
        wt = dvc_project["worktree"]
        (wt / ".gitignore").write_text("/data\n/data/**/*\n")
        _git(wt, "add", "-A"); _git(wt, "commit", "-q", "-m", "ignore data")

        assert dd.pin_would_be_ignored(wt) is not None
        (wt / ".awm").mkdir(exist_ok=True)
        rep = dd.provision_scope_data("p", "main", wt / ".awm")
        assert rep["mode"] == "unknown"
        assert "silently never committed" in rep["detail"]

    def test_preexisting_data_symlink_is_refused_not_deleted(self, dvc_project):
        """Several worktrees already have a hand-rolled `data` symlink."""
        wt = dvc_project["worktree"]
        (wt / "data").symlink_to(dvc_project["workspace"] / "data")
        assert dd.data_path_conflict(wt) is not None
        (wt / ".awm").mkdir(exist_ok=True)
        rep = dd.provision_scope_data("p", "main", wt / ".awm")
        assert rep["mode"] == "unknown"
        assert (wt / "data").is_symlink(), "must not delete what a human put there"

    def test_a_refused_compat_symlink_is_reported_not_papered_over(self, dvc_project):
        """The refusal must never be reported as a successful link.

        A scope with a real directory left at `.awm/data` reads *that* through
        every call site naming the path, while its code expects `../data`.
        Nothing raises. If provisioning answers
        `mode: dvc, checkout: ok, compat_symlink: <path>` the migration looks
        done and is not -- so the caller has to be told.
        """
        wt = dvc_project["worktree"]
        awm = wt / ".awm"
        awm.mkdir(exist_ok=True)
        # somebody's real directory sitting where the compat symlink goes
        (awm / "data").mkdir()
        (awm / "data" / "leftover.txt").write_text("someone put this here\n")

        rep = dd.provision_scope_data("p", "main", awm)
        assert rep["compat_symlink"] == "refused:real-directory"
        assert (awm / "data" / "leftover.txt").exists(), "must not delete it either"

        (awm / "data" / "leftover.txt").unlink()
        (awm / "data").rmdir()
        rep = dd.provision_scope_data("p", "main", awm)
        assert rep["compat_symlink"] == str(awm / "data")
        assert os.readlink(str(awm / "data")) == "../data"


# ---------------------------------------------------------------------------
# The properties the layer exists for
# ---------------------------------------------------------------------------

@needs_dvc
class TestOneLever:
    def test_a_code_merge_carries_the_data(self, dvc_project):
        """AC 1: no `dvc` command typed, and an untouched chunk stays untouched."""
        wt = dvc_project["worktree"]
        bare = dvc_project["bare"]
        (wt / ".awm").mkdir(exist_ok=True)
        dd.provision_scope_data("p", "main", wt / ".awm")

        for chunk in ("alpha", "beta"):
            (wt / "data" / chunk).mkdir(parents=True)
            (wt / "data" / chunk / "f.txt").write_text(f"{chunk} v1\n")
            _sh(dd.dvc_bin(), "add", f"data/{chunk}", "-q", cwd=str(wt))
        _git(wt, "add", "-A"); _git(wt, "commit", "-q", "-m", "two chunks")
        beta_pin_before = (wt / "data" / "beta.dvc").read_text()

        # A sibling scope changes ONLY alpha.
        wt2 = dvc_project["workspace"] / "projects" / "p" / "s2"
        _sh("git", "-C", str(bare), "worktree", "add", "-q", str(wt2), "-b", "feat/s2", "main")
        _git(wt2, "config", "user.email", "t@t"); _git(wt2, "config", "user.name", "t")
        (wt2 / ".awm").mkdir(parents=True, exist_ok=True)
        dd.provision_scope_data("p", "s2", wt2 / ".awm")
        _sh(dd.dvc_bin(), "unprotect", "data/alpha", "-q", cwd=str(wt2))
        (wt2 / "data" / "alpha" / "g.txt").write_text("added by s2\n")
        _sh(dd.dvc_bin(), "add", "data/alpha", "-q", cwd=str(wt2))
        _git(wt2, "add", "-A"); _git(wt2, "commit", "-q", "-m", "s2 adds g.txt")

        # The whole claim: one `git merge`, no dvc command.
        _git(wt, "merge", "--no-edit", "feat/s2")

        assert (wt / "data" / "alpha" / "g.txt").exists(), \
            "post-merge hook did not materialise the merged-in data"
        assert (wt / "data" / "alpha" / "f.txt").exists()
        assert (wt / "data" / "beta.dvc").read_text() == beta_pin_before, \
            "AC 2: a chunk neither branch touched must not move"

    def test_two_scopes_share_one_physical_copy(self, dvc_project):
        """AC 7: N scopes holding a chunk cost one copy, verified by inode."""
        wt = dvc_project["worktree"]
        (wt / ".awm").mkdir(exist_ok=True)
        dd.provision_scope_data("p", "main", wt / ".awm")
        (wt / "data" / "c").mkdir(parents=True)
        (wt / "data" / "c" / "f.bin").write_text("x" * 5000)
        _sh(dd.dvc_bin(), "add", "data/c", "-q", cwd=str(wt))
        _git(wt, "add", "-A"); _git(wt, "commit", "-q", "-m", "chunk")

        wt2 = dvc_project["workspace"] / "projects" / "p" / "s2"
        _sh("git", "-C", str(dvc_project["bare"]), "worktree", "add", "-q",
            str(wt2), "-b", "feat/s2", "main")
        (wt2 / ".awm").mkdir(parents=True, exist_ok=True)
        dd.provision_scope_data("p", "s2", wt2 / ".awm")

        a = (wt / "data" / "c" / "f.bin").stat().st_ino
        b = (wt2 / "data" / "c" / "f.bin").stat().st_ino
        assert a == b, "two scopes must share one inode, not two copies"

    def test_unmounted_chunks_are_pinned_but_not_materialised(self, dvc_project):
        """The cold-chunk property: pinned and backed up, never on disk.

        A bare `dvc checkout` materialises EVERY pin, so without an explicit
        mount list a scope that merely merges would drag in every cold archive
        the project has ever tracked.
        """
        wt = dvc_project["worktree"]
        (wt / ".awm").mkdir(exist_ok=True)
        dd.provision_scope_data("p", "main", wt / ".awm")
        for chunk in ("hot", "cold"):
            (wt / "data" / chunk).mkdir(parents=True)
            (wt / "data" / chunk / "f.txt").write_text(f"{chunk}\n")
            _sh(dd.dvc_bin(), "add", f"data/{chunk}", "-q", cwd=str(wt))
        _git(wt, "add", "-A"); _git(wt, "commit", "-q", "-m", "hot+cold")

        dd.write_mounts(wt / ".awm", ["data/hot"])
        _sh("rm", "-rf", str(wt / "data" / "cold"))
        rep = dd.provision_scope_data("p", "main", wt / ".awm")

        assert rep["checkout"] == "ok"
        assert (wt / "data" / "hot" / "f.txt").exists()
        assert not (wt / "data" / "cold").exists(), \
            "an unmounted chunk must stay off disk"
        assert "data/cold.dvc" in dd.chunk_pins(wt), \
            "...while remaining pinned by the commit"

    def test_an_empty_mount_list_means_nothing_not_everything(self, dvc_project):
        """The two must not collapse, or opting out of every chunk opts you in.

        An ABSENT list means "materialise everything" (the right default for a
        scope that has not thought about it). A list that exists and selects
        nothing means exactly that.
        """
        wt = dvc_project["worktree"]
        (wt / ".awm").mkdir(exist_ok=True)
        dd.provision_scope_data("p", "main", wt / ".awm")
        (wt / "data" / "c").mkdir(parents=True)
        (wt / "data" / "c" / "f.txt").write_text("v1\n")
        _sh(dd.dvc_bin(), "add", "data/c", "-q", cwd=str(wt))
        _git(wt, "add", "-A"); _git(wt, "commit", "-q", "-m", "chunk")

        dd.write_mounts(wt / ".awm", [])
        assert dd.read_mounts(wt / ".awm") == []
        _sh("rm", "-rf", str(wt / "data" / "c"))
        rep = dd.provision_scope_data("p", "main", wt / ".awm")

        assert rep["checkout"] == "skipped"
        assert not (wt / "data" / "c").exists()

        # ...and removing the list restores "everything".
        (wt / ".awm" / dd.MOUNTS_FILE).unlink()
        rep = dd.provision_scope_data("p", "main", wt / ".awm")
        assert rep["checkout"] == "ok"
        assert (wt / "data" / "c" / "f.txt").exists()


@needs_dvc
class TestCacheSafety:
    def test_chmod_dirs_writable_never_unprotects_a_cache_object(self, dvc_project):
        """A materialised file shares its inode with the SHARED cache object.

        chmod +w on it would strip write protection from content every other
        scope, project and historical commit reads through — so the teardown
        helper must touch directories only.
        """
        wt = dvc_project["worktree"]
        (wt / ".awm").mkdir(exist_ok=True)
        dd.provision_scope_data("p", "main", wt / ".awm")
        (wt / "data" / "c").mkdir(parents=True)
        f = wt / "data" / "c" / "f.bin"
        f.write_text("y" * 5000)
        _sh(dd.dvc_bin(), "add", "data/c", "-q", cwd=str(wt))

        before = f.stat().st_mode
        dd.chmod_dirs_writable(wt)
        assert f.stat().st_mode == before, \
            "file mode changed — this would unprotect the shared cache object"
        assert os.access(wt / "data" / "c", os.W_OK), \
            "directories must be writable so teardown can complete"

    def test_deleting_a_worktree_cannot_harm_the_cache(self, dvc_project):
        """Unlinking one name of a hardlinked inode never touches the object."""
        import shutil
        wt = dvc_project["worktree"]
        (wt / ".awm").mkdir(exist_ok=True)
        dd.provision_scope_data("p", "main", wt / ".awm")
        (wt / "data" / "c").mkdir(parents=True)
        (wt / "data" / "c" / "f.bin").write_text("z" * 5000)
        _sh(dd.dvc_bin(), "add", "data/c", "-q", cwd=str(wt))
        ino = (wt / "data" / "c" / "f.bin").stat().st_ino

        dd.chmod_dirs_writable(wt / "data")
        shutil.rmtree(wt / "data")

        found = [p for p in dd.cache_dir().rglob("*")
                 if p.is_file() and p.stat().st_ino == ino]
        assert found, "the cache object must survive the workspace deletion"
        assert found[0].read_text() == "z" * 5000


@needs_dvc
class TestWiring:
    def test_hooks_land_in_the_common_dir_and_are_shared(self, dvc_project):
        wt = dvc_project["worktree"]
        rep = dd.ensure_repo_wiring(wt)
        assert rep["result"] == "ok"
        common = Path(rep["git_common_dir"])
        for name in ("post-merge", "post-commit"):
            hook = common / "hooks" / name
            assert hook.is_file() and os.access(hook, os.X_OK)
            assert dd.dvc_bin() in hook.read_text(), \
                "hooks run under a minimal PATH; the dvc path must be absolute"
        assert "*.dvc merge=dvc" in (common / "info" / "attributes").read_text()

    def test_a_foreign_hook_is_never_clobbered(self, dvc_project):
        wt = dvc_project["worktree"]
        common = Path(dd._common_git_dir(wt))
        (common / "hooks").mkdir(parents=True, exist_ok=True)
        mine = common / "hooks" / "post-merge"
        mine.write_text("#!/bin/sh\necho someone elses hook\n")
        rep = dd.ensure_repo_wiring(wt)
        assert "conflict" in rep["actions"].get("post-merge_hook", "")
        assert "someone elses hook" in mine.read_text()

    def test_cache_config_is_absolute_and_untracked(self, dvc_project):
        """A tracked relative path silently builds a second cache elsewhere."""
        wt = dvc_project["worktree"]
        dd.ensure_cache_config(wt)
        local = wt / ".dvc" / "config.local"
        assert local.is_file()
        assert str(dd.cache_dir()) in local.read_text()
        assert Path(str(dd.cache_dir())).is_absolute()
        # DVC gitignores config.local itself, which is why it is the right home.
        assert _git(wt, "check-ignore", ".dvc/config.local").strip() != ""


def _scope(dvc_project, name: str, branch: str | None = None) -> Path:
    wt = dvc_project["workspace"] / "projects" / "p" / name
    _sh("git", "-C", str(dvc_project["bare"]), "worktree", "add", "-q", str(wt),
        "-b", branch or f"feat/{name}", "main")
    _git(wt, "config", "user.email", "t@t"); _git(wt, "config", "user.name", "t")
    (wt / ".awm").mkdir(parents=True, exist_ok=True)
    return wt


@pytest.fixture()
def shared_project(dvc_project):
    """main pins a big chunk declared shared and a small one that is not."""
    wt = dvc_project["worktree"]
    (wt / ".awm").mkdir(exist_ok=True)
    dd.provision_scope_data("p", "main", wt / ".awm")
    for chunk in ("ref/big", "small"):
        (wt / "data" / chunk).mkdir(parents=True)
        (wt / "data" / chunk / "f.txt").write_text(f"{chunk} v1\n")
        _sh(dd.dvc_bin(), "add", f"data/{chunk}", "-q", cwd=str(wt))
    (wt / "data" / dd.SHARED_DECLARATION).write_text("# shared\nref/big\n")
    _git(wt, "add", "-A"); _git(wt, "commit", "-q", "-m", "chunks")
    yield dvc_project
    dd.chmod_dirs_writable(_store(dvc_project))


def _drop_link(wt: Path) -> None:
    """The post-checkout hook links a new worktree when a shared copy exists."""
    big = wt / "data" / "ref" / "big"
    if big.is_symlink():
        big.unlink()


def _store(dvc_project) -> Path:
    return dvc_project["workspace"] / "projects" / "p" / "data"


@needs_dvc
class TestSharedChunks:
    def test_scopes_link_one_shared_copy(self, shared_project):
        a = _scope(shared_project, "a")
        b = _scope(shared_project, "g/b")
        ra = dd.provision_scope_data("p", "a", a / ".awm")
        rb = dd.provision_scope_data("p", "g/b", b / ".awm")

        md5 = dd.pin_md5(a / "data" / "ref" / "big.dvc")
        copy = _store(shared_project) / "ref" / "big" / md5
        assert ra["shared"] == {"data/ref/big": "linked"} == rb["shared"]
        for wt in (a, b):
            assert (wt / "data" / "ref" / "big").is_symlink()
            assert (wt / "data" / "ref" / "big").resolve() == copy.resolve()
            assert (wt / "data" / "small" / "f.txt").is_file()
            assert not (wt / "data" / "small").is_symlink()
        assert ((a / "data/ref/big/f.txt").stat().st_ino
                == (b / "data/ref/big/f.txt").stat().st_ino)

    def test_no_declaration_keeps_private_checkouts(self, dvc_project):
        wt = dvc_project["worktree"]
        (wt / ".awm").mkdir(exist_ok=True)
        dd.provision_scope_data("p", "main", wt / ".awm")
        (wt / "data" / "c").mkdir(parents=True)
        (wt / "data" / "c" / "f.txt").write_text("v1\n")
        _sh(dd.dvc_bin(), "add", "data/c", "-q", cwd=str(wt))
        _git(wt, "add", "-A"); _git(wt, "commit", "-q", "-m", "chunk")
        s = _scope(dvc_project, "s")
        rep = dd.provision_scope_data("p", "s", s / ".awm")
        assert "shared" not in rep
        assert not (s / "data" / "c").is_symlink()
        assert not _store(dvc_project).exists()

    def test_a_mount_list_can_leave_a_shared_chunk_out(self, shared_project):
        s = _scope(shared_project, "s")
        dd.write_mounts(s / ".awm", ["data/small"])
        rep = dd.provision_scope_data("p", "s", s / ".awm")
        assert rep["shared"] == {}
        assert not (s / "data" / "ref" / "big").exists()
        assert (s / "data" / "small" / "f.txt").is_file()

    def test_a_merge_repoints_the_link_or_leaves_the_sentinel(self, shared_project):
        a = _scope(shared_project, "a")
        b = _scope(shared_project, "b")
        dd.provision_scope_data("p", "a", a / ".awm")
        dd.provision_scope_data("p", "b", b / ".awm")

        # b re-pins the shared chunk from a private tree.
        (b / "data" / "ref" / "big").unlink()
        _sh(dd.dvc_bin(), "checkout", "-q", "data/ref/big", cwd=str(b))
        _sh(dd.dvc_bin(), "unprotect", "data/ref/big", "-q", cwd=str(b))
        (b / "data" / "ref" / "big" / "g.txt").write_text("v2\n")
        _sh(dd.dvc_bin(), "add", "data/ref/big", "-q", cwd=str(b))
        _git(b, "add", "-A"); _git(b, "commit", "-q", "-m", "v2")

        _git(a, "merge", "--no-edit", "feat/b")
        big = a / "data" / "ref" / "big"
        assert not big.exists() and not big.is_symlink(), \
            "no copy for the new md5: drop the link, never build a real tree"
        assert "no shared copy" in (a / ".awm" / dd.CHECKOUT_FAILED_FILE).read_text()

        rep = dd.provision_scope_data("p", "b", b / ".awm")
        assert rep["shared"] == {"data/ref/big": "linked"}, "b's clean tree is adopted"
        md5 = dd.pin_md5(b / "data" / "ref" / "big.dvc")
        assert (_store(shared_project) / "ref" / "big" / md5 / "g.txt").is_file()

        rep = dd.provision_scope_data("p", "a", a / ".awm")
        assert rep["shared"] == {"data/ref/big": "linked"}
        assert (big / "g.txt").is_file()
        assert not (a / ".awm" / dd.CHECKOUT_FAILED_FILE).exists()

    def test_a_merge_repoints_to_an_existing_copy(self, shared_project):
        a = _scope(shared_project, "a")
        dd.provision_scope_data("p", "a", a / ".awm")
        b = _scope(shared_project, "b")
        dd.provision_scope_data("p", "b", b / ".awm")
        (b / "data" / "ref" / "big").unlink()
        _sh(dd.dvc_bin(), "checkout", "-q", "data/ref/big", cwd=str(b))
        _sh(dd.dvc_bin(), "unprotect", "data/ref/big", "-q", cwd=str(b))
        (b / "data" / "ref" / "big" / "g.txt").write_text("v2\n")
        _sh(dd.dvc_bin(), "add", "data/ref/big", "-q", cwd=str(b))
        _git(b, "add", "-A"); _git(b, "commit", "-q", "-m", "v2")
        dd.provision_scope_data("p", "b", b / ".awm")

        _git(a, "merge", "--no-edit", "feat/b")
        assert (a / "data" / "ref" / "big").is_symlink()
        assert (a / "data" / "ref" / "big" / "g.txt").is_file()
        assert not (a / ".awm" / dd.CHECKOUT_FAILED_FILE).exists()

    def test_data_status_reports_links_without_hashing_them(self, shared_project):
        a = _scope(shared_project, "a")
        dd.provision_scope_data("p", "a", a / ".awm")
        st = dd.data_status("p", "a", a)
        assert st["shared"] == {"data/ref/big": "linked"}
        assert st["in_sync"] is True

    def test_heal_replaces_a_clean_private_copy_and_keeps_a_modified_one(self, shared_project):
        a = _scope(shared_project, "a")
        b = _scope(shared_project, "b")
        dd.provision_scope_data("p", "a", a / ".awm")
        # b predates the declaration: a private, clean tree.
        _drop_link(b)
        dd.ensure_cache_config(b)
        _sh(dd.dvc_bin(), "checkout", "-q", "data/ref/big", cwd=str(b))
        assert (b / "data/ref/big/f.txt").is_file()
        assert not (b / "data" / "ref" / "big").is_symlink()
        rep = dd.provision_scope_data("p", "b", b / ".awm")
        assert rep["shared"] == {"data/ref/big": "linked"}
        assert (b / "data" / "ref" / "big").is_symlink()

        c = _scope(shared_project, "c")
        _drop_link(c)
        dd.ensure_cache_config(c)
        _sh(dd.dvc_bin(), "checkout", "-q", "data/ref/big", cwd=str(c))
        _sh(dd.dvc_bin(), "unprotect", "data/ref/big", "-q", cwd=str(c))
        (c / "data" / "ref" / "big" / "f.txt").write_text("local edit\n")
        rep = dd.provision_scope_data("p", "c", c / ".awm")
        assert rep["shared"] == {"data/ref/big": "private:modified"}
        assert (c / "data" / "ref" / "big" / "f.txt").read_text() == "local edit\n"

    def test_heal_adopts_a_clean_private_copy_when_no_shared_copy_exists(self, shared_project):
        b = _scope(shared_project, "b")
        dd.ensure_cache_config(b)
        _sh(dd.dvc_bin(), "checkout", "-q", "data/ref/big", cwd=str(b))
        ino = (b / "data/ref/big/f.txt").stat().st_ino
        rep = dd.provision_scope_data("p", "b", b / ".awm")
        assert rep["shared"] == {"data/ref/big": "linked"}
        md5 = dd.pin_md5(b / "data" / "ref" / "big.dvc")
        moved = _store(shared_project) / "ref" / "big" / md5 / "f.txt"
        assert moved.stat().st_ino == ino, "adopted by rename, not re-linked"

    def test_a_bare_dvc_checkout_never_empties_a_shared_copy(self, shared_project):
        """The hook never runs one, but a person might, over a link to another md5."""
        a = _scope(shared_project, "a")
        dd.provision_scope_data("p", "a", a / ".awm")
        v1 = dd.pin_md5(a / "data" / "ref" / "big.dvc")
        b = _scope(shared_project, "b")
        dd.provision_scope_data("p", "b", b / ".awm")
        (b / "data" / "ref" / "big").unlink()
        _sh(dd.dvc_bin(), "checkout", "-q", "data/ref/big", cwd=str(b))
        _sh(dd.dvc_bin(), "unprotect", "data/ref/big", "-q", cwd=str(b))
        (b / "data" / "ref" / "big" / "g.txt").write_text("v2\n")
        _sh(dd.dvc_bin(), "add", "data/ref/big", "-q", cwd=str(b))
        _git(b, "add", "-A"); _git(b, "commit", "-q", "-m", "v2")
        dd.provision_scope_data("p", "b", b / ".awm")
        v2 = dd.pin_md5(b / "data" / "ref" / "big.dvc")

        _git(a, "merge", "--no-edit", "feat/b")
        _git(a, "checkout", "HEAD~1", "--", "data/ref/big.dvc")
        _sh(dd.dvc_bin(), "checkout", "-f", cwd=str(a))

        store = _store(shared_project) / "ref" / "big"
        assert (store / v1 / "f.txt").read_text() == "ref/big v1\n"
        assert (store / v2 / "f.txt").read_text() == "ref/big v1\n"
        assert (store / v2 / "g.txt").read_text() == "v2\n"

        # DVC answers the refused unlink by making the cache object writable.
        dd.provision_scope_data("p", "b", b / ".awm")
        assert not os.access(store / v2 / "g.txt", os.W_OK), "heal re-protects it"

        # A branch switch repoints the link with no merge involved.
        _git(a, "checkout", "-q", "--", "data/ref/big.dvc")
        assert (a / "data" / "ref" / "big").resolve() == (store / v2).resolve()
        _git(a, "checkout", "-q", "-b", "sw", "main")
        assert (a / "data" / "ref" / "big").resolve() == (store / v1).resolve()

    def test_deleting_a_scope_leaves_the_shared_copy(self, shared_project):
        import shutil
        a = _scope(shared_project, "a")
        dd.provision_scope_data("p", "a", a / ".awm")
        copy = (a / "data" / "ref" / "big").resolve()
        dd.chmod_dirs_writable(a)
        shutil.rmtree(a)
        assert (copy / "f.txt").read_text() == "ref/big v1\n"
        assert not os.access(copy / "f.txt", os.W_OK), "cache objects stay read-only"


# ---------------------------------------------------------------------------
# Garbage collection
# ---------------------------------------------------------------------------

def _dvc_in(wt: Path, *args: str) -> str:
    return _sh(dd.dvc_bin(), *args, cwd=str(wt))


def _add_chunk(wt: Path, name: str, content: str) -> str:
    chunk = wt / "data" / name
    chunk.mkdir(parents=True, exist_ok=True)
    (chunk / "f.txt").write_text(content)
    _dvc_in(wt, "add", "-q", f"data/{name}")
    return yaml.safe_load((wt / "data" / f"{name}.dvc").read_text())["outs"][0]["md5"]


def _objects(cache: Path) -> set[str]:
    return {p.parent.name + p.name for p in (cache / "files" / "md5").glob("*/*")}


def _closure(cache: Path, oid: str) -> set[str]:
    """A ``.dir`` object plus every file its manifest lists."""
    entries = json.loads(dd._object_path(cache, oid).read_text())
    return {oid, *(e["md5"] for e in entries)}


@pytest.fixture()
def gc_layout(dvc_project):
    """Two worktrees whose pins reach the cache by every route gc must honour."""
    wt, bare = dvc_project["worktree"], dvc_project["bare"]
    (wt / ".awm").mkdir(exist_ok=True)
    dd.provision_scope_data("p", "main", wt / ".awm")
    h: dict[str, str] = {}

    h["history"] = _add_chunk(wt, "old", "old\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "add old")
    _dvc_in(wt, "remove", "-q", "data/old.dvc")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "drop old")

    _git(wt, "branch", "side")
    _git(wt, "commit", "-q", "--allow-empty", "-m", "main moves")
    _git(wt, "checkout", "-q", "side")
    _git(wt, "commit", "-q", "--allow-empty", "-m", "side moves")
    _git(wt, "checkout", "-q", "main")
    _git(wt, "merge", "-q", "--no-ff", "--no-commit", "side")
    h["merge"] = _add_chunk(wt, "resolved", "resolved\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "merge side")
    _dvc_in(wt, "remove", "-q", "data/resolved.dvc")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "drop resolved")

    _dvc_in(wt, "stage", "add", "-q", "-n", "gen", "-o", "data/gen",
            "mkdir -p data/gen && echo gen > data/gen/x")
    _dvc_in(wt, "repro", "-q")
    lock = yaml.safe_load((wt / "dvc.lock").read_text())
    h["lock"] = lock["stages"]["gen"]["outs"][0]["md5"]
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "stage gen")
    _dvc_in(wt, "remove", "-q", "gen")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "drop gen")

    h["staged"] = _add_chunk(wt, "staged", "staged\n")
    _git(wt, "add", "data/staged.dvc")

    wt2 = bare.parent / "other"
    _git(bare, "worktree", "add", "-q", "-b", "other", str(wt2), "main")
    dd.ensure_cache_config(wt2)
    h["untracked"] = _add_chunk(wt2, "untracked", "untracked\n")

    h["garbage"] = _add_chunk(wt, "junk", "junk\n")
    (wt / "data" / "junk.dvc").unlink()

    project = dd.GcProject("p", bare, [wt, wt2])
    return {"project": project, "wt": wt, "wt2": wt2, "hashes": h,
            "cache": dd.cache_dir()}


ROUTES = ("history", "merge", "lock", "staged", "untracked")


@needs_dvc
class TestGcKeepSet:
    def test_every_route_to_a_pin_is_kept(self, gc_layout):
        cache, h = gc_layout["cache"], gc_layout["hashes"]
        keep, info = dd.project_keep_set(gc_layout["project"], cache)
        absent = [r for r in ROUTES if not dd._object_path(cache, h[r]).exists()]
        assert not absent, f"fixture left no manifest for {absent}: {h}"
        for route in ROUTES:
            assert _closure(cache, h[route]) <= keep, f"{route} pin not kept"
        assert not _closure(cache, h["garbage"]) <= keep
        assert info["missing_dir_manifests"] == {}

    def test_never_collects_what_dvc_gc_keeps(self, gc_layout):
        """Run DVC's own collection on the test cache and compare."""
        cache, wt, wt2 = gc_layout["cache"], gc_layout["wt"], gc_layout["wt2"]
        keep, _ = dd.project_keep_set(gc_layout["project"], cache)
        before = _objects(cache)
        ours = before - keep
        _dvc_in(wt, "gc", "--all-commits", "-f", "-p", str(wt), "-p", str(wt2))
        dvc_collected = before - _objects(cache)
        assert ours, "the fixture holds garbage, so there must be candidates"
        assert ours <= dvc_collected, f"would delete what DVC keeps: {ours - dvc_collected}"

    def test_an_unparseable_pin_aborts(self, gc_layout):
        (gc_layout["wt2"] / "data" / "broken.dvc").write_text("outs: [\n")
        with pytest.raises(dd.GcRefused, match="broken.dvc"):
            dd.project_keep_set(gc_layout["project"], gc_layout["cache"])


@needs_dvc
class TestGcSweep:
    def _gc(self, gc_layout, **kw):
        kw.setdefault("wired", {"p"})
        return dd.collect_garbage([gc_layout["project"]], **kw)

    def test_grace_window_protects_a_young_object(self, gc_layout):
        cache, h = gc_layout["cache"], gc_layout["hashes"]
        rep = self._gc(gc_layout, dry_run=False)
        assert rep["result"] == "ok", rep
        assert rep["sweep"]["collected"] == 0
        assert rep["sweep"]["grace_protected"] >= 1
        assert _closure(cache, h["garbage"]) <= _objects(cache)

    def test_collects_garbage_and_nothing_else(self, gc_layout):
        cache, h = gc_layout["cache"], gc_layout["hashes"]
        dry = self._gc(gc_layout, grace_days=0)
        assert dry["result"] == "ok" and dry["dry_run"] is True
        assert dry["sweep"]["collected"] >= 1 and not dry["deleted_anything"]
        assert h["garbage"] in _objects(cache), "a dry run deleted something"

        rep = self._gc(gc_layout, dry_run=False, grace_days=0)
        assert rep["result"] == "ok" and rep["deleted_anything"]
        left = _objects(cache)
        assert h["garbage"] not in left
        for route in ROUTES:
            assert _closure(cache, h[route]) <= left, f"{route} pin collected"

    def test_refuses_an_unlisted_wired_project(self, gc_layout):
        rep = self._gc(gc_layout, wired={"p", "q"}, grace_days=0)
        assert rep["result"] == "refused" and "q" in rep["detail"]
        assert self._gc(gc_layout, wired={"p", "q"}, exclude=["q"])["result"] == "ok"

    def test_refuses_a_missing_dir_manifest(self, gc_layout):
        cache, h = gc_layout["cache"], gc_layout["hashes"]
        dd._object_path(cache, h["history"]).unlink()
        before = _objects(cache)
        rep = self._gc(gc_layout, dry_run=False, grace_days=0)
        assert rep["result"] == "refused" and h["history"] in rep["detail"]
        assert "data/old.dvc" in rep["detail"]
        assert _objects(cache) == before
        rep = self._gc(gc_layout, accept_missing=[h["history"]])
        assert rep["result"] == "ok", rep

    def test_refuses_while_another_gc_runs(self, gc_layout):
        lock = dd._acquire_gc_lock(gc_layout["cache"])
        try:
            rep = self._gc(gc_layout, dry_run=False, grace_days=0)
        finally:
            lock.close()
        assert rep["result"] == "refused" and f"pid {os.getpid()}" in rep["detail"]

    def test_collects_an_abandoned_transfer(self, gc_layout):
        partial = gc_layout["cache"] / "files" / "md5" / ".abc123.tmp"
        partial.write_text("partial")
        rep = self._gc(gc_layout, dry_run=False, grace_days=0)
        assert rep["result"] == "ok", rep
        assert not partial.exists()

    def test_refuses_an_unknown_cache_layout(self, gc_layout):
        (gc_layout["cache"] / "runs").mkdir()
        rep = self._gc(gc_layout, dry_run=False, grace_days=0)
        assert rep["result"] == "refused" and "runs" in rep["detail"]

    def test_data_gc_finds_the_wired_project(self, gc_layout):
        assert scopedata.gc([])["result"] == "refused"
        rep = scopedata.gc(["p"])
        assert rep["result"] == "ok", rep
        assert rep["projects"]["p"]["worktrees"] == 2
