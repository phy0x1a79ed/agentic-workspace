"""Private marks: a peer caller never sees a marked project's sessions through any transcripts verb."""

from __future__ import annotations

import gzip
import json
import os
import time

import pytest

import awm.config as config
from awm.config import privacy
from awm.transcripts import layout, search, status, sweep
from awm.transcripts.hub_adapter import HANDLERS

PEER, LOCAL, USER = "peer:altair", None, "user:tony"


@pytest.fixture
def slugs(tmp_path, monkeypatch):
    """A marked project, a session inside it, a sibling whose name only starts alike, and an open one."""
    monkeypatch.setattr(config, "AWM_DIR", tmp_path / ".awm")
    root = tmp_path / "ws" / "projects"
    privacy.write_marks([{"project": "avatar", "scope": None, "path": str(root / "avatar")}])
    return {"marked": privacy.slug(root / "avatar"), "nested": privacy.slug(root / "avatar" / "Avatar" / "dev"),
            "sibling": privacy.slug(root / "avatar2"), "open": privacy.slug(root / "awm")}


@pytest.fixture
def tree(tmp_path, monkeypatch, slugs):
    projects, archive = tmp_path / "projects", tmp_path / "archive"
    old = time.time() - 30 * 86400
    for name in slugs.values():
        for base, suffix in ((projects, ".jsonl"), (archive, ".jsonl.gz")):
            (base / name).mkdir(parents=True)
            f = base / name / f"s-{name[-6:]}{suffix}"
            opener = gzip.open if suffix.endswith(".gz") else open
            with opener(f, "wt") as fh:
                fh.write('{"text":"the deep kitchen sink"}\n')
            os.utime(f, (old, old))
    for module in (layout, search, status, sweep):
        monkeypatch.setattr(module, "ARCHIVE", archive, raising=False)
        monkeypatch.setattr(module, "PROJECTS", projects, raising=False)
    monkeypatch.setattr(sweep.live, "session_ids", lambda: set())
    return projects, archive


def visible(slugs):
    return {slugs["sibling"], slugs["open"]}


@pytest.mark.parametrize("verb,key", [("search", "pattern"), ("query", "query")])
def test_a_peer_search_drops_marked_sessions_and_keeps_the_sibling(tree, slugs, verb, key):
    args = {key: "kitchen" if verb == "query" else "(?i)kitchen"}
    assert {h["project"] for h in HANDLERS[verb](args, PEER)["hits"]} == visible(slugs)
    for as_ in (LOCAL, USER):
        assert {h["project"] for h in HANDLERS[verb](args, as_)["hits"]} == set(slugs.values())


@pytest.mark.parametrize("verb,key", [("search", "pattern"), ("query", "query")])
def test_a_peer_naming_a_marked_project_gets_what_an_unknown_one_gets(tree, slugs, verb, key):
    named = HANDLERS[verb]({key: "kitchen", "project": slugs["nested"]}, PEER)
    unknown = HANDLERS[verb]({key: "kitchen", "project": "-no-such-project"}, PEER)
    assert named["hits"] == unknown["hits"] == [] and named["truncated"] is unknown["truncated"] is False
    prefix = HANDLERS[verb]({key: "kitchen", "project": slugs["marked"]}, PEER)
    assert {h["project"] for h in prefix["hits"]} == {slugs["sibling"]}


def test_a_peer_limit_fills_with_visible_hits(tree, slugs):
    out = HANDLERS["query"]({"query": "kitchen", "limit": 2}, PEER)
    assert {h["project"] for h in out["hits"]} == visible(slugs) and out["truncated"] is True


def test_status_counts_only_what_a_peer_may_see(tree, slugs):
    peer = HANDLERS["status"]({}, PEER)
    assert peer["live"]["sessions"] == peer["archive"]["sessions"] == 2
    assert peer["live"]["past_retention"] == 2
    assert HANDLERS["status"]({}, LOCAL)["live"]["sessions"] == 4


def test_restore_answers_a_peer_as_if_a_marked_session_were_not_archived(tree, slugs):
    sid = f"s-{slugs['marked'][-6:]}"
    with pytest.raises(FileNotFoundError) as hidden:
        HANDLERS["restore"]({"project": slugs["marked"], "session": sid}, PEER)
    with pytest.raises(FileNotFoundError) as missing:
        HANDLERS["restore"]({"project": "-no-such-project", "session": sid}, PEER)
    assert str(hidden.value) == f"{slugs['marked']}/{sid} is not in the archive"
    assert str(missing.value).endswith("is not in the archive")


def test_a_peer_sweep_leaves_marked_sessions_alone(tree, slugs):
    projects, _ = tree
    out = sweep.archive(days=7, hide=privacy.hidden_slug)
    assert out["sessions"] == 2
    assert {p.name for p in projects.iterdir() if any(p.iterdir())} == {slugs["marked"], slugs["nested"]}


def test_an_unreadable_marks_file_hides_every_session_from_a_peer(tree, slugs):
    privacy.marks_path().write_text("{not json")
    assert HANDLERS["query"]({"query": "kitchen"}, PEER)["hits"] == []
    assert HANDLERS["status"]({}, PEER)["live"]["sessions"] == 0
    assert len(HANDLERS["query"]({"query": "kitchen"}, LOCAL)["hits"]) == 4


@pytest.fixture
def wandered(tree, slugs, tmp_path):
    """Archived sessions filed under the workspace folder: one that wandered into the marked project."""
    _, archive = tree
    root = tmp_path / "ws" / "projects"
    folder = archive / privacy.slug(root)
    folder.mkdir()
    lines = {
        "a-wander": [{"cwd": str(root), "text": "kitchen sink before"},
                     {"text": "kitchen sink with no cwd"},
                     {"cwd": str(root / "avatar" / "Avatar"), "text": "inside"},
                     {"cwd": str(root), "text": "kitchen sink after"}],
        "s-stay": [{"cwd": str(root), "text": "kitchen sink at home"},
                   {"cwd": str(root / "avatar2"), "text": "kitchen sink next door"}],
    }
    for sid, body in lines.items():
        with gzip.open(folder / f"{sid}.jsonl.gz", "wt") as fh:
            fh.writelines(json.dumps(line) + "\n" for line in body)
    return folder.name


@pytest.mark.parametrize("verb,key", [("search", "pattern"), ("query", "query")])
def test_a_peer_never_sees_a_session_that_entered_a_marked_path(wandered, verb, key):
    def sessions(as_):
        hits = HANDLERS[verb]({key: "kitchen", "project": wandered}, as_)["hits"]
        return {h["session"] if verb == "search" else h["id"].split(":")[0]
                for h in hits if h["project"] == wandered}
    assert sessions(PEER) == {"s-stay"}
    assert sessions(LOCAL) == sessions(USER) == {"a-wander", "s-stay"}


def test_a_peer_limit_counts_only_sessions_it_may_see(wandered):
    out = HANDLERS["search"]({"pattern": "kitchen", "project": wandered, "limit": 1}, PEER)
    assert [h["session"] for h in out["hits"]] == ["s-stay"] and out["truncated"] is True


def test_restore_answers_a_peer_as_if_a_wandering_session_were_not_archived(wandered, tree):
    with pytest.raises(FileNotFoundError, match="^" + wandered + "/a-wander is not in the archive$"):
        HANDLERS["restore"]({"project": wandered, "session": "a-wander"}, PEER)
    assert HANDLERS["restore"]({"project": wandered, "session": "s-stay"}, PEER)["files"] == 1
    assert HANDLERS["restore"]({"project": wandered, "session": "a-wander"}, LOCAL)["files"] == 1


@pytest.mark.parametrize("verb,args", [("sweep", {"dry_run": True}), ("prune", {"days": 1})])
@pytest.mark.parametrize("as_", ["peer", PEER])
def test_a_peer_cannot_run_the_maintenance_verbs(tree, verb, args, as_):
    with pytest.raises(RuntimeError, match=f"^unknown function '{verb}'$"):
        HANDLERS[verb](args, as_)
