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

import os
import subprocess
from pathlib import Path

import pytest

from awm.scopes import data_dvc as dd

needs_dvc = pytest.mark.skipif(not dd.dvc_available(), reason="dvc not installed")


def _sh(*args, cwd=None) -> str:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    return (r.stdout or "") + (r.stderr or "")


def _git(repo: Path, *args: str) -> str:
    return _sh("git", "-C", str(repo), *args)


@pytest.fixture()
def dvc_project(scopes_workspace, tmp_path):
    """A bare repo + one worktree, DVC-initialised against a shared cache.

    Deliberately the real awm layout — a *secondary* worktree off a bare repo,
    where `.git` is a file rather than a directory — because that is the shape
    every hook and cache-path claim here depends on.
    """
    ws = scopes_workspace["workspace"]
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
    def test_unconverted_project_keeps_the_shared_symlink(self, scopes_workspace):
        """No `.dvc/` in the checkout means nothing changes for that project."""
        ws = scopes_workspace["workspace"]
        wt = ws / "projects" / "plain" / "main"
        (wt / ".awm").mkdir(parents=True)
        rep = dd.provision_scope_data("plain", "main", wt / ".awm")
        assert rep["mode"] == "symlink"
        assert (wt / ".awm" / "data").is_symlink()
        assert Path(os.readlink(str(wt / ".awm" / "data"))) == ws / "data" / "plain"

    def test_missing_dvc_binary_degrades_rather_than_failing(
        self, scopes_workspace, monkeypatch):
        """Scope creation must never break because a tool is absent."""
        monkeypatch.setattr(dd, "dvc_available", lambda: False)
        ws = scopes_workspace["workspace"]
        wt = ws / "projects" / "p" / "s"
        (wt / ".awm").mkdir(parents=True)
        rep = dd.provision_scope_data("p", "s", wt / ".awm")
        assert rep["mode"] == "symlink"
        assert "dvc not found" in rep["detail"]

    def test_relative_awm_dir_is_refused_not_normalised(self, scopes_workspace):
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


# ---------------------------------------------------------------------------
# collect_garbage — the only verb here that can destroy data
# ---------------------------------------------------------------------------

class TestCollectGarbage:
    """Three defects that only compound.

    Each was reproduced against real dvc before these tests existed:
    a repeated ``-p`` kept one repo of sixty, the working directory decided
    which cache got collected, and naming that directory in its own projects
    list deadlocked the run against itself.
    """

    def _repos(self, tmp_path, shared, *, wired: list[str], private: list[str]):
        """Bare DVC repos, some pointed at ``shared`` and some left unprovisioned."""
        made = {}
        for name in [*wired, *private]:
            r = tmp_path / name
            r.mkdir(parents=True)
            _sh("git", "init", "-q", "-b", "main", str(r))
            _git(r, "config", "user.email", "t@t")
            _git(r, "config", "user.name", "t")
            _sh(dd.dvc_bin(), "init", "-q", cwd=str(r))
            if name in wired:
                (r / ".dvc" / "config.local").write_text(
                    f"[cache]\n    dir = {shared}\n")
            _git(r, "add", "-A")
            _git(r, "commit", "-q", "-m", name)
            made[name] = r
        return made

    @needs_dvc
    def test_one_projects_flag_carries_every_path(self, tmp_path, monkeypatch):
        """`-p` is nargs="*", so a repeated flag OVERWRITES.

        Passing one flag per repo kept only the last, and every other named
        worktree silently left the keep set — i.e. became garbage. The captured
        argv is the assertion because the damage is invisible in the result.
        """
        shared = tmp_path / "cache"
        shared.mkdir()
        monkeypatch.setattr(dd, "cache_dir", lambda: shared)
        repos = self._repos(tmp_path, shared, wired=["a", "b", "c"], private=[])

        seen = {}
        real_dvc = dd._dvc

        def fake_dvc(repo, *args, **kw):
            # Intercept only the collection; `cache dir` must stay real, since
            # choosing the working directory depends on its answer.
            if args and args[0] == "gc":
                seen["cwd"] = repo
                seen["args"] = list(args)
                import subprocess as sp
                return sp.CompletedProcess([], 0, "", "")
            return real_dvc(repo, *args, **kw)

        monkeypatch.setattr(dd, "_dvc", fake_dvc)
        dd.collect_garbage([repos["a"], repos["b"], repos["c"]], dry_run=True)

        assert seen["args"].count("-p") == 1, "one flag, or repos silently drop out"
        tail = seen["args"][seen["args"].index("-p") + 1:]
        listed = [t for t in tail if not t.startswith("--")]
        # cwd repo is deliberately absent from -p; the other two must be there.
        assert len(listed) == 2
        assert str(seen["cwd"]) not in listed

    @needs_dvc
    def test_working_directory_repo_is_not_named_in_projects(self, tmp_path, monkeypatch):
        """DVC locks the cwd repo AND every listed repo.

        Naming the cwd repo in its own projects list takes one lock twice and
        aborts with "Unable to acquire lock" — with no other dvc process alive.
        This is a real run, not a stub, because the deadlock is the point.
        """
        shared = tmp_path / "cache"
        shared.mkdir()
        monkeypatch.setattr(dd, "cache_dir", lambda: shared)
        repos = self._repos(tmp_path, shared, wired=["a", "b"], private=[])
        rep = dd.collect_garbage([repos["a"], repos["b"]], dry_run=True)
        assert rep["result"] == "ok", rep.get("output")
        assert "Unable to acquire lock" not in (rep.get("output") or "")

    @needs_dvc
    def test_runs_from_a_worktree_on_the_shared_cache(self, tmp_path, monkeypatch):
        """The cwd decides which cache is collected, so it is chosen, not inherited.

        ``data_gc`` sorts each project's worktrees, so ``repos[0]`` was an
        alphabetical accident. Here the first-sorted repo resolves to a private
        cache; picking it would collect that one and report the shared path.
        """
        shared = tmp_path / "cache"
        shared.mkdir()
        monkeypatch.setattr(dd, "cache_dir", lambda: shared)
        repos = self._repos(tmp_path, shared, wired=["bbb"], private=["aaa"])
        rep = dd.collect_garbage([repos["aaa"], repos["bbb"]], dry_run=True)

        assert rep["cwd"] == str(repos["bbb"]), "must skip the private-cache repo"
        assert rep["cache"] == str(shared)
        # The unwired one is reported, not refused: DVC unions used-object
        # hashes across every named repo, so it still protects its own content.
        assert [e["repo"] for e in rep["resolved_elsewhere"]] == [str(repos["aaa"])]

    @needs_dvc
    def test_refuses_when_nothing_resolves_to_the_shared_cache(self, tmp_path, monkeypatch):
        """A run that cannot reach the shared cache must say so, not report success."""
        shared = tmp_path / "cache"
        shared.mkdir()
        monkeypatch.setattr(dd, "cache_dir", lambda: shared)
        repos = self._repos(tmp_path, shared, wired=[], private=["aaa", "bbb"])
        rep = dd.collect_garbage([repos["aaa"], repos["bbb"]], dry_run=True)

        assert rep["result"] == "refused"
        assert "shared" in rep["detail"]
        assert len(rep["resolved_elsewhere"]) == 2

    @needs_dvc
    def test_resolved_cache_reports_what_dvc_actually_uses(self, tmp_path, monkeypatch):
        """A worktree awm never provisioned resolves to its own cache, silently."""
        shared = tmp_path / "cache"
        shared.mkdir()
        monkeypatch.setattr(dd, "cache_dir", lambda: shared)
        repos = self._repos(tmp_path, shared, wired=["w"], private=["p"])
        assert dd.resolved_cache(repos["w"]).resolve() == shared.resolve()
        assert dd.resolved_cache(repos["p"]).resolve() != shared.resolve()

    def test_still_refuses_an_empty_repo_list(self, scopes_workspace):
        """The plural argument is the guard; an empty set keeps nothing."""
        rep = dd.collect_garbage([], dry_run=True)
        assert rep["result"] in ("refused", "unavailable")
