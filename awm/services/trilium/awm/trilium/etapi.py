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


def client() -> Etapi:
    """The vault's ETAPI. No arguments and no credential: there is one vault,
    and reaching it is a matter of being inside this process."""
    return Etapi()
