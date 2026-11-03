import json

import pytest

from awm.kb import hub_adapter, papers


def item(key, **kw):
    return {"key": key, "title": f"Title {key}", "creators": ["Smith, A", {"lastName": "Doe"}], "year": "2014",
            "publication": "J", "doi": f"10.1/{key}", "abstract": f"abstract {key}", **kw}


@pytest.fixture
def library(tmp_path):
    path = tmp_path / "library.json"
    keys = ["CCC", "AAA", "EEE", "BBB", "DDD"]
    path.write_text(json.dumps({"pulled": "2026-09-08T22:08:14Z", "items": [item(k) for k in keys]}))
    return path


def test_document_shape():
    doc = papers.to_document(item("AAA", url="https://x"), "2026-01-01T00:00:00Z")
    assert set(doc) == {"id", "title", "text", "ts", "kind", "url"}
    assert doc["id"] == "AAA" and doc["kind"] == "paper" and doc["url"] == "https://x"
    assert doc["ts"] == "2014-01-01T00:00:00Z"
    assert doc["text"].startswith("Title AAA\nSmith, A, Doe (2014). J. doi:10.1/AAA\n\nabstract AAA")


def test_document_falls_back_to_pulled_time_and_doi_url():
    doc = papers.to_document(item("AAA", year=None), "2026-01-01T00:00:00Z")
    assert doc["ts"] == "2026-01-01T00:00:00Z" and doc["url"] == "https://doi.org/10.1/AAA"
    assert "url" not in papers.to_document(item("AAA", doi=None), "")


def test_full_pass_pages_in_key_order_and_ends_with_every_id(library):
    seen, cursor, pages = [], None, 0
    while True:
        page = papers.export_page(library, cursor, 2)
        pages += 1
        seen += [d["id"] for d in page["documents"]]
        if page["done"]:
            assert page["next_cursor"] is None
            assert page["ids"] == ["AAA", "BBB", "CCC", "DDD", "EEE"]
            break
        assert "ids" not in page
        cursor = page["next_cursor"]
    assert seen == ["AAA", "BBB", "CCC", "DDD", "EEE"] and pages == 3


def test_a_page_of_exactly_the_remainder_is_done(library):
    page = papers.export_page(library, "BBB", 3)
    assert page["done"] and [d["id"] for d in page["documents"]] == ["CCC", "DDD", "EEE"]


def test_limit_is_clamped_and_defaults(library):
    assert len(papers.export_page(library, None, None)["documents"]) == 5
    assert len(papers.export_page(library, None, -5)["documents"]) == 1
    assert papers.export_page(library, None, 10**6)["done"]


def test_missing_library_raises_rather_than_reporting_an_empty_set(tmp_path):
    with pytest.raises(FileNotFoundError):
        papers.export_page(tmp_path / "absent.json", None, None)


async def test_export_handler_serves_local_callers_and_refuses_foreign(library, monkeypatch):
    monkeypatch.setattr(hub_adapter.instances, "ZOTERO_LIBRARY", library)
    monkeypatch.setattr(hub_adapter.config, "peer_relation", lambda n: "foreign")
    out = await hub_adapter.HANDLERS["export"]({"limit": 2}, as_=None)
    assert [d["id"] for d in out["documents"]] == ["AAA", "BBB"] and out["next_cursor"] == "BBB"
    with pytest.raises(PermissionError):
        await hub_adapter.HANDLERS["export"]({}, as_="peer:x")
