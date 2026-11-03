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
