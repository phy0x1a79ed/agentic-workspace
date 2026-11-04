"""The fleet's one vocabulary: merge, clash, the dispatched agent and the block (R107 item 6)."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.journal]


@pytest.fixture()
def node(journal_workspace, monkeypatch):
    from awm.journal import tags, tagsync
    monkeypatch.setattr(tags, "_node", lambda: "mizar")
    monkeypatch.setattr(tagsync, "peers", lambda: [])
    started = []
    monkeypatch.setattr(tagsync, "dispatcher", lambda c: started.append(c) or "job-1")
    return started


def _remote(tid="r" * 32, name="complaint", created=10**15, version=1, origin="altair",
            description="A gripe from altair."):
    return {"id": tid, "name": name, "description": description, "origin": origin,
            "version": version, "created_ms": created}


def test_a_peers_new_tag_and_its_edits_merge_in(node):
    from awm.journal import tags, tagsync
    assert tagsync.merge([_remote()], from_node="altair") == {"added": 1, "updated": 0, "clashes": 0}
    assert tags.by_name("complaint").origin == "altair"
    tagsync.merge([_remote(name="gripe", version=2)], from_node="altair")
    assert tags.get("r" * 32).name == "gripe"
    tagsync.merge([_remote(name="stale", version=1)], from_node="altair")
    assert tags.get("r" * 32).name == "gripe"


def test_the_newer_of_two_same_named_tags_is_renamed_and_blocks_the_peers(node):
    from awm.journal import tags, tagsync
    local = tags.add("complaint", "A bug report.")
    out = tagsync.merge([_remote(created=local.created_ms + 1000)], from_node="altair")
    assert out["clashes"] == 1
    assert tags.by_name("complaint").id == local.id
    assert tagsync.provisional(tags.get("r" * 32).name)
    assert tagsync.blocked("altair") and not tagsync.blocked("alcor")
    assert node == []  # altair added the newer tag, so altair dispatches the agent
    tagsync.merge([_remote(name="altair-gripe", version=2)], from_node="altair")
    assert tags.get("r" * 32).name == "altair-gripe" and not tagsync.blocked("altair")


def test_the_node_holding_the_newer_tag_dispatches_the_agent_once(node):
    from awm.journal import tags, tagsync
    local = tags.add("complaint", "A bug report.")
    tagsync.merge([_remote(created=local.created_ms - 1000)], from_node="altair")
    assert tags.by_name("complaint").id == "r" * 32
    assert tagsync.provisional(tags.get(local.id).name)
    assert [c["newer_id"] for c in node] == [local.id]
    assert "tag_edit" in tagsync._prompt(node[0])
    tagsync.dispatch_open()
    assert len(node) == 1
    tags.edit(local.id, name="bug-report")
    assert not tagsync.blocked("altair")


def test_a_peer_read_is_refused_while_a_clash_stands(node, monkeypatch):
    from awm import config
    from awm.journal import tags, tagsync
    from awm.journal.operations.posts import POST_HANDLERS
    monkeypatch.setattr(config, "caller_peer", lambda as_: as_.split(":", 1)[1])
    local = tags.add("complaint", "A bug report.")
    tagsync.merge([_remote(created=local.created_ms + 1)], from_node="altair")
    with pytest.raises(PermissionError, match="tag clash"):
        POST_HANDLERS["fetch"]({"tags": "journal"}, "peer:altair")


def test_receive_takes_tags_from_a_peer_only(node, monkeypatch):
    from awm import config
    from awm.journal import tagsync
    monkeypatch.setattr(config, "caller_peer", lambda as_: (as_ or "").split(":", 1)[-1] or None)
    with pytest.raises(PermissionError):
        tagsync.receive([_remote()], None)
    assert tagsync.receive([_remote()], "peer:altair")["added"] == 1


def test_a_pull_merges_each_peers_vocabulary(node, monkeypatch):
    from awm.journal import tags, tagsync
    monkeypatch.setattr(tagsync, "peers", lambda: ["altair"])
    assert tagsync.pull(lambda peer: [_remote()])["altair"]["added"] == 1
    assert tags.by_name("complaint") is not None
