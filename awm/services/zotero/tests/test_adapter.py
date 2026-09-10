"""What the service does before its first sync.

The adapter's `on_start` is the one place that reads the bundle outside the
sync itself, so it is the one place a rename of a `stats()` key goes unnoticed
until the service will not start.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from awm.zotero import bundle, hub_adapter


@pytest.fixture
def _no_timer(monkeypatch):
    """Start the service without starting its 20-minute loop."""
    started: list[str] = []
    monkeypatch.setattr(hub_adapter, "spawn_supervised",
                        lambda name, fn: started.append(name))
    return started


def _bundle_at(tmp_path):
    item = {"version": 1, "data": {"key": "AAAA", "itemType": "journalArticle",
                                   "title": "A paper"}}
    b = bundle.Bundle(tmp_path)
    b.write(bundle.merge([bundle.normalize([item], [], {})], {"users/0": 7}))
    return b


def test_start_reports_the_bundle_it_found(tmp_path, monkeypatch, caplog,
                                           _no_timer):
    b = _bundle_at(tmp_path)
    monkeypatch.setattr(hub_adapter.sync, "bundle", lambda: b)
    with caplog.at_level(logging.INFO):
        asyncio.run(hub_adapter._on_start())
    line = " ".join(r.getMessage() for r in caplog.records)
    assert "1 items" in line and "users/0" in line
    assert _no_timer == ["zotero:sync", "zotero:push", "zotero:stream"]


def test_start_survives_a_bundle_that_is_not_there_yet(tmp_path, monkeypatch,
                                                       caplog, _no_timer):
    """A missing bundle is the state before the first sync, not a failure.

    The adapter treats an `on_start` raise as a failed initialisation and the
    gateway then reaps the service, so this must stay non-fatal.
    """
    monkeypatch.setattr(hub_adapter.sync, "bundle",
                        lambda: bundle.Bundle(tmp_path / "nothing"))
    with caplog.at_level(logging.INFO):
        asyncio.run(hub_adapter._on_start())
    assert _no_timer == ["zotero:sync", "zotero:push", "zotero:stream"]


# -- status ------------------------------------------------------------------


def _status(monkeypatch, tmp_path, versions):
    """Run the status verb against a one-item bundle and a stubbed service."""
    b = _bundle_at(tmp_path)
    monkeypatch.setattr(hub_adapter.sync, "bundle", lambda: b)
    monkeypatch.setattr(hub_adapter.source, "versions", lambda: versions)
    monkeypatch.setattr(hub_adapter.source, "whoami",
                        lambda: {"user_id": "1", "username": "someone",
                                 "writes": False, "groups_read": True})
    return asyncio.run(hub_adapter._h_status({}))


def test_status_is_not_behind_when_every_library_matches(tmp_path, monkeypatch):
    out = _status(monkeypatch, tmp_path, {"users/0": 7})
    assert out["library"]["reachable"] is True
    assert out["library"]["key_can_write"] is False
    assert out["behind"] is False


def test_status_is_behind_when_one_library_moved(tmp_path, monkeypatch):
    out = _status(monkeypatch, tmp_path, {"users/0": 8})
    assert out["behind"] is True


def test_status_is_behind_when_a_library_is_new(tmp_path, monkeypatch):
    """A group library shared with you after the last sync is the common case,
    and it moves no version the bundle already holds."""
    out = _status(monkeypatch, tmp_path, {"users/0": 7, "groups/1": 3})
    assert out["behind"] is True


def test_an_unreachable_library_is_reported_not_raised(tmp_path, monkeypatch):
    """The network drops and the service restarts. Saying so is the answer, not
    failing the tick."""
    b = _bundle_at(tmp_path)
    monkeypatch.setattr(hub_adapter.sync, "bundle", lambda: b)

    def _boom() -> dict:
        raise hub_adapter.source.ZoteroUnavailable("the network went away")

    monkeypatch.setattr(hub_adapter.source, "whoami", _boom)
    monkeypatch.setattr(hub_adapter.source, "versions", _boom)
    out = asyncio.run(hub_adapter._h_status({}))
    assert out["library"]["reachable"] is False
    assert "behind" not in out
