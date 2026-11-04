"""Private marks: a peer caller sees no post or render of a marked project or scope."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.journal]

PEERS = ["peer", "peer:altair"]
LOCALS = [None, "user:tony", "agent:awm/dev"]


@pytest.fixture()
def seeded(journal_workspace):
    from awm.journal import channel
    ids = {}
    for project, scope in [("avatar", "Avatar"), ("avatar", "Avatar/dev"), ("awm", "dev"),
                           ("awm", "secret"), ("awm", "secret/sub")]:
        ids[(project, scope)] = channel.post(project, scope, author=f"agent:{project}/{scope}",
                                             kind="journal", body=f"entry {project} {scope}").id
    return ids


@pytest.fixture()
def legacy(seeded, journal_conn):
    """Rows in the legacy spellings the journal DB still holds: ``task:<p>`` and ``('', 'user:proj:<p>')``."""
    rows = [("legacy-task", "task:avatar", "Avatar", "entry legacy task"),
            ("legacy-user", "", "user:proj:avatar", "entry legacy user"),
            ("legacy-other", "task:awm", "dev", "entry legacy awm")]
    for i, (pid, project, scope, body) in enumerate(rows):
        journal_conn.execute(
            "INSERT INTO scope_posts (id, owner_project, owner_scope, author, body, meta, ts)"
            " VALUES (?, ?, ?, 'system', ?, '{}', ?)", (pid, project, scope, body, i + 1))
        journal_conn.execute("INSERT INTO post_tags VALUES (?, 'builtin:journal')", (pid,))
    journal_conn.commit()
    return seeded


@pytest.fixture()
def marked(seeded, journal_workspace):
    from awm.config import privacy
    projects = journal_workspace["projects_dir"]
    privacy.write_marks([
        {"project": "avatar", "scope": None, "path": str(projects / "avatar")},
        {"project": "awm", "scope": "secret", "path": str(projects / "awm" / "secret")},
    ])
    return seeded


def _owners(posts):
    return {(p["project"], p["scope"]) for p in posts}


def _fetch(as_, **args):
    from awm.journal.operations.posts import _handle_fetch
    return _handle_fetch(args, as_)["posts"]


@pytest.mark.parametrize("as_", PEERS)
def test_a_peer_fetch_without_a_project_drops_marked_channels(marked, as_):
    assert _owners(_fetch(as_, kind="journal")) == {("awm", "dev")}
    assert _owners(_fetch(as_, kind="journal", query="entry")) == {("awm", "dev")}


@pytest.mark.parametrize("as_", PEERS)
def test_a_peer_fetch_naming_a_marked_project_finds_nothing(marked, as_):
    assert _fetch(as_, project="avatar", kind="journal") == []
    assert _fetch(as_, project="awm", scope="secret/sub", kind="journal") == []
    assert _fetch(as_, post_id=marked[("avatar", "Avatar")]) == []
    assert _fetch(as_, post_id=marked[("awm", "dev")]) != []


@pytest.mark.parametrize("as_", LOCALS)
def test_a_local_fetch_sees_everything(marked, as_):
    assert len(_owners(_fetch(as_, kind="journal"))) == 5
    assert _fetch(as_, post_id=marked[("avatar", "Avatar")]) != []


def test_a_broken_marks_file_shows_a_peer_nothing(seeded, journal_workspace):
    from awm.config import privacy
    privacy.marks_path().write_text("{not json")
    assert _fetch("peer", kind="journal") == []
    assert len(_fetch(None, kind="journal")) == 5


def test_an_unmark_restores_the_peer_view(marked):
    from awm.config import privacy
    privacy.write_marks([])
    assert len(_owners(_fetch("peer", kind="journal"))) == 5


def test_a_peer_cannot_post_or_subscribe_to_a_marked_scope(marked):
    from awm.journal.operations.posts import _handle_post, _handle_subscribe
    with pytest.raises(ValueError, match="no scope"):
        _handle_post({"project": "avatar", "scope": "Avatar", "author": "x", "body": "b"}, "peer")
    with pytest.raises(ValueError, match="no scope"):
        _handle_subscribe({"project": "awm", "scope": "secret", "guest": "user:x"}, "peer")
    assert _handle_post({"project": "avatar", "scope": "Avatar", "author": "user:t",
                         "body": "b"}, None)["post"]["project"] == "avatar"


def test_a_peer_render_omits_marked_siblings(marked, journal_workspace):
    from awm.journal.operations.render import _handle_render
    out = _handle_render({"project": "awm", "scope": "dev"}, "peer")
    assert "secret" not in out["content"] and "entry awm dev" in out["content"]
    assert "entry awm secret" in _handle_render({"project": "awm", "scope": "dev"}, None)["content"]


def test_a_peer_render_of_a_marked_scope_is_empty(marked, journal_workspace):
    from awm.journal.operations.render import _handle_render
    out = _handle_render({"project": "avatar", "scope": "Avatar"}, "peer")
    assert "entry avatar" not in out["content"] and "No journal entries yet" in out["content"]


def test_a_peer_export_drops_marked_documents_and_ids(marked):
    from awm.journal.operations.render import _handle_export
    peer, local = _handle_export({}, "peer:altair"), _handle_export({}, None)
    assert _owners(peer["documents"]) == {("awm", "dev")}
    assert marked[("avatar", "Avatar")] not in peer["ids"]
    assert marked[("avatar", "Avatar")] in local["ids"]
    assert len(local["documents"]) == 5


@pytest.mark.parametrize("as_", PEERS)
def test_a_peer_sees_no_legacy_spelling_of_a_marked_project(legacy, journal_workspace, as_):
    from awm.config import privacy
    from awm.journal.operations.render import _handle_export
    projects = journal_workspace["projects_dir"]
    privacy.write_marks([{"project": "avatar", "scope": None, "path": str(projects / "avatar")}])
    shown = {p["id"] for p in _fetch(as_, kind="journal")}
    assert "legacy-other" in shown and not shown & {"legacy-task", "legacy-user"}
    assert _fetch(as_, post_id="legacy-task") == [] and _fetch(as_, post_id="legacy-user") == []
    exported = {d["id"] for d in _handle_export({}, as_)["documents"]}
    assert "legacy-other" in exported and not exported & {"legacy-task", "legacy-user"}
    assert {"legacy-task", "legacy-user"} <= {p["id"] for p in _fetch(None, kind="journal")}


def test_a_scope_mark_covers_the_task_spelling(legacy, journal_workspace):
    from awm.config import privacy
    projects = journal_workspace["projects_dir"]
    privacy.write_marks([{"project": "avatar", "scope": "Avatar",
                          "path": str(projects / "avatar" / "Avatar")}])
    shown = {p["id"] for p in _fetch("peer", kind="journal")}
    assert "legacy-task" not in shown and "legacy-user" in shown
