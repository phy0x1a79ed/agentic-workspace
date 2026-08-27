"""Giving the vault a database, so nobody's first visit is a setup wizard.

The probe is the idempotency check — there is no flag on disk — and Trilium
guards the creation endpoint itself, so the interesting cases are all about
*not* acting: on a vault that already has a database, on one that is mid-sync,
and on one that is not answering yet.
"""

from __future__ import annotations

import io
import urllib.error

import pytest

from awm.trilium import provision

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def _err(code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("url", code, "", {}, io.BytesIO(body))


def _fake(monkeypatch, status_doc, on_post=None):
    calls = []

    def _request(path, *, method="GET", body=None, timeout=None):
        calls.append((method, path, body))
        if path == "/api/setup/status":
            return status_doc
        if on_post is not None:
            return on_post()
        return {}

    monkeypatch.setattr(provision, "_request", _request)
    return calls


def test_an_initialized_vault_is_not_touched(monkeypatch):
    calls = _fake(monkeypatch, {"isInitialized": True, "schemaExists": True})
    out = provision.ensure_document()
    assert out == {"action": "already-initialized", "initialized": True}
    assert [c[0] for c in calls] == ["GET"], "it must not POST"


def test_an_empty_vault_gets_a_database_without_the_demo_notes(monkeypatch):
    """The sample notes are a tour of the features. A shared vault that starts
    with somebody's demo content in it is a tidy-up nobody volunteered for."""
    calls = _fake(monkeypatch, {"isInitialized": False, "schemaExists": False})
    out = provision.ensure_document()
    assert out["initialized"] is True and out["action"] == "created"
    method, path, body = calls[-1]
    assert method == "POST"
    assert path == "/api/setup/new-document?skipDemoDb"
    assert body == {"locale": "en"}


def test_a_vault_mid_sync_is_left_alone(monkeypatch):
    """A schema without an initialized database means a sync somebody started.
    Creating a document over it would destroy what is arriving."""
    calls = _fake(monkeypatch, {"isInitialized": False, "schemaExists": True})
    out = provision.ensure_document()
    assert out["action"] == "sync-in-progress" and out["initialized"] is False
    assert [c[0] for c in calls] == ["GET"]


def test_a_vault_that_is_not_answering_is_not_an_error(monkeypatch):
    """The normal case on a cold start: the child takes up to two minutes to
    bind. The supervision loop will be back in twenty seconds, so this must not
    raise into it."""
    def _request(path, **kw):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(provision, "_request", _request)
    out = provision.ensure_document()
    assert out["action"] == "unreachable" and out["initialized"] is False


def test_losing_the_race_counts_as_success(monkeypatch):
    """Two supervision ticks, or a tick racing a start. Trilium refuses the
    second POST itself, and somebody else having done the work is not a
    failure."""
    def _refuse():
        raise _err(400, b"already busy")

    calls = _fake(monkeypatch, {"isInitialized": False, "schemaExists": False},
                  on_post=_refuse)
    out = provision.ensure_document()
    assert out["initialized"] is True and out["action"] == "already-under-way"


def test_a_real_failure_is_reported_and_not_raised(monkeypatch):
    """A broken vault has to keep reporting through `status`; an exception here
    would take the supervision loop's whole pass with it."""
    def _fail():
        raise _err(500, b"stack trace")

    _fake(monkeypatch, {"isInitialized": False, "schemaExists": False},
          on_post=_fail)
    out = provision.ensure_document()
    assert out["initialized"] is False and out["action"] == "failed"
    assert "500" in out["detail"]


# -- the launchbar ----------------------------------------------------------
#
# Trilium keeps the launchbar in the database, so "this button is off" is a
# note that has been moved rather than a setting that has been written. That
# makes the order of the two calls the whole of the correctness argument, and
# makes "runs again next start" a feature rather than waste.


class _FakeApi:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = fail or set()

    def branch_id(self, note_id, parent):
        return f"{parent}_{note_id}"

    def put_branch(self, note_id, parent):
        if note_id in self.fail:
            from awm.trilium.etapi import EtapiError
            raise EtapiError("nope")
        self.calls.append(("put", f"{parent}_{note_id}"))
        return {}

    def delete_branch(self, branch_id):
        self.calls.append(("delete", branch_id))


def _fake_api(monkeypatch, api):
    from awm.trilium import etapi
    monkeypatch.setattr(etapi, "client", lambda: api)
    return api


def test_the_day_note_launchers_move_off_the_bar(monkeypatch):
    api = _fake_api(monkeypatch, _FakeApi())
    out = provision.hide_day_note_launchers()
    assert out["moved"] == [n for n, _, _ in provision.DAY_NOTE_LAUNCHERS]
    assert not out["failed"]
    assert ("put", "_lbAvailableLaunchers__lbToday") in api.calls
    assert ("delete", "_lbVisibleLaunchers__lbToday") in api.calls


def test_the_new_place_is_made_before_the_old_one_is_taken_away(monkeypatch):
    """A note's last branch takes the note with it. Delete-then-create would
    destroy the launcher, and upstream would put it back — visible."""
    api = _fake_api(monkeypatch, _FakeApi())
    provision.hide_day_note_launchers()
    for note_id, visible, available in provision.DAY_NOTE_LAUNCHERS:
        made = api.calls.index(("put", f"{available}_{note_id}"))
        gone = api.calls.index(("delete", f"{visible}_{note_id}"))
        assert made < gone, note_id


def test_a_vault_that_refuses_does_not_stop_provisioning(monkeypatch):
    """A launchbar is not worth failing a provision over, and a cold vault
    refusing is the ordinary case rather than a fault."""
    api = _fake_api(monkeypatch, _FakeApi(fail={"_lbToday"}))
    out = provision.hide_day_note_launchers()
    assert out["failed"] and "_lbToday" in out["failed"][0]
    assert "_lbCalendar" in out["moved"], "one failure must not abandon the rest"


def test_both_widgets_that_can_create_a_day_note_are_covered(monkeypatch):
    """`getDayNote` is reachable from the Today launcher and from the calendar
    widget, and the calendar has a second copy on the mobile bar. Missing any
    one of them leaves the tree creatable by somebody."""
    ids = {n for n, _, _ in provision.DAY_NOTE_LAUNCHERS}
    assert ids == {"_lbToday", "_lbCalendar", "_lbMobileCalendar"}
