"""The deploy engine: tickets, batches, the six steps, refresh and the failure dispatch."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from awm.routine import deployconf
from awm.routine.deploy import Deployer
from awm.routine.hub_adapter import DEPLOY_FUNCTIONS, DeployVerbs
from awm.routine.refresh import Refresher, plugin_folders

from tests.conftest import git

pytestmark = pytest.mark.smoke


def make(store, out, ws, node="mizar") -> Deployer:
    ref = Refresher(out, node=node, checkout_root=lambda p: ws.proj / "release")
    return Deployer(store, out, workspace=ws.root, refresher=ref)


async def land(dep: Deployer, *scopes: str) -> list[dict]:
    tickets = [dep.submit(dep.resolve(cwd=str(dep.workspace / "projects/proj" / s)))
               for s in scopes]
    await dep.drain()
    return [dep.store.get_ticket(t["id"]) for t in tickets]


# -- submit ----------------------------------------------------------------

def test_resolve_from_caller_cwd(store, out, ws):
    wt = ws.scope("a", {"a.txt": "a\n"})
    (wt / "sub").mkdir()
    sub = make(store, out, ws).resolve(cwd=str(wt / "sub"))
    assert (sub.project, sub.scope, sub.branch) == ("proj", "a", "feat/a")
    assert sub.sha == ws.rev("feat/a")


def test_resolve_refuses_dev_and_unknown(store, out, ws):
    dep = make(store, out, ws)
    with pytest.raises(ValueError, match="calling scope"):
        dep.resolve()
    with pytest.raises(ValueError, match="feature branch"):
        dep.resolve(project="proj", scope="x", branch="dev")
    with pytest.raises(ValueError, match="no branch"):
        dep.resolve(project="proj", scope="ghost")
    with pytest.raises(ValueError, match="Push"):
        dep.resolve(project="proj", scope="ghost", sha="0" * 40)


def test_submit_returns_queued_ticket(store, out, ws):
    ws.scope("a", {"a.txt": "a\n"})
    dep = make(store, out, ws)
    t = dep.submit(dep.resolve(project="proj", scope="a"), caller="job x")
    assert t["state"] == "queued" and t["sha"] == ws.rev("feat/a")
    assert store.list_tickets("proj", "queued")[0]["id"] == t["id"]


# -- landing ---------------------------------------------------------------

async def test_one_batch_lands_every_waiting_ticket(store, out, ws):
    ws.scope("a", {"a.txt": "a\n"})
    ws.scope("b", {"b.txt": "b\n"})
    dep = make(store, out, ws)
    ta, tb = await land(dep, "a", "b")
    assert ta["state"] == tb["state"] == "landed"
    assert ta["batch_id"] == tb["batch_id"]
    assert len(store.list_batches("proj")) == 1
    assert ws.rev("dev") == ws.rev("release")
    assert ws.show("release", "a.txt") == "a" and ws.show("release", "b.txt") == "b"
    assert sorted(p[1] for p in out.posts) == ["a", "b"]
    assert all(p[2].startswith("✓") for p in out.posts)
    # A clean release checkout follows the ref.
    assert (ws.proj / "release" / "b.txt").read_text() == "b\n"
    batch = store.get_batch(ta["batch_id"])
    assert [s["step"] for s in batch["steps"]] == ["pin", "merge", "check", "promote",
                                                    "refresh"]


async def test_dirty_checkout_is_left_alone(store, out, ws):
    (ws.proj / "release" / "app.txt").write_text("local edit\n")
    ws.scope("a", {"a.txt": "a\n"})
    [t] = await land(make(store, out, ws), "a")
    assert t["state"] == "landed"
    assert not (ws.proj / "release" / "a.txt").exists()
    assert (ws.proj / "release" / "app.txt").read_text() == "local edit\n"


async def test_release_ahead_of_dev_is_merged_in(store, out, ws):
    hot = ws.scope("hotfix", {"hot.txt": "h\n"}, base="release")
    git(ws.bare, "update-ref", "refs/heads/release", ws.rev("feat/hotfix"))
    del hot
    ws.scope("a", {"a.txt": "a\n"})
    [t] = await land(make(store, out, ws), "a")
    assert t["state"] == "landed"
    assert ws.show("release", "hot.txt") == "h"


# -- failures ---------------------------------------------------------------

async def test_conflict_dispatches_one_session_with_the_whole_batch(store, out, ws):
    ws.scope("a", {"app.txt": "from a\n"})
    ws.scope("b", {"app.txt": "from b\n"})
    dev0, rel0 = ws.rev("dev"), ws.rev("release")
    ta, tb = await land(make(store, out, ws), "a", "b")
    assert ta["state"] == tb["state"] == "failed"
    assert (ws.rev("dev"), ws.rev("release")) == (dev0, rel0)
    batch = store.get_batch(ta["batch_id"])
    assert batch["failed_step"] == "merge" and "app.txt" in batch["error"]
    [start] = out.starts
    assert start["scope"] == f"deploy-fix-{batch['id']}"
    assert "feat/a" in start["prompt"] and "feat/b" in start["prompt"]
    assert batch["fixer"] == "job1"
    assert all(p[2].startswith("✗ failed at merge") for p in out.posts)
    assert not list(Path(ws.bare / "worktrees").glob("tree*"))


async def test_check_failure(store, out, ws):
    ws.scope("a", {"deploy.toml": '[deploy]\ncheck = "echo bad; exit 3"\n'})
    rel0 = ws.rev("release")
    [t] = await land(make(store, out, ws), "a")
    batch = store.get_batch(t["batch_id"])
    assert batch["failed_step"] == "check" and "bad" in batch["error"]
    assert ws.rev("release") == rel0
    assert len(out.starts) == 1


async def test_check_timeout_is_its_own_failure(store, out, ws):
    ws.scope("a", {"deploy.toml": '[deploy]\ncheck = "sleep 10"\ncheck_timeout = 0.3\n'})
    [t] = await land(make(store, out, ws), "a")
    assert store.get_batch(t["batch_id"])["failed_step"] == "check timeout"


async def test_check_sees_base_and_head(store, out, ws, tmp_path):
    log = tmp_path / "env.txt"
    ws.scope("a", {"deploy.toml":
                   f'[deploy]\ncheck = "echo $AWM_DEPLOY_BASE $AWM_DEPLOY_HEAD > {log}"\n'})
    dev0 = ws.rev("dev")
    await land(make(store, out, ws), "a")
    base, head = log.read_text().split()
    assert base == dev0 and head == ws.rev("release")


async def test_ref_moved_under_batch_fails_promote(store, out, ws):
    # The check stands in for a writer that bypassed the engine.
    ws.scope("a", {"deploy.toml": '[deploy]\ncheck = "git update-ref refs/heads/release HEAD"\n'})
    dev0 = ws.rev("dev")
    [t] = await land(make(store, out, ws), "a")
    batch = store.get_batch(t["batch_id"])
    assert batch["failed_step"] == "promote"
    assert ws.rev("dev") == dev0


async def test_missing_commit_fails_pin(store, out, ws):
    store.add_ticket(project="proj", scope="a", branch="feat/a", sha="1" * 40)
    await make(store, out, ws).drain()
    [b] = store.list_batches("proj")
    assert b["failed_step"] == "pin" and b["state"] == "failed"


async def test_bad_deploy_toml_fails_check(store, out, ws):
    ws.scope("a", {"deploy.toml": "[deploy]\nsmoke = 1\n"})
    [t] = await land(make(store, out, ws), "a")
    assert store.get_batch(t["batch_id"])["failed_step"] == "check"


# -- refresh and the fleet route ---------------------------------------------

FLEET = """
[refresh]
kind = "restart"
command = "echo $AWM_NODE_NAME $AWM_REFRESH_TO >> {log}"

[fleet]
nodes = ["mizar", "altair", "capella"]
"""


async def test_refresh_runs_homebase_then_peers_in_order(store, out, ws, tmp_path):
    log = tmp_path / "refresh.log"
    ws.scope("a", {"deploy.toml": FLEET.format(log=log)})
    [t] = await land(make(store, out, ws), "a")
    assert t["state"] == "landed"
    assert log.read_text().split() == ["mizar", ws.rev("release")]
    assert [(c[0], c[1], c[3]) for c in out.calls] == [("routine", "refresh", "altair"),
                                                      ("routine", "refresh", "capella")]
    assert out.calls[0][2]["to"] == ws.rev("release")
    nodes = store.get_batch(t["batch_id"])["nodes"]
    assert [n["node"] for n in nodes] == ["mizar", "altair", "capella"]


async def test_refresh_stops_at_first_failed_node(store, out, ws, tmp_path):
    ws.scope("a", {"deploy.toml": FLEET.format(log=tmp_path / "r.log")})
    out.invoke_results[("routine", "refresh", "altair")] = {"ok": False, "detail": "boom"}
    [t] = await land(make(store, out, ws), "a")
    assert [c[3] for c in out.calls] == ["altair"]
    batch = store.get_batch(t["batch_id"])
    assert batch["state"] == "refresh-failed" and batch["failed_step"] == "refresh"
    assert "altair" in batch["error"] and "boom" in batch["error"]
    assert t["state"] == "landed"
    assert "Release already holds" in out.starts[0]["prompt"]


async def test_unreachable_peer_fails_refresh(store, out, ws, tmp_path):
    ws.scope("a", {"deploy.toml": FLEET.format(log=tmp_path / "r.log")})
    out.invoke_results[("routine", "refresh", "altair")] = RuntimeError("no route")
    [t] = await land(make(store, out, ws), "a")
    assert "no route" in store.get_batch(t["batch_id"])["error"]


async def test_queue_refresh_starts_supervising_session(store, out, ws):
    ws.scope("a", {"deploy.toml": '[refresh]\nkind = "queue"\nprompt = "Restart awm."\n'})
    [t] = await land(make(store, out, ws), "a")
    assert t["state"] == "landed"
    [start] = out.starts
    assert start["scope"] == "deploy-refresh" and "Restart awm." in start["prompt"]


HOT = '[refresh]\nkind = "hot"\ncommand = "echo project >> {log}"\n'


async def test_hot_refresh_runs_changed_plugin_hooks(store, out, ws, tmp_path):
    log = tmp_path / "hot.log"
    ws.scope("a", {
        "deploy.toml": HOT.format(log=log),
        "plugins/p/awm-plugin.toml": f'[plugin]\nname = "p"\nrefresh = "echo p $PWD >> {log}"\n',
        "plugins/q/awm-plugin.toml": '[plugin]\nname = "q"\n',
    })
    await land(make(store, out, ws), "a")
    first = log.read_text().splitlines()
    # p has its own hook. q has none, and deploy.toml sits outside every plugin.
    assert first[0].startswith("p ") and first[0].endswith("plugins/p")
    assert first[1:] == ["project"]
    log.write_text("")
    ws.scope("a", {"plugins/p/code.py": "x = 1\n"})
    await land(make(store, out, ws), "a")
    assert log.read_text().splitlines()[0].startswith("p ")
    assert len(log.read_text().splitlines()) == 1


def test_plugin_folders(tmp_path):
    (tmp_path / "plugins/p/sub").mkdir(parents=True)
    (tmp_path / "plugins/p/awm-plugin.toml").write_text('[plugin]\nrefresh = "x"\n')
    folders, outside = plugin_folders(tmp_path, ["plugins/p/sub/a.py", "plugins/p/b.py"])
    assert folders == {"plugins/p": "x"} and not outside
    _, outside = plugin_folders(tmp_path, ["README.md"])
    assert outside


async def test_peer_refresh_verb_reads_its_own_config(store, out, ws, tmp_path):
    log = tmp_path / "peer.log"
    rel = ws.proj / "release"
    (rel / "deploy.toml").write_text(
        f'[refresh]\nkind = "restart"\ncommand = "echo $AWM_REFRESH_FROM > {log}"\n')
    verbs = DeployVerbs(make(store, out, ws, node="altair"))
    res = await verbs.refresh({"project": "proj", "from": "abc", "to": "def"})
    assert res["ok"] and res["node"] == "altair"
    assert log.read_text().strip() == "abc"


# -- config and verbs ---------------------------------------------------------

@pytest.mark.parametrize("text, msg", [
    ("[refresh]\nkind = 'reboot'", "kind must be"),
    ("[refresh]\nkind = 'restart'", "needs a command"),
    ("[refresh]\nkind = 'queue'", "needs a prompt"),
    ("[fleet]\nnodes = ['a', 'a']", "twice"),
    ("[deploy]\nnode = 'x'", "unknown keys"),
    ("[mesh]\n", "unknown section"),
])
def test_deployconf_rejects(text, msg):
    with pytest.raises(deployconf.ConfigError, match=msg):
        deployconf.parse(text)


def test_deploy_functions_have_handlers(store, out, ws):
    verbs = DeployVerbs(make(store, out, ws))
    assert {f["name"] for f in DEPLOY_FUNCTIONS} == set(verbs.handlers())
    assert all(f["tool"] == f"routine_{f['name']}" for f in DEPLOY_FUNCTIONS)


async def test_deploy_verb_takes_scope_from_caller_cwd(store, out, ws, monkeypatch):
    wt = ws.scope("a", {"a.txt": "a\n"})
    monkeypatch.chdir(wt)
    verbs = DeployVerbs(make(store, out, ws))
    res = await verbs.deploy({"_caller_pid": os.getpid()})
    assert (res["project"], res["scope"], res["branch"]) == ("proj", "a", "feat/a")
    listing = verbs.deploys({"project": "proj"})
    assert [t["id"] for t in listing["waiting"]] == [res["ticket"]]
    await verbs.deployer.drain()
    one = verbs.deploys({"id": res["ticket"]})
    assert one["state"] == "landed"
    batch = verbs.deploys({"id": one["batch_id"]})
    assert [t["id"] for t in batch["tickets"]] == [res["ticket"]]


async def test_deploy_forwards_to_canonical_node(store, out, ws):
    (ws.proj / "release" / "deploy.toml").write_text('[fleet]\ncanonical = "altair"\n')
    ws.scope("a", {"a.txt": "a\n"})
    out.invoke_results[("routine", "deploy", "altair")] = {"ticket": "t1"}
    verbs = DeployVerbs(make(store, out, ws, node="mizar"))
    res = await verbs.deploy({"project": "proj", "scope": "a"})
    assert res == {"ticket": "t1"}
    [(tool, verb, args, peer)] = out.calls
    assert (verb, peer, args["sha"]) == ("deploy", "altair", ws.rev("feat/a"))
    assert store.list_tickets("proj") == []
    # On the canonical node itself the ticket is local.
    local = await DeployVerbs(make(store, out, ws, node="altair")).deploy(
        {"project": "proj", "scope": "a"})
    assert store.get_ticket(local["ticket"])["state"] == "queued"
