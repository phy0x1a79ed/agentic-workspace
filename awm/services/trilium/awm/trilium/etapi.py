"""Trilium's external REST API, reached over loopback with no credential.

**There is no token any more, and that is a consequence rather than a
shortcut.** Trilium's own authentication is off on this deployment — the awm
edge is the only way to reach the child, so a second gate would ask the same
question twice — and upstream's ETAPI guard stands down with it
(`etapi_utils.ts` admits when `noAuthentication` is set). So this service holds
no credential at all, and the whole token store it used to keep is gone: an
unforgeable one already sits in front of the process.

That is exactly why `/etapi/` is **not** on the edge's forwarded path list. What
makes these calls safe is that they come from inside, over loopback, from the
supervisor. Forwarding the same surface to a browser would hand vault-origin
JavaScript an unauthenticated API to the shared vault.

**The internal API is still out of reach, for an unchanged reason.** `POST
/api/revisions/{id}/restore` wants an express session, and this service opens
none. That is why `vault.restore` restores a whole-vault snapshot rather than a
single note revision — see its docstring. Putting one note back is one click in
Trilium's own revisions dialog, where the person already is.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from awm import config

#: Every ETAPI call is against a loopback node on the same host. Generous
#: because an export of a large vault is a zip built in one request, and a
#: backup is a whole-database copy under the sync mutex.
TIMEOUT_S = float(os.environ.get("TRILIUM_ETAPI_TIMEOUT_S", "300"))


class EtapiError(RuntimeError):
    """Trilium answered, and said no."""


# -- the client -------------------------------------------------------------


class Etapi:
    """The vault's ETAPI. Every method raises rather than returning a status."""

    @property
    def base(self) -> str:
        return config.VAULT_URL

    def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            r = httpx.request(method, f"{self.base}{path}",
                              timeout=TIMEOUT_S, **kw)
        except httpx.HTTPError as e:
            raise EtapiError(f"{method} {path}: {e}") from e
        if r.status_code == 401:
            raise EtapiError(
                f"{method} {path} -> 401. The vault is asking for credentials, "
                f"which means Trilium's own authentication is on — check "
                f"TRILIUM_EDGE_ONLY and restart the service.")
        if r.status_code >= 400:
            raise EtapiError(f"{method} {path} -> {r.status_code}: {r.text[:400]}")
        return r

    def app_info(self) -> dict:
        return self._request("GET", "/etapi/app-info").json()

    def backup(self, name: str) -> None:
        """Ask Trilium to copy its database. Returns nothing, because ETAPI
        answers 204 — the caller finds the file by looking."""
        self._request("PUT", f"/etapi/backup/{name}")

    def export_zip(self, note_id: str = "root", fmt: str = "markdown") -> bytes:
        return self._request(
            "GET", f"/etapi/notes/{note_id}/export", params={"format": fmt}).content

    def save_revision(self, note_id: str, description: str = "") -> None:
        self._request("POST", f"/etapi/notes/{note_id}/revision",
                      json={"description": description})

    def revisions(self, note_id: str) -> list[dict]:
        return self._request("GET", f"/etapi/notes/{note_id}/revisions").json()

    # -- branches ------------------------------------------------------------
    #
    # A note's place in the tree is its branch, and a note may have several. So
    # "move X under Y" is create-then-delete, in that order: a note's last
    # branch takes the note with it when it goes.

    def branch_id(self, note_id: str, parent_note_id: str) -> str:
        """Trilium composes a branch's ID from its two ends, so it can be named
        without being looked up first."""
        return f"{parent_note_id}_{note_id}"

    def put_branch(self, note_id: str, parent_note_id: str) -> dict:
        """Place ``note_id`` under ``parent_note_id``. Idempotent — upstream
        answers 200 with the existing branch rather than refusing."""
        return self._request("POST", "/etapi/branches",
                             json={"noteId": note_id,
                                   "parentNoteId": parent_note_id}).json()

    def delete_branch(self, branch_id: str) -> None:
        """Unplace. Quiet (204) for a branch that is already gone."""
        self._request("DELETE", f"/etapi/branches/{branch_id}")

    def create_note(self, *, parent_note_id: str, title: str, type: str,
                    content: str, mime: str | None = None) -> dict:
        return self._request("POST", "/etapi/create-note", json={
            "parentNoteId": parent_note_id, "title": title, "type": type,
            "content": content, **({"mime": mime} if mime else {}),
        }).json()

    def note(self, note_id: str) -> dict:
        return self._request("GET", f"/etapi/notes/{note_id}").json()

    def children(self, note_id: str) -> list[dict]:
        """Every direct child of a note, one GET each.

        Deliberately not :meth:`search`. A title reaches Trilium's search
        grammar as part of a query string, where a quote inside it changes
        what is being asked, and the answer would then be a fuzzy match. This
        is what :meth:`upsert_note` matches against before it overwrites a
        body on a vault everybody shares, so it has to be exact.
        """
        return [self.note(child)
                for child in self.note(note_id).get("childNoteIds") or []]

    def note_content(self, note_id: str) -> str:
        return self._request("GET", f"/etapi/notes/{note_id}/content").text

    def set_content(self, note_id: str, content: str) -> None:
        """Replace a note's body. ETAPI answers 204, so there is nothing to
        return -- read it back with :meth:`note_content` for proof."""
        self._request("PUT", f"/etapi/notes/{note_id}/content",
                      headers={"Content-Type": "text/plain"},
                      content=content.encode("utf-8"))

    def upsert_note(self, *, parent_note_id: str, title: str, content: str,
                    type: str = "text", mime: str | None = None) -> dict:
        """Create the note with exactly this title under this parent, or
        replace the body of the one already there.

        Refuses when the parent holds several notes of that title. A shared
        vault has no owner to ask, and the wrong guess silently discards
        somebody's writing.
        """
        matches = [n for n in self.children(parent_note_id)
                   if n.get("title") == title]
        if len(matches) > 1:
            ids = ", ".join(n.get("noteId", "?") for n in matches)
            raise EtapiError(
                f"{len(matches)} notes under {parent_note_id} are titled "
                f"{title!r} ({ids}) -- refusing to guess which one to "
                f"overwrite")
        if not matches:
            made = self.create_note(parent_note_id=parent_note_id, title=title,
                                    type=type, content=content, mime=mime)
            return {"note_id": made["note"]["noteId"],
                    "created": True, "changed": True}
        note_id = matches[0]["noteId"]
        if self.note_content(note_id) == content:
            return {"note_id": note_id, "created": False, "changed": False}
        self.set_content(note_id, content)
        return {"note_id": note_id, "created": False, "changed": True}

    def search(self, query: str, **params: Any) -> dict:
        return self._request("GET", "/etapi/notes",
                             params={"search": query, **params}).json()


def client() -> Etapi:
    """The vault's ETAPI. No arguments and no credential: there is one vault,
    and reaching it is a matter of being inside this process."""
    return Etapi()
