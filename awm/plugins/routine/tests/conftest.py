"""Fixtures: a temp store, a fake outbound and a git helper."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from awm.routine.store import Store


class FakeOutbound:
    def __init__(self) -> None:
        self.starts: list[dict[str, Any]] = []
        self.dms: list[str] = []
        self.posts: list[tuple[str, str, str]] = []
        self.calls: list[tuple[str, str, dict, str | None]] = []
        self.fail_start = False
        self.fail_dm = False
        self.invoke_results: dict[tuple[str, str, str | None], Any] = {}
        self.cards: dict[int, str] = {}
        self.card_posts: list[tuple[str, str]] = []

    async def start_session(self, **kw: Any) -> dict[str, Any]:
        if self.fail_start:
            raise RuntimeError("peer unreachable")
        self.starts.append(kw)
        return {"job": f"job{len(self.starts)}"}

    async def notify_operator(self, text: str) -> None:
        if self.fail_dm:
            raise RuntimeError("discord down")
        self.dms.append(text)

    async def journal(self, project: str, scope: str, body: str) -> None:
        self.posts.append((project, scope, body))

    async def card_of(self, pid: int) -> str:
        return self.cards.get(pid, "")

    async def card_post(self, card: str, body: str) -> None:
        self.card_posts.append((card, body))

    async def invoke(self, tool: str, verb: str, args: dict, *, peer: str | None = None,
                     timeout: float = 60.0) -> Any:
        self.calls.append((tool, verb, args, peer))
        res = self.invoke_results.get((tool, verb, peer))
        if isinstance(res, Exception):
            raise res
        return res if res is not None else {"ok": True}


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "routine.db")
    yield s
    s.close()


@pytest.fixture()
def out() -> FakeOutbound:
    return FakeOutbound()


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t",
                           "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def project_repo(tmp_path: Path) -> Path:
    """A git checkout with a routines/ folder, standing in for a project repo."""
    repo = tmp_path / "proj"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "routines").mkdir()
    (repo / "README").write_text("x\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def write_routine(repo: Path, name: str, body: str) -> Path:
    p = repo / "routines" / f"{name}.toml"
    p.write_text(body)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", f"routine {name}")
    return p


class Workspace:
    """A temp workspace with projects/proj/.bare, dev and release, scope worktrees
    and a live release checkout, standing in for a node."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.proj = root / "projects" / "proj"
        self.bare = self.proj / ".bare"
        seed = root / "seed"
        seed.mkdir(parents=True)
        git(seed, "init", "-q", "-b", "dev")
        (seed / "app.txt").write_text("base\n")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "base")
        git(seed, "branch", "release")
        self.proj.mkdir(parents=True)
        subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(self.bare)],
                       check=True, capture_output=True)
        git(self.bare, "worktree", "add", "-q", str(self.proj / "release"), "release")

    def scope(self, name: str, files: dict[str, str], base: str = "dev") -> Path:
        wt = self.proj / name
        if not wt.exists():
            git(self.bare, "worktree", "add", "-q", "-b", f"feat/{name}", str(wt), base)
            (wt / ".awm").mkdir()
        for rel, text in files.items():
            (wt / rel).parent.mkdir(parents=True, exist_ok=True)
            (wt / rel).write_text(text)
        git(wt, "add", "-A", "--", *files)
        git(wt, "commit", "-q", "-m", f"{name}: change")
        return wt

    def rev(self, ref: str) -> str:
        return git(self.bare, "rev-parse", ref)

    def backup(self, name: str = "github") -> Path:
        """A bare repo standing in for GitHub, a remote of the canonical repo that
        holds release as it is now."""
        remote = self.root / f"{name}.git"
        git(self.root, "init", "-q", "--bare", str(remote))
        git(self.bare, "remote", "add", name, str(remote))
        git(self.bare, "push", "-q", name, "release")
        return remote

    def show(self, ref: str, path: str) -> str:
        return git(self.bare, "show", f"{ref}:{path}")


@pytest.fixture()
def ws(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path / "ws")
