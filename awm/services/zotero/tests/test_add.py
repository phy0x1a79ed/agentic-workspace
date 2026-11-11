"""Filing a paper: what reaches the library, and what never does."""

from __future__ import annotations

import json

import pytest

from awm.zotero import add, source

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

WORK = {
    "type": "journal-article", "DOI": "10.1000/XYZ", "URL": "https://doi.org/10.1000/xyz",
    "title": ["On  Protein\nSynthesis"], "container-title": ["Symp. Soc. Exp. Biol."],
    "author": [{"given": "F. H. C.", "family": "Crick"}, {"name": "The Consortium"}],
    "issued": {"date-parts": [[1958, 1]]}, "volume": "12", "page": "138-163",
    "abstract": "<jats:p>The central dogma.</jats:p>",
}


class _Service:
    """`source._send`, recording each request and answering like the Web API."""

    def __init__(self, collections=()):
        self.sent: list[tuple[str, str, dict, object]] = []
        self.collections = list(collections)

    def __call__(self, method, url, headers, body=None):
        self.sent.append((method, url, headers, json.loads(body) if body else None))
        if method == "GET":
            return source.Response(200, {"last-modified-version": "7", "total-results": str(len(self.collections))},
                                   json.dumps(self.collections).encode())
        key = "COLL0001" if url.endswith("/collections") else "ITEM0001"
        return source.Response(200, {}, json.dumps({"successful": {"0": {"key": key}}, "failed": {}}).encode())

    def posts(self):
        return [(url, headers, body) for method, url, headers, body in self.sent if method == "POST"]


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(source, "USER", "5331043")
    monkeypatch.setattr(source, "KEY", "k")
    monkeypatch.setattr(add, "crossref", lambda doi: WORK)
    svc = _Service()
    monkeypatch.setattr(source, "_send", svc)
    return svc


def test_a_crossref_work_becomes_an_item_with_only_its_types_fields():
    item = add.item_from_crossref(WORK)
    assert item == {
        "itemType": "journalArticle", "title": "On Protein Synthesis",
        "creators": [{"creatorType": "author", "firstName": "F. H. C.", "lastName": "Crick"},
                     {"creatorType": "author", "name": "The Consortium"}],
        "date": "1958-01", "url": "https://doi.org/10.1000/xyz", "abstractNote": "The central dogma.",
        "publicationTitle": "Symp. Soc. Exp. Biol.", "volume": "12", "pages": "138-163",
        "DOI": "10.1000/xyz"}
    book = add.item_from_crossref({**WORK, "type": "book", "publisher": "P"})
    assert book["itemType"] == "book" and book["extra"] == "DOI: 10.1000/xyz"
    assert "DOI" not in book and "volume" not in book and "publicationTitle" not in book


def test_a_doi_is_filed_once_in_a_new_agent_adds_collection_with_a_write_token(service):
    out = add.add(doi="https://doi.org/10.1000/XYZ")
    assert out["status"] == "added" and out["key"] == "ITEM0001"
    (curl, cheaders, cbody), (iurl, iheaders, ibody) = service.posts()
    assert curl == "/users/5331043/collections" and cbody == [{"name": "Agent adds"}]
    assert iurl == "/users/5331043/items" and ibody[0]["collections"] == ["COLL0001"]
    tokens = {cheaders["Zotero-Write-Token"], iheaders["Zotero-Write-Token"]}
    assert len(tokens) == 2 and all(len(t) == 32 for t in tokens)


def test_an_existing_agent_adds_collection_is_reused(service):
    service.collections = [{"key": "HAVE0001", "data": {"name": "Agent adds"}}]
    add.add(url="https://example.org/paper", fields={"title": "A page"})
    [(url, _, body)] = service.posts()
    assert body == [{"itemType": "webpage", "title": "A page", "url": "https://example.org/paper",
                     "collections": ["HAVE0001"]}]


def test_a_doi_the_mirror_holds_sends_nothing(service):
    out = add.add(doi="10.1000/xyz", held=[{"key": "K1", "library": "groups/1", "doi": "10.1000/XYZ"}])
    assert out == {"status": "exists", "key": "K1", "library": "groups/1", "title": None}
    assert service.sent == []


def test_a_failed_create_raises(service, monkeypatch):
    monkeypatch.setattr(source, "_send", lambda *a, **k: source.Response(
        200, {}, json.dumps({"successful": {}, "failed": {"0": {"code": 400, "message": "bad field"}}}).encode()))
    with pytest.raises(source.ZoteroError, match="bad field"):
        source.create_item({"itemType": "document", "title": "x"})
