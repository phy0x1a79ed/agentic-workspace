"""A dict standing in for the vault's ETAPI, for tests that are about our
logic rather than about Trilium.

It grew out of the one `test_note_upsert` carried inline. What made it worth
sharing is the board: a card is a note plus an attribute plus a search, and a
fake that models only notes cannot say whether the right card was matched.

What it models is what this service actually calls, and no more — notes and
their content, attributes, attachments, branches, and a `#label` search
restricted to a subtree. Anything else raises rather than returning a plausible
empty result, so a test cannot pass because the fake quietly agreed.
"""

from __future__ import annotations

import httpx


#: What Trilium reports as an attachment's length.
#:
#: Text is decoded and stored in a SQLite TEXT column, and the length reported
#: back is `LENGTH()` over that, which counts characters. Modelling it as bytes
#: made this fake unable to reproduce a real defect: every text attachment
#: holding one non-ASCII character was re-uploaded on every pass, and the suite
#: stayed green throughout.
_STRING_MIMES = frozenset({
    "application/javascript", "application/x-javascript", "application/json",
    "application/x-sql", "image/svg+xml", "application/inkml+xml",
})


def _content_length(blob: bytes, mime: str) -> int:
    if not (mime.startswith("text/") or mime in _STRING_MIMES):
        return len(blob)
    try:
        return len(blob.decode("utf-8"))
    except UnicodeDecodeError:
        return len(blob)


class FakeVault:
    """Just enough ETAPI to hold a note tree, its labels and its files."""

    def __init__(self) -> None:
        #: note id -> {"title", "content", "type", "mime", "children", "parents"}
        self.notes: dict[str, dict] = {
            "root": {"title": "root", "content": "", "type": "text",
                     "mime": "text/html", "children": [], "parents": []}}
        #: attribute id -> {"noteId", "type", "name", "value", "isInheritable"}
        self.attributes: dict[str, dict] = {}
        #: attachment id -> {"ownerId", "title", "mime", "role", "blob"}
        self.attachments: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        #: The query parameters of every search, so a test can assert what the
        #: adapter asked ETAPI for rather than only what came back.
        self.searches: list[dict] = []
        self.next_id = 0

    # -- helpers a test may use directly -------------------------------------

    def new_id(self, prefix: str) -> str:
        self.next_id += 1
        return f"{prefix}{self.next_id}"

    def labels(self, note_id: str) -> dict[str, str]:
        return {a["name"]: a["value"] for a in self.attributes.values()
                if a["noteId"] == note_id and a["type"] == "label"}

    def descendants(self, note_id: str) -> set[str]:
        out, stack = set(), list(self.notes[note_id]["children"])
        while stack:
            nid = stack.pop()
            if nid in out:
                continue
            out.add(nid)
            stack += self.notes[nid]["children"]
        return out

    # -- the transport -------------------------------------------------------

    def request(self, method: str, url: str, **kw) -> httpx.Response:
        path = url.split("/etapi/", 1)[1]
        self.calls.append((method, path))
        req = httpx.Request(method, url)
        head, _, tail = path.partition("/")

        if method == "POST" and head == "create-note":
            return self._create_note(kw["json"], req)
        if head == "notes" and "?" not in path and tail:
            return self._notes(method, tail, kw, req)
        if head.startswith("notes"):          # GET /etapi/notes?search=...
            return self._search(kw.get("params") or {}, req)
        if head == "attributes":
            return self._attributes(method, tail, kw, req)
        if head == "attachments":
            return self._attachments(method, tail, kw, req)
        if head == "branches":
            return self._branches(method, tail, kw, req)
        raise AssertionError(f"unexpected {method} {path}")

    # -- notes ---------------------------------------------------------------

    def _create_note(self, body: dict, req) -> httpx.Response:
        nid = self.new_id("note")
        self.notes[nid] = {"title": body["title"], "content": body["content"],
                           "type": body.get("type", "text"),
                           "mime": body.get("mime", "text/html"),
                           "children": [], "parents": [body["parentNoteId"]]}
        self.notes[body["parentNoteId"]]["children"].append(nid)
        return httpx.Response(201, request=req, json={
            "note": {"noteId": nid, "title": body["title"]},
            "branch": {"branchId": f"{body['parentNoteId']}_{nid}"}})

    def _notes(self, method: str, tail: str, kw: dict, req) -> httpx.Response:
        note_id, _, sub = tail.partition("/")
        if method == "DELETE" and not sub:
            for gone in self.descendants(note_id) | {note_id}:
                self.notes.pop(gone, None)
                for aid in [a for a, v in self.attributes.items()
                            if v["noteId"] == gone]:
                    del self.attributes[aid]
            for note in self.notes.values():
                note["children"] = [c for c in note["children"]
                                    if c in self.notes]
            return httpx.Response(204, request=req)

        note = self.notes[note_id]
        if sub == "content":
            if method == "GET":
                return httpx.Response(200, text=note["content"], request=req)
            note["content"] = kw["content"].decode("utf-8")
            return httpx.Response(204, request=req)
        if sub == "attachments":
            return httpx.Response(200, request=req, json=[
                self._attachment_pojo(aid) for aid, a in self.attachments.items()
                if a["ownerId"] == note_id])
        if method == "PATCH" and not sub:
            note.update({k: v for k, v in kw["json"].items()
                         if k in ("title", "type", "mime")})
        return httpx.Response(200, request=req, json=self._note_pojo(note_id))

    def _note_pojo(self, note_id: str) -> dict:
        note = self.notes[note_id]
        return {"noteId": note_id, "title": note["title"], "type": note["type"],
                "mime": note["mime"], "childNoteIds": list(note["children"]),
                "parentNoteIds": list(note["parents"]),
                "parentBranchIds": [f"{p}_{note_id}" for p in note["parents"]],
                "attributes": [self._attribute_pojo(a) for a, v
                               in self.attributes.items()
                               if v["noteId"] == note_id]}

    def _search(self, params: dict, req) -> httpx.Response:
        """Only the one query shape this service builds: `#name`, optionally
        restricted to a subtree. Anything else is a test writing a query the
        code does not."""
        self.searches.append(dict(params))
        query = str(params.get("search", "")).strip()
        if not query.startswith("#") or " " in query:
            raise AssertionError(f"fake vault cannot answer {query!r}")
        name = query[1:]
        scope = params.get("ancestorNoteId")
        allowed = self.descendants(scope) if scope else set(self.notes)
        hits = sorted({a["noteId"] for a in self.attributes.values()
                       if a["type"] == "label" and a["name"] == name
                       and a["noteId"] in allowed})
        return httpx.Response(200, request=req, json={
            "results": [self._note_pojo(n) for n in hits]})

    # -- attributes ----------------------------------------------------------

    def _attribute_pojo(self, attribute_id: str) -> dict:
        a = self.attributes[attribute_id]
        return {"attributeId": attribute_id, **a}

    def _attributes(self, method: str, tail: str, kw: dict, req) -> httpx.Response:
        if method == "POST":
            body = kw["json"]
            aid = body["attributeId"]
            self.attributes[aid] = {
                "noteId": body["noteId"], "type": body["type"],
                "name": body["name"], "value": body.get("value", ""),
                "isInheritable": bool(body.get("isInheritable"))}
            return httpx.Response(201, request=req,
                                  json=self._attribute_pojo(aid))
        if method == "PATCH":
            self.attributes[tail]["value"] = kw["json"]["value"]
            return httpx.Response(200, request=req,
                                  json=self._attribute_pojo(tail))
        if method == "DELETE":
            self.attributes.pop(tail, None)
            return httpx.Response(204, request=req)
        raise AssertionError(f"unexpected {method} attributes/{tail}")

    # -- attachments ---------------------------------------------------------

    def _attachment_pojo(self, attachment_id: str) -> dict:
        a = self.attachments[attachment_id]
        return {"attachmentId": attachment_id, "ownerId": a["ownerId"],
                "title": a["title"], "mime": a["mime"], "role": a["role"],
                "contentLength": _content_length(a["blob"], a["mime"])}

    def _attachments(self, method: str, tail: str, kw: dict, req) -> httpx.Response:
        if method == "POST":
            body = kw["json"]
            aid = self.new_id("att")
            self.attachments[aid] = {
                "ownerId": body["ownerId"], "title": body["title"],
                "mime": body["mime"], "role": body.get("role", "file"),
                "blob": b""}
            return httpx.Response(201, request=req,
                                  json=self._attachment_pojo(aid))
        attachment_id, _, sub = tail.partition("/")
        if sub == "content" and method == "PUT":
            self.attachments[attachment_id]["blob"] = kw["content"]
            return httpx.Response(204, request=req)
        if method == "DELETE":
            self.attachments.pop(attachment_id, None)
            return httpx.Response(204, request=req)
        raise AssertionError(f"unexpected {method} attachments/{tail}")

    # -- branches ------------------------------------------------------------

    def _branches(self, method: str, tail: str, kw: dict, req) -> httpx.Response:
        if method == "POST":
            body = kw["json"]
            note = self.notes[body["noteId"]]
            parent = self.notes[body["parentNoteId"]]
            if body["parentNoteId"] not in note["parents"]:
                note["parents"].append(body["parentNoteId"])
                parent["children"].append(body["noteId"])
            return httpx.Response(200, request=req, json={
                "branchId": f"{body['parentNoteId']}_{body['noteId']}",
                "noteId": body["noteId"],
                "parentNoteId": body["parentNoteId"]})
        if method == "DELETE":
            parent_id, _, note_id = tail.partition("_")
            note = self.notes.get(note_id)
            if note and parent_id in note["parents"]:
                note["parents"].remove(parent_id)
                self.notes[parent_id]["children"].remove(note_id)
            return httpx.Response(204, request=req)
        raise AssertionError(f"unexpected {method} branches/{tail}")
