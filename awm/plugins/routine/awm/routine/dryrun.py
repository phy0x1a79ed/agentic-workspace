"""Dry-run the deploy engine end to end in a scratch directory: `scripts/deploy-dryrun.sh`.

It builds a throwaway workspace with a bare repo, `dev`, `release` and feature
branches, then lands them through the real engine. Session starts are recorded,
not made. Peer nodes are stand-in refreshers on the same checkout. Nothing
outside the scratch directory is read or written.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from awm.routine.deploy import Deployer
from awm.routine.refresh import Refresher
from awm.routine.store import Store

NODES = ["mizar", "alcor", "altair"]


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=dryrun",
                           "-c", "user.email=dryrun@awm.invalid", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


class Recorder:
    """The outbound side: records sessions and posts, routes peer refreshes to
    stand-in refreshers."""

    def __init__(self) -> None:
        self.starts: list[dict[str, Any]] = []
        self.posts: list[tuple[str, str, str]] = []
        self.peers: dict[str, Refresher] = {}

    async def start_session(self, **kw: Any) -> dict[str, Any]:
        self.starts.append(kw)
        return {"job": f"dry{len(self.starts)}"}

    async def notify_operator(self, text: str) -> None:
        raise RuntimeError("the dry run sends no DM")

    async def journal(self, project: str, scope: str, body: str) -> None:
        self.posts.append((project, scope, body))

    async def invoke(self, tool: str, verb: str, args: dict, *, peer: str | None = None,
                     timeout: float = 60.0) -> Any:
        if (tool, verb) != ("routine", "refresh") or peer not in self.peers:
            raise RuntimeError(f"the dry run has no {tool} {verb} on {peer}")
        r = await self.peers[peer].local(args["project"], args["from"], args["to"])
        return r.to_dict()


def build(root: Path) -> tuple[Path, Path]:
    proj = root / "projects" / "demo"
    seed = root / "seed"
    seed.mkdir(parents=True)
    git(seed, "init", "-q", "-b", "dev")
    log = root / "refresh.log"
    (seed / "app.txt").write_text("base\n")
    (seed / "deploy.toml").write_text(
        '[deploy]\ncheck = "test -f app.txt && ! grep -q BROKEN *.txt"\n'
        f'[refresh]\nkind = "restart"\ncommand = "echo $AWM_NODE_NAME >> {log}"\n'
        f'[fleet]\ncanonical = "mizar"\nnodes = {json.dumps(NODES)}\n')
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "base")
    git(seed, "branch", "release")
    proj.mkdir(parents=True)
    git(root, "clone", "-q", "--bare", str(seed), str(proj / ".bare"))
    git(proj / ".bare", "worktree", "add", "-q", str(proj / "release"), "release")
    return proj, log


def scope(proj: Path, name: str, files: dict[str, str]) -> None:
    wt = proj / name
    git(proj / ".bare", "worktree", "add", "-q", "-b", f"feat/{name}", str(wt), "dev")
    for rel, text in files.items():
        (wt / rel).write_text(text)
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", f"{name}: change")


async def run(root: Path) -> list[str]:
    proj, log = build(root)
    bare = proj / ".bare"
    out = Recorder()

    def refresher(node: str) -> Refresher:
        return Refresher(out, node=node, checkout_root=lambda _p: proj / "release")

    out.peers = {n: refresher(n) for n in NODES[1:]}
    store = Store(root / "routine.db")
    dep = Deployer(store, out, workspace=root, refresher=refresher(NODES[0]))
    problems: list[str] = []

    def expect(ok: bool, what: str) -> None:
        print(("  ok    " if ok else "  FAIL  ") + what)
        if not ok:
            problems.append(what)

    print("case 1: two branches land in one batch")
    scope(proj, "alpha", {"alpha.txt": "alpha\n"})
    scope(proj, "beta", {"beta.txt": "beta\n"})
    rel0 = git(bare, "rev-parse", "release")
    tickets = [dep.submit(dep.resolve(cwd=str(proj / s))) for s in ("alpha", "beta")]
    expect(all(t["state"] == "queued" for t in tickets), "deploy returns queued tickets")
    await dep.drain()
    landed = [store.get_ticket(t["id"]) for t in tickets]
    expect(all(t["state"] == "landed" for t in landed), "both tickets landed")
    expect(landed[0]["batch_id"] == landed[1]["batch_id"], "one batch held both")
    files = git(bare, "ls-tree", "--name-only", "release").split()
    expect({"alpha.txt", "beta.txt"} <= set(files), "release holds both branches")
    expect(git(bare, "rev-parse", "dev") == git(bare, "rev-parse", "release"),
           "dev and release agree")
    expect(log.read_text().split() == NODES, f"refresh ran once per node in order {NODES}")
    expect(not out.starts, "no session started")
    expect(sorted(p[1] for p in out.posts) == ["alpha", "beta"]
           and all(p[2].startswith("✓") for p in out.posts), "each scope got a ✓")
    expect(rel0 != git(bare, "rev-parse", "release"), "release moved")

    print("case 2: a conflict fails the whole batch")
    log.write_text("")
    out.posts.clear()
    scope(proj, "gamma", {"app.txt": "gamma\n"})
    scope(proj, "delta", {"app.txt": "delta\n"})
    rel1 = git(bare, "rev-parse", "release")
    tickets = [dep.submit(dep.resolve(cwd=str(proj / s))) for s in ("gamma", "delta")]
    await dep.drain()
    failed = [store.get_ticket(t["id"]) for t in tickets]
    batch = store.get_batch(failed[0]["batch_id"])
    expect(all(t["state"] == "failed" for t in failed), "both tickets failed")
    expect(batch["failed_step"] == "merge", "the batch failed at merge")
    expect(git(bare, "rev-parse", "release") == rel1, "release did not move")
    expect(len(out.starts) == 1, "one fixing session started")
    expect(bool(out.starts) and "feat/gamma" in out.starts[0]["prompt"]
           and "feat/delta" in out.starts[0]["prompt"], "the session got both branches")
    expect(log.read_text() == "", "no node refreshed")
    expect(all(p[2].startswith("✗ failed at merge") for p in out.posts),
           "each scope got a ✗ naming the step")
    store.close()
    return problems


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m awm.routine.dryrun <empty scratch dir>", file=sys.stderr)
        return 2
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        print(f"{root} is not empty", file=sys.stderr)
        return 2
    problems = asyncio.run(run(root))
    print("dry run passed" if not problems else f"dry run FAILED: {len(problems)} checks")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
