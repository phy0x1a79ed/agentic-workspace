"""The agent stamp: the name comes from the session behind the gateway's pid, never from the model."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from door_stubs import Live  # noqa: E402

from awm.board import agent, hub_adapter  # noqa: E402


def jobs(tmp_path, job="b59c1af9", **state):
    (tmp_path / job).mkdir()
    (tmp_path / job / "state.json").write_text(json.dumps(state))
    return tmp_path


def test_a_background_job_is_named_by_its_state_file(tmp_path):
    root = jobs(tmp_path, name="msm fed shaula")
    record = {"kind": "bg", "jobId": "b59c1af9", "name": "stale seed name"}
    assert agent.session_name(7, lookup=lambda pid: record, jobs_dir=root) == "msm fed shaula"


def test_a_job_without_a_state_name_falls_back_to_its_session_record(tmp_path):
    record = {"kind": "bg", "jobId": "nojob", "name": "from the record"}
    assert agent.session_name(7, lookup=lambda pid: record, jobs_dir=tmp_path) == "from the record"


def test_an_interactive_session_is_named_by_its_record(tmp_path):
    assert agent.session_name(7, lookup=lambda pid: {"kind": "interactive", "name": "me"},
                              jobs_dir=tmp_path) == "me"


@pytest.mark.parametrize("pid,record", [
    (None, {"name": "x"}), ("7", {"name": "x"}), (True, {"name": "x"}),
    (7, None), (7, {}), (7, {"kind": "interactive"}), (7, {"name": "   "})])
def test_no_pid_no_record_or_no_name_is_none(tmp_path, pid, record):
    assert agent.session_name(pid, lookup=lambda p: record, jobs_dir=tmp_path) is None


def test_a_lookup_that_fails_is_none_and_not_an_error(tmp_path):
    def broken(pid):
        raise OSError("no sessions dir")

    assert agent.session_name(7, lookup=broken, jobs_dir=tmp_path) is None


# -- the relay ----------------------------------------------------------------------


@pytest.fixture
async def relay(monkeypatch):
    server = Live()
    await server.start()
    _, token = server.parties.add("alpha", "p", "domestic")
    monkeypatch.setenv("AWM_BOARD_ROLE", "client")
    monkeypatch.setenv("AWM_BOARD_URL", server.url)
    monkeypatch.setenv("AWM_BOARD_TOKEN", token)
    monkeypatch.setattr(agent, "session_name",
                        lambda pid, **kw: {4242: "the real session"}.get(pid))
    monkeypatch.setattr("awm.config.node_name", lambda: "deneb")
    yield server
    await server.stop()


async def test_the_relay_sends_the_stamped_sessions_name_and_ignores_the_models(relay):
    posted = await hub_adapter.post({"kind": "request", "recipient": "alpha", "title": "t",
                                     "agent": "forged", "_caller_pid": 4242})
    assert posted["ok"] and relay.board.last_post_extra["agent"] == "the real session"
    card_id = posted["card"]["id"]
    await hub_adapter.claim({"card_id": card_id, "agent": "forged", "node": "elsewhere",
                             "_caller_pid": 4242})
    assert relay.board.last_claim_extra == {"agent": "the real session", "node": "deneb"}
    sent = await hub_adapter.message_post({"card_id": card_id, "body": "hi", "agent": "forged",
                                           "_caller_pid": 4242})
    assert sent["message"]["agent"] == "the real session"


async def test_a_model_supplied_agent_is_dropped_when_no_session_is_stamped(relay):
    posted = await hub_adapter.post({"kind": "request", "recipient": "alpha", "title": "t",
                                     "agent": "forged"})
    assert posted["ok"] and relay.board.last_post_extra["agent"] is None
    unknown = await hub_adapter.post({"kind": "request", "recipient": "alpha", "title": "t",
                                      "_caller_pid": 1})
    assert unknown["ok"] and relay.board.last_post_extra["agent"] is None


async def test_the_new_verbs_relay_to_the_host(relay):
    card_id = (await hub_adapter.post({"kind": "request", "recipient": "alpha", "title": "t"}))["card"]["id"]
    await hub_adapter.claim({"card_id": card_id})
    moved = await hub_adapter.status({"card_id": card_id, "state": "active"})
    assert moved["ok"] and moved["card"]["state"] == "active"
    refused = await hub_adapter.status({"card_id": card_id, "state": "proposed"})
    assert refused["ok"] is False
    history = await hub_adapter.events({"card_id": card_id})
    assert history["ok"] and [e["type"] for e in history["events"]] == [
        "card.posted", "card.claimed", "card.moved"]
    later = await hub_adapter.events({"card_id": card_id, "since": history["events"][1]["id"]})
    assert [e["type"] for e in later["events"]] == ["card.moved"]
    rejected = await hub_adapter.reject({"card_id": card_id, "reason": "no", "_caller_pid": 4242})
    assert rejected["ok"] and rejected["card"]["state"] == "rejected"
    assert relay.board.last_reject_extra == {"agent": "the real session"}


# -- the migration verbs ---------------------------------------------------------------


class Recorder:
    def __init__(self):
        self.calls = []

    def migrate_columns(self, dry_run):
        self.calls.append(("columns", dry_run))
        return {"dry_run": dry_run}

    def migrate_archive(self, dry_run):
        self.calls.append(("archive", dry_run))
        return {"dry_run": dry_run}

    def migrate_homes(self, dry_run):
        self.calls.append(("homes", dry_run))
        return {"dry_run": dry_run}

    def migrate_threads(self, dry_run, path):
        self.calls.append(("threads", dry_run, str(path)))
        return {"dry_run": dry_run}


@pytest.fixture
def host(monkeypatch, tmp_path):
    monkeypatch.setenv("AWM_BOARD_ROLE", "host")
    monkeypatch.setenv("AWM_BOARD_DIR", str(tmp_path))
    state = SimpleNamespace(board=Recorder())
    monkeypatch.setattr(hub_adapter, "HOST", state)
    return state


async def test_the_migrations_default_to_a_dry_run_and_read_the_default_proposal_path(host, tmp_path):
    assert (await hub_adapter.migrate_columns({}, None))["dry_run"] is True
    assert (await hub_adapter.migrate_threads({}, None))["ok"] is True
    await hub_adapter.migrate_threads({"dry_run": "false", "proposal_path": "/x/p.json"}, None)
    await hub_adapter.migrate_columns({"dry_run": False}, None)
    assert (await hub_adapter.migrate_archive({}, None))["dry_run"] is True
    await hub_adapter.migrate_archive({"dry_run": "false"}, None)
    assert (await hub_adapter.migrate_homes({}, None))["dry_run"] is True
    await hub_adapter.migrate_homes({"dry_run": "false"}, None)
    assert host.board.calls == [
        ("columns", True), ("threads", True, str(tmp_path / "thread-proposal.json")),
        ("threads", False, "/x/p.json"), ("columns", False),
        ("archive", True), ("archive", False), ("homes", True), ("homes", False)]


@pytest.mark.parametrize("caller", ["peer", "peer:shaula", "user:x", "anything"])
async def test_the_migrations_refuse_every_stamped_caller(host, caller):
    for reply in (await hub_adapter.migrate_columns({"dry_run": False}, caller),
                  await hub_adapter.migrate_archive({"dry_run": False}, caller),
                  await hub_adapter.migrate_homes({"dry_run": False}, caller),
                  await hub_adapter.migrate_threads({"dry_run": False}, caller)):
        assert reply["ok"] is False
    assert host.board.calls == []


@pytest.mark.parametrize("verb", ["migrate_columns", "migrate_archive", "migrate_threads",
                                  "migrate_homes"])
async def test_the_client_role_refuses_the_migrations(monkeypatch, verb):
    monkeypatch.setenv("AWM_BOARD_ROLE", "client")
    monkeypatch.setattr(hub_adapter, "HOST", SimpleNamespace(board=Recorder()))
    reply = await hub_adapter.HANDLERS[verb]({"dry_run": False}, None)
    assert reply["ok"] is False and "host only" in reply["error"]
    assert hub_adapter.HOST.board.calls == []


async def test_a_failing_migration_is_reported_as_text(monkeypatch, host):
    def boom(dry_run, path):
        raise ValueError("cannot read the proposal")

    host.board.migrate_threads = boom
    reply = await hub_adapter.migrate_threads({"dry_run": False}, None)
    assert reply == {"ok": False, "error": "cannot read the proposal"}
