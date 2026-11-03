"""For the Chromium board test only: the trilium tool's verbs over a local container's ETAPI.

The plugin itself never holds an ETAPI token (fleet.md R105). This stands in for the
``trilium`` tool on deneb, including the fixed ``note_id`` on ``note_create`` that the
real tool does not take yet.
"""

from __future__ import annotations

from typing import Any

import httpx

from awm.operations.relay import Refused


class EtapiTool:
    def __init__(self, url: str, token: str) -> None:
        self.http = httpx.Client(base_url=url.rstrip("/") + "/etapi",
                                 headers={"Authorization": token}, timeout=10)

    def close(self) -> None:
        self.http.close()

    def _do(self, method: str, path: str, **kw: Any) -> httpx.Response:
        r = self.http.request(method, path, **kw)
        if r.status_code >= 400:
            raise Refused(f"{r.status_code} {r.text[:200]}")
        return r

    def _labels(self, note_id: str, labels: dict[str, str] | None, type_: str = "label") -> None:
        attrs = self._do("GET", f"/notes/{note_id}").json()["attributes"]
        for name, value in (labels or {}).items():
            have = [a for a in attrs if a["type"] == type_ and a["name"] == name
                    and a["noteId"] == note_id]
            for extra in have[1:]:
                self._do("DELETE", f"/attributes/{extra['attributeId']}")
            if have and type_ == "label":
                self._do("PATCH", f"/attributes/{have[0]['attributeId']}", json={"value": value})
                continue
            if have:
                self._do("DELETE", f"/attributes/{have[0]['attributeId']}")
            self._do("POST", "/attributes", json={"noteId": note_id, "type": type_,
                                                  "name": name, "value": value})

    def call_sync(self, verb: str, args: dict[str, Any], **_: Any) -> Any:
        a = dict(args)
        if verb == "note_get":
            out = self._do("GET", f"/notes/{a['note_id']}").json()
            if a.get("content", True) is not False:
                out["content"] = self._do("GET", f"/notes/{a['note_id']}/content").text
            return out
        if verb == "note_create":
            body = {"parentNoteId": a.get("parent", "root"), "noteId": a["note_id"],
                    "title": a["title"], "type": a.get("type", "text"),
                    "content": a.get("content", "")}
            if a.get("mime"):
                body["mime"] = a["mime"]
            self._do("POST", "/create-note", json=body)
            self._labels(a["note_id"], a.get("labels"))
            return {"note_id": a["note_id"], "created": True}
        if verb == "note_update":
            fields = {k: a[k] for k in ("title", "mime") if a.get(k) is not None}
            if fields:
                self._do("PATCH", f"/notes/{a['note_id']}", json=fields)
            if a.get("content") is not None:
                self._do("PUT", f"/notes/{a['note_id']}/content", content=a["content"].encode(),
                         headers={"Content-Type": "text/plain"})
            self._labels(a["note_id"], a.get("labels"))
            return {"note_id": a["note_id"]}
        if verb == "attr_set":
            self._labels(a["note_id"], {a["name"]: a.get("value", "")}, a.get("type", "label"))
            return {}
        if verb == "attr_delete":
            for attr in self._do("GET", f"/notes/{a['note_id']}").json()["attributes"]:
                if attr["name"] == a["name"] and attr["noteId"] == a["note_id"]:
                    self._do("DELETE", f"/attributes/{attr['attributeId']}")
            return {}
        if verb == "note_delete":
            self._do("DELETE", f"/notes/{a['note_id']}")
            return {"deleted": True}
        if verb == "note_search":
            return self._do("GET", "/notes", params={"search": a["query"],
                                                     "limit": a.get("limit", 50)}).json()
        raise Refused(f"the test adapter has no {verb}")
