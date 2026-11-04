"""The deploy engine: tickets, batches, the six steps, refresh and the failure dispatch."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from awm.routine import deployconf, fleetconf
from awm.routine.deploy import Deployer
from awm.routine.hub_adapter import DEPLOY_FUNCTIONS, DeployVerbs
from awm.routine.refresh import Refresher, plugin_folders

from tests.conftest import git

pytestmark = pytest.mark.smoke


def make(store, out, ws, node="mizar", fleet: str = "") -> Deployer:
    ref = Refresher(out, node=node, checkout_root=lambda p: ws.proj / "release",
                    fleet_config=lambda: fleetconf.parse(fleet))
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
"""

# A fixture install config: the real one is the cloud project's (fleet.md R140).
INSTALL = """
plugins = ["*"]

[fleet]
home = "mizar"
nodes = ["mizar", "alcor", "capella"]
"""


async def test_refresh_runs_homebase_then_peers_in_order(store, out, ws, tmp_path):
    log = tmp_path / "refresh.log"
    ws.scope("a", {"deploy.toml": FLEET.format(log=log)})
    [t] = await land(make(store, out, ws, fleet=INSTALL), "a")
    assert t["state"] == "landed"
    assert log.read_text().split() == ["mizar", ws.rev("release")]
    assert [(c[0], c[1], c[3]) for c in out.calls] == [("routine", "refresh", "alcor"),
                                                      ("routine", "refresh", "capella")]
    assert out.calls[0][2]["to"] == ws.rev("release")
    nodes = store.get_batch(t["batch_id"])["nodes"]
    assert [n["node"] for n in nodes] == ["mizar", "alcor", "capella"]


async def test_refresh_stops_at_first_failed_node(store, out, ws, tmp_path):
    ws.scope("a", {"deploy.toml": FLEET.format(log=tmp_path / "r.log")})
    out.invoke_results[("routine", "refresh", "alcor")] = {"ok": False, "detail": "boom"}
    [t] = await land(make(store, out, ws, fleet=INSTALL), "a")
    assert [c[3] for c in out.calls] == ["alcor"]
    batch = store.get_batch(t["batch_id"])
    assert batch["state"] == "refresh-failed" and batch["failed_step"] == "refresh"
    assert "alcor" in batch["error"] and "boom" in batch["error"]
    assert t["state"] == "landed"
    assert "Release already holds" in out.starts[0]["prompt"]


async def test_unreachable_peer_fails_refresh(store, out, ws, tmp_path):
    ws.scope("a", {"deploy.toml": FLEET.format(log=tmp_path / "r.log")})
    out.invoke_results[("routine", "refresh", "alcor")] = RuntimeError("no route")
    [t] = await land(make(store, out, ws, fleet=INSTALL), "a")
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
    verbs = DeployVerbs(make(store, out, ws, node="alcor"))
    res = await verbs.refresh({"project": "proj", "from": "abc", "to": "def"})
    assert res["ok"] and res["node"] == "alcor"
    assert log.read_text().strip() == "abc"


# -- config and verbs ---------------------------------------------------------

@pytest.mark.parametrize("text, msg", [
    ("[refresh]\nkind = 'reboot'", "kind must be"),
    ("[refresh]\nkind = 'restart'", "needs a command"),
    ("[refresh]\nkind = 'queue'", "needs a prompt"),
    ("[fleet]\nnodes = ['a']", "install config"),
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


FORWARD = '[fleet]\nhome = "mizar"\n[fleet.project.proj]\ncanonical = "altair"\n'


async def test_deploy_forwards_to_canonical_node(store, out, ws):
    ws.scope("a", {"a.txt": "a\n"})
    out.invoke_results[("routine", "deploy", "altair")] = {"ticket": "t1"}
    verbs = DeployVerbs(make(store, out, ws, node="mizar", fleet=FORWARD))
    res = await verbs.deploy({"project": "proj", "scope": "a"})
    assert res == {"ticket": "t1"}
    [(tool, verb, args, peer)] = out.calls
    assert (verb, peer, args["sha"]) == ("deploy", "altair", ws.rev("feat/a"))
    assert store.list_tickets("proj") == []
    # On the canonical node itself the ticket is local.
    local = await DeployVerbs(make(store, out, ws, node="altair", fleet=FORWARD)).deploy(
        {"project": "proj", "scope": "a"})
    assert store.get_ticket(local["ticket"])["state"] == "queued"


# -- the install config's [fleet] table ---------------------------------------

def test_fleetconf_reads_home_and_overrides():
    f = fleetconf.parse(INSTALL + '[fleet.project.engcyc]\ncanonical = "altair"\n'
                        'nodes = ["altair"]\n')
    assert f.canonical("awm") == "mizar"
    assert f.rollout("awm") == ("mizar", "alcor", "capella")
    assert (f.canonical("engcyc"), f.rollout("engcyc")) == ("altair", ("altair",))


def test_fleetconf_absent_is_this_node_alone(tmp_path):
    f = fleetconf.load(tmp_path / "config.toml")
    assert f.canonical("awm") == "" and f.rollout("awm") == ()


@pytest.mark.parametrize("text, msg", [
    ('[fleet]\nhome = "a"\nnodes = ["b", "a"]', "must start with 'a'"),
    ('[fleet]\nnodes = ["a", "a"]', "twice"),
    ('[fleet]\nhomebase = "a"', "unknown keys"),
    ('[fleet.project.p]\ncanonical = "b"\nnodes = ["a", "b"]', "must start with 'b'"),
    ('[fleet.project.p]\npeer = "b"', "only canonical and nodes"),
])
def test_fleetconf_rejects(text, msg):
    with pytest.raises(fleetconf.FleetConfigError, match=msg):
        fleetconf.parse(text)


# -- the backup remote (install.md R114) --------------------------------------

BACKUP = '[promote]\nbackup = "github"\n'


async def test_backup_takes_each_release(store, out, ws):
    gh = ws.backup()
    ws.scope("a", {"deploy.toml": BACKUP})
    [t] = await land(make(store, out, ws), "a")
    assert t["state"] == "landed"
    assert git(gh, "rev-parse", "release") == ws.rev("release")
    steps = [s["step"] for s in store.get_batch(t["batch_id"])["steps"]]
    assert steps.index("promote") < steps.index("backup") < steps.index("refresh")


async def test_diverged_backup_stops_before_release_moves(store, out, ws, tmp_path):
    gh = ws.backup()
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(gh), str(other))
    git(other, "checkout", "-q", "release")
    git(other, "commit", "-q", "--allow-empty", "-m", "made on github")
    git(other, "push", "-q", "origin", "release")
    tip = git(gh, "rev-parse", "release")
    ws.scope("a", {"deploy.toml": BACKUP})
    rel0, dev0 = ws.rev("release"), ws.rev("dev")
    [t] = await land(make(store, out, ws), "a")
    batch = store.get_batch(t["batch_id"])
    assert (t["state"], batch["state"], batch["failed_step"]) == ("failed", "failed", "backup")
    assert (ws.rev("release"), ws.rev("dev")) == (rel0, dev0)
    assert git(gh, "rev-parse", "release") == tip
    assert "never forces" in batch["error"]
    assert "Nothing landed" in out.starts[0]["prompt"]


async def test_failed_backup_push_lands_but_refreshes_nothing(store, out, ws, tmp_path):
    gh = ws.backup()
    hook = gh / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho read only >&2\nexit 1\n")
    hook.chmod(0o755)
    log = tmp_path / "r.log"
    ws.scope("a", {"deploy.toml": BACKUP + FLEET.format(log=log)})
    [t] = await land(make(store, out, ws, fleet=INSTALL), "a")
    batch = store.get_batch(t["batch_id"])
    assert (t["state"], batch["state"]) == ("landed", "backup-failed")
    assert "read only" in batch["error"]
    assert not log.exists() and out.calls == []
    assert "the backup remote does not" in out.starts[0]["prompt"]


async def test_missing_backup_remote_fails_backup(store, out, ws):
    ws.scope("a", {"deploy.toml": BACKUP})
    [t] = await land(make(store, out, ws), "a")
    assert store.get_batch(t["batch_id"])["failed_step"] == "backup"
    assert t["state"] == "failed"


# -- the card half of the verdict (R26) ---------------------------------------

async def test_verdict_posts_to_the_callers_card(store, out, ws, monkeypatch):
    wt = ws.scope("a", {"a.txt": "a\n"})
    monkeypatch.chdir(wt)
    out.cards[os.getpid()] = "TASK0001"
    verbs = DeployVerbs(make(store, out, ws))
    res = await verbs.deploy({"_caller_pid": os.getpid()})
    assert store.get_ticket(res["ticket"])["card"] == "TASK0001"
    await verbs.deployer.drain()
    [(card, body)] = out.card_posts
    assert card == "TASK0001" and body.startswith("✓") and res["ticket"] in body
    assert out.posts[0][2] == body


async def test_no_card_means_journal_only(store, out, ws):
    ws.scope("a", {"a.txt": "a\n"})
    await land(make(store, out, ws), "a")
    assert out.card_posts == [] and len(out.posts) == 1


async def test_forwarded_deploy_carries_the_card(store, out, ws, monkeypatch):
    wt = ws.scope("a", {"a.txt": "a\n"})
    monkeypatch.chdir(wt)
    out.cards[os.getpid()] = "TASK0002"
    out.invoke_results[("routine", "deploy", "altair")] = {"ticket": "t1"}
    verbs = DeployVerbs(make(store, out, ws, node="mizar", fleet=FORWARD))
    await verbs.deploy({"_caller_pid": os.getpid()})
    [(_, _, args, _)] = out.calls
    assert args["card"] == "TASK0002"


# -- push nodes (R31: a node that cannot fetch from home) ---------------------

PUSH = INSTALL + '\n[fleet.push.alcor]\nurl = "{url}"\nprojects = ["proj"]\n'


async def test_push_node_takes_release_before_its_refresh(store, out, ws, tmp_path):
    peer = tmp_path / "peer-{project}.git"
    git(tmp_path, "init", "-q", "--bare", str(tmp_path / "peer-proj.git"))
    ws.scope("a", {"deploy.toml": FLEET.format(log=tmp_path / "r.log")})
    [t] = await land(make(store, out, ws, fleet=PUSH.format(url=peer)), "a")
    assert t["state"] == "landed"
    assert git(tmp_path / "peer-proj.git", "rev-parse", "refs/mizar/release") == ws.rev("release")
    assert [c[3] for c in out.calls] == ["alcor", "capella"]


async def test_push_refuses_a_non_fast_forward(store, out, ws, tmp_path):
    target = tmp_path / "peer-proj.git"
    git(tmp_path, "init", "-q", "--bare", str(target))
    other = tmp_path / "o"
    git(tmp_path, "init", "-q", str(other))
    git(other, "commit", "-q", "--allow-empty", "-m", "unrelated")
    git(other, "push", "-q", str(target), "HEAD:refs/mizar/release")
    ws.scope("a", {"deploy.toml": FLEET.format(log=tmp_path / "r.log")})
    [t] = await land(make(store, out, ws, fleet=PUSH.format(url=tmp_path / "peer-{project}.git")), "a")
    batch = store.get_batch(t["batch_id"])
    assert batch["state"] == "refresh-failed" and "refs/mizar/release" in batch["error"]
    assert out.calls == []


def test_fleetconf_push():
    f = fleetconf.parse(PUSH.format(url="altair:/w/projects/{project}/.bare"))
    assert f.push_url("alcor", "proj") == "altair:/w/projects/proj/.bare"
    assert f.push_url("alcor", "other") == "" and f.push_url("capella", "proj") == ""


@pytest.mark.parametrize("text, msg", [
    ('[fleet.push.a]\nurl = "a:/x"\nprojects = ["p"]', "with {project}"),
    ('[fleet.push.a]\nurl = "a:/{project}"', "url and projects"),
    ('[fleet.push.a]\nurl = "a:/{project}"\nprojects = "p"', "list of project names"),
])
def test_fleetconf_push_rejects(text, msg):
    with pytest.raises(fleetconf.FleetConfigError, match=msg):
        fleetconf.parse(text)
