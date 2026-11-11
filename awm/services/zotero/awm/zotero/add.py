"""Filing one paper in the personal library, for a user who asked for it.

A DOI is looked up on Crossref and turned into a Zotero item. A URL alone
becomes a web page. Explicit fields override both. Every added item lands in
the `Agent adds` collection, made on first use, so a person can review what
agents filed apart from what they saved themselves.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from collections.abc import Iterable
from typing import Any

from awm.zotero import source

COLLECTION = "Agent adds"
CROSSREF = "https://api.crossref.org/works/"
#: A generic client name. Crossref asks for a contact address, and this sends none.
USER_AGENT = "awm-zotero/0.1"

#: Crossref work type -> (Zotero item type, the field its container goes in).
TYPES = {
    "journal-article": ("journalArticle", "publicationTitle"),
    "proceedings-article": ("conferencePaper", "proceedingsTitle"),
    "book-chapter": ("bookSection", "bookTitle"),
    "book": ("book", None),
    "posted-content": ("preprint", None),
}
#: The item types whose schema has a DOI field. Others carry it in `extra`.
HAS_DOI = {"journalArticle", "conferencePaper", "preprint"}

_TAG = re.compile(r"<[^>]+>")
_DOI = re.compile(r"10\.\d{4,9}/\S+")


def norm_doi(s: str) -> str:
    m = _DOI.search(s or "")
    if not m:
        raise ValueError(f"not a DOI: {s!r}")
    return m.group(0).rstrip(".").lower()


def crossref(doi: str) -> dict[str, Any]:
    req = urllib.request.Request(CROSSREF + urllib.parse.quote(doi),
                                 headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=source.TIMEOUT_S) as r:
            return json.load(r)["message"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ValueError(f"Crossref does not know {doi}") from e
        raise source.ZoteroUnavailable(f"Crossref answered {e.code}") from e
    except OSError as e:
        raise source.ZoteroUnavailable(f"cannot reach Crossref: {e}") from e


def item_from_crossref(m: dict[str, Any]) -> dict[str, Any]:
    """A Zotero item from a Crossref work, holding only fields its type allows."""
    kind, container = TYPES.get(m.get("type") or "", ("document", None))
    creators = []
    for role, people in (("author", m.get("author")), ("editor", m.get("editor"))):
        for p in people or []:
            creators.append({"creatorType": role, "firstName": p.get("given", ""),
                             "lastName": p["family"]} if p.get("family")
                            else {"creatorType": role, "name": p.get("name", "")})
    parts = ((m.get("issued") or {}).get("date-parts") or [[]])[0]
    doi = (m.get("DOI") or "").lower()
    item: dict[str, Any] = {
        "itemType": kind,
        "title": " ".join((m.get("title") or [""])[0].split()),
        "creators": creators,
        "date": "-".join(f"{int(x):02d}" if i else str(x) for i, x in enumerate(parts) if x),
        "url": m.get("URL", ""),
        "abstractNote": " ".join(_TAG.sub(" ", m.get("abstract") or "").split()),
    }
    if container and m.get("container-title"):
        item[container] = m["container-title"][0]
    if kind == "journalArticle":
        item.update(volume=m.get("volume", ""), issue=m.get("issue", ""),
                    pages=m.get("page", ""), ISSN=(m.get("ISSN") or [""])[0])
    elif kind in ("conferencePaper", "bookSection"):
        item.update(pages=m.get("page", ""), publisher=m.get("publisher", ""))
    elif kind in ("book", "document"):
        item["publisher"] = m.get("publisher", "")
    if kind in HAS_DOI:
        item["DOI"] = doi
    elif doi:
        item["extra"] = f"DOI: {doi}"
    return {k: v for k, v in item.items() if v not in ("", [])}


def collection_key() -> str:
    """The `Agent adds` collection's key, made if the library lacks it."""
    for c in source.collections(source.PERSONAL).records:
        data = c.get("data") or {}
        if data.get("name") == COLLECTION and not data.get("parentCollection"):
            return c["key"]
    return source.create_collection(COLLECTION)


def add(*, doi: str = "", url: str = "", fields: dict[str, Any] | None = None,
        held: Iterable[dict[str, Any]] = ()) -> dict[str, Any]:
    """File one item, unless `held` (the mirrored items) already has its DOI."""
    fields = dict(fields or {})
    if doi:
        doi = norm_doi(doi)
        have = next((i for i in held if _DOI.search(i.get("doi") or "")
                     and norm_doi(i["doi"]) == doi), None)
        if have:
            return {"status": "exists", "key": have.get("key"),
                    "library": have.get("library"), "title": have.get("title")}
        item = item_from_crossref(crossref(doi))
    elif url:
        item = {"itemType": "webpage", "title": url, "url": url}
    elif fields.get("title"):
        item = {"itemType": "document"}
    else:
        raise ValueError("add needs a doi, a url, or fields with a title")
    item.update(fields)
    item["collections"] = [collection_key()]
    key = source.create_item(item)
    return {"status": "added", "key": key, "title": item.get("title"),
            "item_type": item["itemType"], "collection": COLLECTION}
