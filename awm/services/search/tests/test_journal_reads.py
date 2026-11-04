"""A search get of a journal post counts as a read for citations (sessions.md R15)."""

from __future__ import annotations

import asyncio

import pytest

from awm.search import hub_adapter
from awm.search.hub_adapter import HANDLERS

from .conftest import doc, synced

pytestmark = [pytest.mark.search]


@pytest.fixture()
async def served(archive, gateway, monkeypatch):
    gateway.plug("journals", {
        "j1": doc("Journal", "a session journal", project="awm", scope="dev", tags=["journal"]),
        "m1": doc("Message", "a message", project="awm", scope="dev", tags=["message"]),
    }, service="journal")
    gateway.handlers[("journal", "note_read")] = lambda args: {"ok": True, "recorded": 1}
    await synced(archive, gateway)
    monkeypatch.setattr(hub_adapter, "ARCHIVE", archive)
    return gateway


async def _noted(gateway):
    await asyncio.sleep(0)
    await asyncio.gather(*hub_adapter._NOTING)
    return [c[2] for c in gateway.calls if c[:2] == ("journal", "note_read")]


async def test_a_get_of_a_journal_post_notes_the_read(served):
    await HANDLERS["get"]({"source": "journals", "id": "j1", "_caller_pid": 4242}, None)
    assert await _noted(served) == [{"post_id": "j1", "_caller_pid": 4242}]


async def test_other_tags_and_callers_without_a_session_note_nothing(served):
    await HANDLERS["get"]({"source": "journals", "id": "m1", "_caller_pid": 4242}, None)
    await HANDLERS["get"]({"source": "journals", "id": "j1"}, None)
    assert await _noted(served) == []


async def test_a_journal_that_does_not_answer_never_fails_the_get(served):
    served.handlers[("journal", "note_read")] = lambda args: RuntimeError("down")
    out = await HANDLERS["get"]({"source": "journals", "id": "j1", "_caller_pid": 4242}, None)
    assert out["document"]["tags"] == ["journal"]
    await _noted(served)
