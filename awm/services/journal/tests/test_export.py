"""journal_export: the page shape the search archive reads."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.journal]


@pytest.fixture()
def posted(journal_workspace):
    from awm.journal import channel
    ids = []
    for i in range(5):
        ids.append(channel.post("awm", "dev", author="agent:awm/dev", kind="journal",
                                body=f"entry {i}\nmore", meta={"title": f"Title {i}"}).id)
    channel.post("awm", "dev", author="system", kind="system", body="not indexed")
    channel.post("awm", "dev", author="user:a", kind="message", body="")
    return sorted(ids)


def test_one_page_carries_everything_and_the_id_set(posted):
    from awm.journal.export import export_page
    page = export_page()
    assert page["done"] is True and page["next_cursor"] is None
    assert page["ids"] == posted
    doc = page["documents"][0]
    assert set(doc) == {"id", "title", "text", "ts", "project", "scope", "kind"}
    assert doc["project"] == "awm" and doc["scope"] == "dev" and doc["kind"] == "journal"
    assert doc["ts"].endswith("+00:00") and doc["text"].startswith("entry")


def test_pages_walk_in_id_order_without_repeats(posted):
    from awm.journal.export import export_page
    seen, cursor, pages = [], None, 0
    while True:
        page = export_page(cursor, 2)
        pages += 1
        seen += [d["id"] for d in page["documents"]]
        if page["done"]:
            assert page["ids"] == posted and page["next_cursor"] is None
            break
        assert "ids" not in page and page["next_cursor"] == page["documents"][-1]["id"]
        cursor = page["next_cursor"]
    assert seen == posted and pages == 3


def test_an_edit_between_pages_neither_repeats_nor_skips(posted, journal_conn):
    from awm.journal.export import export_page
    first = export_page(None, 2)
    journal_conn.execute("DELETE FROM scope_posts WHERE id=?", (posted[0],))
    journal_conn.commit()
    rest = export_page(first["next_cursor"], 10)
    assert [d["id"] for d in rest["documents"]] == posted[2:]
    assert rest["ids"] == posted[1:]


def test_limit_is_clamped(posted):
    from awm.journal.export import export_page
    assert len(export_page(None, 0)["documents"]) == 1
    assert len(export_page(None, 99999)["documents"]) == 5


def test_a_title_falls_back_to_the_first_line(journal_workspace):
    from awm.journal import channel
    from awm.journal.export import export_page
    channel.post("awm", "dev", author="user:a", kind="message", body="\n\nfirst line\nsecond")
    assert export_page()["documents"][0]["title"] == "first line"


def test_the_handler_and_the_manifest_agree(posted):
    from awm.journal.operations.render import RENDER_HANDLERS, RENDER_MANIFEST_FUNCTIONS
    fn = next(f for f in RENDER_MANIFEST_FUNCTIONS if f["name"] == "export")
    assert fn["tool"] == "journal_export" and fn["surfaces"] == ["cli", "http"]
    assert RENDER_HANDLERS["export"]({"limit": 2})["done"] is False
