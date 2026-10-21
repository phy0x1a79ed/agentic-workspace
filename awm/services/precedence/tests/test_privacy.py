"""Private marks: a peer never sees a decision tagged with a marked project's name."""

from __future__ import annotations

import pytest

from awm.config import privacy

pytestmark = [pytest.mark.precedence]

PEER, LOCAL, USER = "peer:altair", None, "user:tony"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    services_dir = tmp_path / "services"
    services_dir.mkdir()
    import awm.config as cfg
    monkeypatch.setattr(cfg, "AWM_DIR", tmp_path / ".awm")
    import awm.persistence.databases as dbmod
    monkeypatch.setattr(dbmod, "SERVICES_DIR", services_dir, raising=False)
    import awm.precedence.dao as daomod
    monkeypatch.setattr(daomod, "_initialized", False)
    daomod.init()
    return tmp_path


@pytest.fixture()
def ids(isolated):
    from awm.precedence import dao, store
    conn = dao.connect()
    try:
        made = {
            name: store.add(conn, context=f"kitchen sink {name}", question=f"which sink {name}?",
                            decision=f"the deep sink {name}", tag=tags, created="2026-03-04")["id"]
            for name, tags in (("open", ["ui"]), ("tagged", ["UI", "Avatar"]),
                               ("near", ["avatar2"]), ("mention", []))
        }
        store.note(conn, made["tagged"], body="a note on the private one")
        store.vote(conn, made["tagged"], direction="up")
    finally:
        conn.close()
    privacy.write_marks([{"project": "avatar", "scope": None, "path": str(isolated / "projects" / "avatar")}])
    return made


def _call(verb, args, as_):
    from awm.precedence.hub_adapter import HANDLERS
    return HANDLERS[verb](args, as_)


def _listed(as_, **args):
    return {r["id"] for r in _call("search", {"k": 50, **args}, as_)["results"]}


def test_a_peer_search_leaves_out_a_tagged_decision(ids):
    everything = set(ids.values())
    assert _listed(LOCAL) == _listed(USER) == everything
    assert _listed(PEER) == _listed("peer") == everything - {ids["tagged"]}
    assert _listed(PEER, keyword="sink") == everything - {ids["tagged"]}
    assert _listed(PEER, tag=["Avatar"]) == set()


@pytest.mark.parametrize("verb, args, error", [
    ("get", {}, "no such decision"),
    ("note", {"body": "x"}, "no such decision"),
    ("vote", {"direction": "up"}, "no such decision"),
    ("edit", {"rm_tag": ["Avatar"]}, "no such decision"),
    ("supersede", {}, "no such decision"),
    ("remove", {}, "no such decision"),
])
def test_a_peer_naming_a_tagged_decision_gets_the_missing_id_error(ids, verb, args, error):
    with pytest.raises(ValueError) as hidden:
        _call(verb, {"id": ids["tagged"], **args}, PEER)
    with pytest.raises(ValueError) as missing:
        _call(verb, {"id": "nonesuch", **args}, PEER)
    assert str(hidden.value) == f"{error}: {ids['tagged']}"
    assert str(missing.value) == f"{error}: nonesuch"
    assert _call("get", {"id": ids["tagged"]}, LOCAL)["tags"] == ["Avatar", "UI"]


def test_a_peer_cannot_point_at_or_merge_a_tagged_decision(ids):
    with pytest.raises(ValueError, match="^no such replacement decision: "):
        _call("supersede", {"id": ids["open"], "by": ids["tagged"]}, PEER)
    with pytest.raises(ValueError, match="^no such keeper: "):
        _call("merge", {"keeper": ids["tagged"], "dup": [ids["open"]]}, PEER)
    assert _call("merge", {"keeper": ids["open"], "dup": [ids["tagged"]]}, PEER)["merged"] == 0
    assert _call("get", {"id": ids["tagged"]}, LOCAL)["id"] == ids["tagged"]


def test_peer_stats_and_export_leave_out_a_tagged_decision(ids):
    local, peer = _call("stats", {}, LOCAL), _call("stats", {}, PEER)
    assert (local["decisions"], local["notes"], local["upvotes"]) == (4, 1, 1)
    assert (peer["decisions"], peer["notes"], peer["upvotes"]) == (3, 0, 0)
    assert peer["embedded"] == local["embedded"] - 1
    page = _call("export", {}, PEER)
    assert ids["tagged"] not in page["ids"] and len(page["ids"]) == 3
    assert ids["tagged"] not in {d["id"] for d in page["documents"]}
    assert ids["tagged"] in _call("export", {}, LOCAL)["ids"]


def test_export_pages_skip_a_hidden_decision_without_naming_it(ids):
    seen, cursor = [], None
    while True:
        page = _call("export", {"limit": 1, **({"cursor": cursor} if cursor else {})}, PEER)
        seen += [d["id"] for d in page["documents"]]
        if page["done"]:
            break
        assert page["next_cursor"] != ids["tagged"]
        cursor = page["next_cursor"]
    assert sorted(seen) == sorted(set(ids.values()) - {ids["tagged"]})


def test_export_carries_the_tags_for_the_search_archive(ids):
    docs = {d["id"]: d for d in _call("export", {}, LOCAL)["documents"]}
    assert docs[ids["tagged"]]["tags"] == ["Avatar", "UI"]
    assert "tags" not in docs[ids["mention"]]


def test_unmarking_shows_the_decision_again(ids):
    privacy.write_marks([])
    assert ids["tagged"] in _listed(PEER)


def test_an_unreadable_marks_file_hides_every_tagged_decision_from_a_peer(ids):
    privacy.marks_path().write_text("{not json")
    assert _listed(PEER) == {ids["mention"]}
    assert _listed(LOCAL) == set(ids.values())
