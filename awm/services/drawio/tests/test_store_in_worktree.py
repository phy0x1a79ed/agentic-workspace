"""A drawio store nested inside a user's scope worktree.

What these pin: the store joins the enclosing repo instead of init-ing its
own; reads at a revision resolve relative to the store; an amend never folds
a foreign tip (a notes commit) into a diagram edit; the hub adapter hands a
bound user their own Service and the legacy one otherwise.
"""

from __future__ import annotations

import subprocess
import time

import pytest

from awm.config import userroot
from awm.drawio import hub_adapter, store as store_mod
from awm.drawio.store import Store

from test_checkout import TEMPLATE

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c",
                           "user.email=t@x", *args], capture_output=True,
                          text=True, check=True).stdout


def _worktree(path):
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "user/tony")
    (path / "README").write_text("x")
    _git(path, "add", "."); _git(path, "commit", "-qm", "init")
    return path


def test_store_joins_the_enclosing_repo(tmp_path):
    wt = _worktree(tmp_path / "wt")
    store = Store(wt / "drawio")
    assert not (wt / "drawio" / ".git").exists()
    assert (wt / "drawio" / ".gitattributes").is_file()
    assert _git(wt, "rev-parse", "--abbrev-ref", "HEAD").strip() == "user/tony"
    store.create("fig/a.drawio", author="user:tony", xml=TEMPLATE)
    log = _git(wt, "log", "-1", "--format=%B", "--name-only")
    assert "Author-Handle: user:tony" in log
    assert "drawio/fig/a.drawio" in log
    # A second Store over the same root neither re-inits nor re-commits.
    head = _git(wt, "rev-parse", "HEAD")
    Store(wt / "drawio")
    assert _git(wt, "rev-parse", "HEAD") == head


def test_read_at_a_revision_resolves_relative_to_the_store(tmp_path):
    wt = _worktree(tmp_path / "wt")
    store = Store(wt / "drawio")
    first = store.create("fig/a.drawio", author="user:tony", xml=TEMPLATE)
    rev = first["rev"]
    assert "<mxfile" in store.read("fig/a.drawio", rev=rev)
    assert store.history("fig/a.drawio")[0].rev == rev


def test_amend_never_swallows_a_foreign_tip(tmp_path, monkeypatch):
    wt = _worktree(tmp_path / "wt")
    store = Store(wt / "drawio")
    store.create("fig/a.drawio", author="user:tony", xml=TEMPLATE)
    # A notes autocommit by the same author lands on top.
    (wt / "notes").mkdir()
    (wt / "notes" / "n.md").write_text("hi")
    _git(wt, "add", "notes")
    _git(wt, "commit", "-qm", "notes: autosave\n\nAuthor-Handle: user:tony")
    notes_tip = _git(wt, "rev-parse", "HEAD").strip()
    edited = TEMPLATE.replace('value="', 'value="x', 1)
    res = store.write("fig/a.drawio", edited, author="user:tony")
    assert res["amended"] is False
    assert _git(wt, "rev-parse", "HEAD~1").strip() == notes_tip
    assert (wt / "notes" / "n.md").read_text() == "hi"


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    services = tmp_path / ".awm" / "services"
    services.mkdir(parents=True)
    monkeypatch.setattr("awm.config.WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr("awm.config.SERVICES_DIR", services)
    monkeypatch.setattr("awm.persistence.databases.SERVICES_DIR", services)
    monkeypatch.delenv(userroot.TEMPLATE_ENV, raising=False)
    monkeypatch.delenv(userroot.STRICT_ENV, raising=False)
    for name in ("tony", "steven"):
        _worktree(tmp_path / "projects" / "userdata" / name)
    legacy = Store(tmp_path / "legacy")
    from awm.drawio.checkout import Checkouts
    from awm.drawio.service import Service
    monkeypatch.setattr(hub_adapter, "SERVICE",
                        Service(legacy, Checkouts(legacy, tmp_path / "co")))
    monkeypatch.setattr(hub_adapter, "_USER_SERVICES", {})
    return tmp_path


def test_handlers_pick_the_callers_store(ws):
    create = hub_adapter.HANDLERS["create"]
    listing = hub_adapter.HANDLERS["list"]
    create({"save": "t.drawio"}, "user:tony")
    create({"save": "s.drawio"}, "user:steven")
    create({"save": "l.drawio"}, "agent-slug")
    names = lambda as_: sorted(d["save"] for d in listing({}, as_)["diagrams"])
    assert names("user:tony") == ["t.drawio"]
    assert names("user:steven") == ["s.drawio"]
    assert names(None) == ["l.drawio"]
    assert names("user:nobody") == ["l.drawio"]
    assert (ws / "projects/userdata/tony/drawio/t.drawio").is_file()
    assert (ws / "projects/userdata/steven/drawio/s.drawio").is_file()
    log = _git(ws / "projects/userdata/tony", "log", "-1", "--format=%B")
    assert "Author-Handle: user:tony" in log


def test_editor_open_returns_a_user_topic(ws):
    hub_adapter.HANDLERS["create"]({"save": "t.drawio"}, "user:tony")
    info = hub_adapter.HANDLERS["editor_open"]({"save": "t.drawio", "tab": "1"}, "user:tony")
    assert info["topic"] == "drawio:tony:t.drawio"
    hub_adapter.HANDLERS["create"]({"save": "l.drawio"}, None)
    info = hub_adapter.HANDLERS["editor_open"]({"save": "l.drawio", "tab": "1"}, None)
    assert info["topic"] == "drawio:l.drawio"


def test_strict_mode_refuses_non_users(ws, monkeypatch):
    monkeypatch.setenv(userroot.STRICT_ENV, "1")
    with pytest.raises(PermissionError):
        hub_adapter.HANDLERS["list"]({}, "agent-slug")
    assert hub_adapter.HANDLERS["list"]({}, "user:tony")["diagrams"] == []
