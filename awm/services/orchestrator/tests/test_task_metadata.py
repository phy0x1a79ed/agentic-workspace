"""Task metadata: a human ``title``, free-text ``tags``, and the sticky
``paused`` flag — the public user-facing set verbs, the agent-gated (attached-only)
worker verbs, and ``orch_node_open`` being born paused.

``title`` / ``tags`` are pure human metadata projected by ``orch_dag`` /
``orch_status`` (tags decoded from their stored JSON array). ``paused`` is the
durable, sticky freeze the supervisor honours (orthogonal to ``attached``): a
button-created drop-in node is born paused so it idles for the human's first turn.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def test_node_open_is_born_paused(orch):
    res = orch.operations.orch_node_open({"goal": ""})
    task = orch.DAO().get_task(res["task_id"])
    assert bool(task["paused"]) is True
    assert bool(task["attached"]) is True
    # The placement payload carries the pause so a redispatch re-seeds the freeze.
    assert orch.placements[(res["task_id"], "worker")].get("paused") is True


def test_title_and_tags_round_trip_through_dag(orch):
    res = orch.operations.orch_node_open({"goal": "x"})
    tid = res["task_id"]

    orch.operations.orch_set_title({"task_id": tid, "title": "Build the thing"})
    orch.operations.orch_set_tags({"task_id": tid, "tags": ["infra", "urgent"]})

    dag = orch.operations.orch_dag({})
    row = next(t for t in dag["tasks"] if t["task_id"] == tid)
    assert row["title"] == "Build the thing"
    assert row["tags"] == ["infra", "urgent"]  # decoded from JSON to a list

    # orch_status projects the same metadata.
    st = orch.operations.orch_status({})
    srow = next(t for t in st["tasks"] if t["task_id"] == tid)
    assert srow["title"] == "Build the thing"
    assert srow["tags"] == ["infra", "urgent"]


def test_set_paused_round_trips_and_is_durable(orch):
    res = orch.operations.orch_node_open({"goal": "x"})
    tid = res["task_id"]

    orch.operations.orch_set_paused({"task_id": tid, "paused": False})
    assert bool(orch.DAO().get_task(tid)["paused"]) is False
    dag = orch.operations.orch_dag({})
    assert next(t for t in dag["tasks"] if t["task_id"] == tid)["paused"] is False

    orch.operations.orch_set_paused({"task_id": tid, "paused": True})
    assert bool(orch.DAO().get_task(tid)["paused"]) is True


def test_tags_default_empty_and_tolerate_bad_input(orch):
    res = orch.operations.orch_node_open({"goal": "x"})
    tid = res["task_id"]
    # A freshly-opened node has no tags.
    dag = orch.operations.orch_dag({})
    assert next(t for t in dag["tasks"] if t["task_id"] == tid)["tags"] == []
    # A bare string is wrapped into a one-element list.
    orch.operations.orch_set_tags({"task_id": tid, "tags": "solo"})
    dag = orch.operations.orch_dag({})
    assert next(t for t in dag["tasks"] if t["task_id"] == tid)["tags"] == ["solo"]


def test_agent_set_title_requires_attached(orch):
    """The worker may curate the title ONLY while a human is attached; the public
    op (the user's own edit) has no such gate."""
    res = orch.operations.orch_node_open({"goal": "x"})
    tid = res["task_id"]
    agent_ref = orch.DAO().get_task(tid)["agent_ref"]

    # Born attached → the agent-gated set_title is accepted.
    ok = orch.operations.set_title(
        {"task_id": tid, "agent_ref": agent_ref, "title": "agent-set"})
    assert ok["ok"] is True
    assert orch.DAO().get_task(tid)["title"] == "agent-set"

    # Detach → the agent-gated verb is now rejected...
    orch.operations.set_attached({"task_id": tid, "agent_ref": agent_ref,
                                  "attached": False})
    rej = orch.operations.set_title(
        {"task_id": tid, "agent_ref": agent_ref, "title": "nope"})
    assert rej["ok"] is False
    assert orch.DAO().get_task(tid)["title"] == "agent-set"  # unchanged

    # ...but the user's public op still works regardless of attachment.
    orch.operations.orch_set_title({"task_id": tid, "title": "user-set"})
    assert orch.DAO().get_task(tid)["title"] == "user-set"


def test_agent_set_tags_requires_attached(orch):
    res = orch.operations.orch_node_open({"goal": "x"})
    tid = res["task_id"]
    agent_ref = orch.DAO().get_task(tid)["agent_ref"]
    orch.operations.set_attached({"task_id": tid, "agent_ref": agent_ref,
                                  "attached": False})
    rej = orch.operations.set_tags(
        {"task_id": tid, "agent_ref": agent_ref, "tags": ["x"]})
    assert rej["ok"] is False
