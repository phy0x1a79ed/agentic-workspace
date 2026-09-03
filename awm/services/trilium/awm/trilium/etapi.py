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
import secrets
import string
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


#: Trilium accepts ``[A-Za-z0-9_]{4,128}`` for an entity id and mints 12
#: characters itself. Attributes are the one entity ETAPI makes the *caller*
#: name — `POST /etapi/attributes` marks `attributeId` mandatory — so this is
#: not a convenience but a requirement of the call.
_ID_ALPHABET = string.ascii_letters + string.digits


def new_entity_id(length: int = 12) -> str:
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(length))


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

    def patch_note(self, note_id: str, **fields: Any) -> dict:
        """Change a note's own columns.

        ETAPI accepts only `title`, `type`, `mime` and the creation dates here;
        a body is :meth:`set_content` and a place in the tree is a branch.
        Anything else is dropped before the call rather than after it, so a
        caller gets a refusal it can read instead of a silent no-op.
        """
        allowed = {"title", "type", "mime", "dateCreated", "utcDateCreated"}
        unknown = set(fields) - allowed
        if unknown:
            raise EtapiError(
                f"patch_note cannot set {sorted(unknown)}: ETAPI patches only "
                f"{sorted(allowed)}. A body is set_content, a parent is a branch.")
        body = {k: v for k, v in fields.items() if v is not None}
        if not body:
            return self.note(note_id)
        return self._request("PATCH", f"/etapi/notes/{note_id}", json=body).json()

    def delete_note(self, note_id: str) -> None:
        """Delete a note and its subtree. Quiet for a note already gone."""
        self._request("DELETE", f"/etapi/notes/{note_id}")

    # -- attributes ----------------------------------------------------------
    #
    # A label is a fact about a note; a relation is a link to another. Both are
    # rows in one table, which is why one pair of methods covers them and why
    # `type` is never inferred.

    def attributes(self, note_id: str) -> list[dict]:
        """Every attribute owned by this note, as ETAPI reports them on the
        note itself — there is no list endpoint of its own."""
        return self.note(note_id).get("attributes") or []

    def create_attribute(self, *, note_id: str, type: str, name: str,
                         value: str = "", is_inheritable: bool = False) -> dict:
        if type not in ("label", "relation"):
            raise EtapiError(f"attribute type must be label or relation, not {type!r}")
        return self._request("POST", "/etapi/attributes", json={
            "attributeId": new_entity_id(), "noteId": note_id, "type": type,
            "name": name, "value": value, "isInheritable": bool(is_inheritable),
        }).json()

    def patch_attribute(self, attribute_id: str, value: str) -> dict:
        """Change a label's value. A relation's target is not patchable —
        upstream allows only `position` there — so :meth:`set_attribute`
        replaces one instead."""
        return self._request("PATCH", f"/etapi/attributes/{attribute_id}",
                             json={"value": value}).json()

    def delete_attribute(self, attribute_id: str) -> None:
        self._request("DELETE", f"/etapi/attributes/{attribute_id}")

    def set_attribute(self, *, note_id: str, name: str, value: str = "",
                      type: str = "label", is_inheritable: bool = False) -> dict:
        """Give the note exactly one attribute of this name and type.

        Matched on the note's **own** attributes, never an inherited one: a
        label reaching a note from a template belongs to the template, and
        patching it there would change every note that shares it.

        A relation is replaced rather than patched, because ETAPI will not
        patch a relation's target. Duplicates of the same name are collapsed
        onto the first, since a single-valued field with two rows is a state
        nothing downstream can read.
        """
        own = [a for a in self.attributes(note_id)
               if a.get("name") == name and a.get("type") == type
               and a.get("noteId") == note_id]
        if not own:
            made = self.create_attribute(note_id=note_id, type=type, name=name,
                                         value=value, is_inheritable=is_inheritable)
            return {"attribute_id": made["attributeId"], "created": True,
                    "changed": True}
        for extra in own[1:]:
            self.delete_attribute(extra["attributeId"])
        first = own[0]
        if first.get("value") == value and bool(first.get("isInheritable")) == bool(is_inheritable):
            return {"attribute_id": first["attributeId"], "created": False,
                    "changed": False}
        if type == "relation" or bool(first.get("isInheritable")) != bool(is_inheritable):
            self.delete_attribute(first["attributeId"])
            made = self.create_attribute(note_id=note_id, type=type, name=name,
                                         value=value, is_inheritable=is_inheritable)
            return {"attribute_id": made["attributeId"], "created": False,
                    "changed": True}
        self.patch_attribute(first["attributeId"], value)
        return {"attribute_id": first["attributeId"], "created": False,
                "changed": True}

    def set_attributes(self, *, note_id: str, values: dict[str, str],
                       type: str = "label") -> list[str]:
        """Set several attributes, reading the note once.

        :meth:`set_attribute` re-reads the note to find what it already owns,
        which is right for one attribute and wrong for five: a sync writing
        five citation fields onto eight hundred notes made four thousand
        needless round trips, and every one of them asked the same question.

        Returns the names that actually changed.
        """
        own: dict[str, dict] = {}
        for a in self.attributes(note_id):
            if a.get("type") == type and a.get("noteId") == note_id:
                own.setdefault(a.get("name") or "", a)
        changed = []
        for name, value in values.items():
            existing = own.get(name)
            if existing is None:
                self.create_attribute(note_id=note_id, type=type, name=name,
                                      value=value)
                changed.append(name)
            elif existing.get("value") != value:
                if type == "relation":
                    self.delete_attribute(existing["attributeId"])
                    self.create_attribute(note_id=note_id, type=type,
                                          name=name, value=value)
                else:
                    self.patch_attribute(existing["attributeId"], value)
                changed.append(name)
        return changed

    def clear_attribute(self, *, note_id: str, name: str,
                        type: str = "label") -> int:
        """Remove every attribute of this name the note owns. Returns how many
        went; an inherited one is not the note's to remove and is left."""
        gone = 0
        for a in self.attributes(note_id):
            if (a.get("name") == name and a.get("type") == type
                    and a.get("noteId") == note_id):
                self.delete_attribute(a["attributeId"])
                gone += 1
        return gone

    def label_value(self, note_id: str, name: str) -> str | None:
        for a in self.attributes(note_id):
            if a.get("type") == "label" and a.get("name") == name:
                return a.get("value") or ""
        return None

    # -- attachments ---------------------------------------------------------

    def attachments(self, note_id: str) -> list[dict]:
        return self._request("GET", f"/etapi/notes/{note_id}/attachments").json()

    def create_attachment(self, *, owner_id: str, title: str, mime: str,
                          role: str = "file") -> dict:
        """Make an empty attachment. The bytes go in separately: ETAPI
        validates the create body's `content` as a string, which a PDF is
        not."""
        return self._request("POST", "/etapi/attachments", json={
            "ownerId": owner_id, "title": title, "mime": mime, "role": role,
            "content": "",
        }).json()

    def set_attachment_content(self, attachment_id: str, blob: bytes) -> None:
        """Put the bytes in. `application/octet-stream` is what makes express
        hand the route a Buffer rather than a parsed string, which is the
        difference between a readable PDF and a corrupted one."""
        self._request("PUT", f"/etapi/attachments/{attachment_id}/content",
                      headers={"Content-Type": "application/octet-stream"},
                      content=blob)

    def delete_attachment(self, attachment_id: str) -> None:
        self._request("DELETE", f"/etapi/attachments/{attachment_id}")

    def upsert_attachment(self, *, owner_id: str, title: str, mime: str,
                          blob: bytes, role: str = "file") -> dict:
        """Attach these bytes to this note under this title, replacing what
        was there. Matched on title, because an attachment has no other stable
        name — and re-uploaded only when the size differs, so a periodic sync
        does not rewrite a 20 MB PDF every pass."""
        existing = [a for a in self.attachments(owner_id)
                    if a.get("title") == title]
        for extra in existing[1:]:
            self.delete_attachment(extra["attachmentId"])
        if existing:
            att = existing[0]
            if att.get("contentLength") == len(blob):
                return {"attachment_id": att["attachmentId"], "created": False,
                        "changed": False}
            self.set_attachment_content(att["attachmentId"], blob)
            return {"attachment_id": att["attachmentId"], "created": False,
                    "changed": True}
        made = self.create_attachment(owner_id=owner_id, title=title, mime=mime,
                                      role=role)
        self.set_attachment_content(made["attachmentId"], blob)
        return {"attachment_id": made["attachmentId"], "created": True,
                "changed": True}

    # -- placement -----------------------------------------------------------

    def move_note(self, note_id: str, parent_note_id: str) -> dict:
        """Put the note under this parent and nowhere else.

        Create-then-delete, in that order: a note's last branch takes the note
        with it, so removing the old placement first would delete the note.
        """
        made = self.put_branch(note_id, parent_note_id)
        for old in self.note(note_id).get("parentBranchIds") or []:
            if old != made.get("branchId"):
                self.delete_branch(old)
        return {"note_id": note_id, "branch_id": made.get("branchId"),
                "parent": parent_note_id}

    def search(self, query: str, **params: Any) -> dict:
        return self._request("GET", "/etapi/notes",
                             params={"search": query, **params}).json()


def client() -> Etapi:
    """The vault's ETAPI. No arguments and no credential: there is one vault,
    and reaching it is a matter of being inside this process."""
    return Etapi()
