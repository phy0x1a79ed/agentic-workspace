"""A small synchronous ETAPI client for the board writer and the spawn body check."""

from __future__ import annotations

from typing import Any

import httpx


class EtapiError(Exception):
    def __init__(self, status: int, text: str) -> None:
        super().__init__(f"ETAPI {status}: {text[:200]}")
        self.status = status


class Etapi:
    def __init__(self, url: str, token: str, *, timeout: float = 10.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.http = httpx.Client(base_url=url.rstrip("/") + "/etapi",
                                 headers={"Authorization": token}, timeout=timeout,
                                 transport=transport)

    def close(self) -> None:
        self.http.close()

    def _call(self, method: str, path: str, **kw: Any) -> httpx.Response:
        r = self.http.request(method, path, **kw)
        if r.status_code >= 400:
            raise EtapiError(r.status_code, r.text)
        return r

    def note(self, note_id: str) -> dict[str, Any] | None:
        try:
            return self._call("GET", f"/notes/{note_id}").json()
        except EtapiError as e:
            if e.status == 404:
                return None
            raise

    def create_note(self, parent: str, note_id: str, title: str, type_: str, content: str = "",
                    mime: str | None = None) -> dict[str, Any]:
        body = {"parentNoteId": parent, "noteId": note_id, "title": title, "type": type_,
                "content": content}
        if mime:
            body["mime"] = mime
        return self._call("POST", "/create-note", json=body).json()["note"]

    def patch_note(self, note_id: str, **fields: Any) -> None:
        self._call("PATCH", f"/notes/{note_id}", json=fields)

    def delete_note(self, note_id: str) -> None:
        try:
            self._call("DELETE", f"/notes/{note_id}")
        except EtapiError as e:
            if e.status != 404:
                raise

    def content(self, note_id: str) -> str:
        return self._call("GET", f"/notes/{note_id}/content").text

    def set_content(self, note_id: str, text: str) -> None:
        self._call("PUT", f"/notes/{note_id}/content", content=text.encode(),
                   headers={"Content-Type": "text/plain"})

    def search(self, query: str, limit: int = 100) -> list[dict[str, Any]]:
        return self._call("GET", "/notes", params={"search": query, "limit": limit}).json()["results"]

    def sync_attributes(self, note: dict[str, Any], want: dict[tuple[str, str], str],
                        managed: set[tuple[str, str]]) -> None:
        """Make the note's ``(type, name)`` attributes in ``managed`` equal ``want``.

        A value change on a relation needs a new attribute: ETAPI patches only label values.
        """
        have: dict[tuple[str, str], dict[str, Any]] = {}
        for a in note.get("attributes", []):
            key = (a["type"], a["name"])
            if key not in managed or a["noteId"] != note["noteId"]:
                continue
            if key in have:  # a duplicate from an earlier crash
                self._call("DELETE", f"/attributes/{a['attributeId']}")
                continue
            have[key] = a
        for key in managed:
            cur, value = have.get(key), want.get(key)
            if value is None:
                if cur is not None:
                    self._call("DELETE", f"/attributes/{cur['attributeId']}")
            elif cur is None:
                self._call("POST", "/attributes", json={"noteId": note["noteId"], "type": key[0],
                                                        "name": key[1], "value": value})
            elif cur["value"] != value:
                if key[0] == "label":
                    self._call("PATCH", f"/attributes/{cur['attributeId']}", json={"value": value})
                else:
                    self._call("DELETE", f"/attributes/{cur['attributeId']}")
                    self._call("POST", "/attributes", json={"noteId": note["noteId"], "type": key[0],
                                                            "name": key[1], "value": value})
