"""Trilium's external REST API, and the one credential this service keeps.

**A token, never a password.** Trilium's login is the per-user identity — it is
the reason one server runs per person — so the service must not hold it. What
it holds is an ETAPI token: `POST /etapi/auth/login` exchanges a password for
one, the password is discarded on the way, and the person can revoke the token
from Trilium's own options screen without changing anything else. A token they
created there themselves works just as well and never puts the password on a
wire at all, which is the documented path.

**The internal API is deliberately out of reach.** `POST
/api/revisions/{id}/restore` is the one operation a person can do in the
browser and this service cannot: `checkApiAuth` wants an express session, and a
session is only obtainable with the password. That is the reason `vault.restore`
restores a whole-vault snapshot rather than a single note revision — see its
docstring. Nothing here works around it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from awm.trilium.instances import Instance

#: Every ETAPI call is against a loopback node on the same host. Generous
#: because an export of a large vault is a zip built in one request, and a
#: backup is a whole-database copy under the sync mutex.
TIMEOUT_S = float(os.environ.get("TRILIUM_ETAPI_TIMEOUT_S", "300"))


class NotAuthorized(RuntimeError):
    """No usable ETAPI token for this user. `trilium authorize` fixes it."""


class EtapiError(RuntimeError):
    """Trilium answered, and said no."""


# -- the token store --------------------------------------------------------


def read_token(inst: Instance) -> str | None:
    try:
        token = inst.token_file.read_text().strip()
    except OSError:
        return None
    return token or None


def store_token(inst: Instance, token: str) -> Path:
    """Write the token 0600, creating the directory 0700.

    The mode is set before the content is written, not after: a token that
    exists world-readable for even an instant has been disclosed.
    """
    path = inst.token_file
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(token.strip() + "\n")
    return path


def forget_token(inst: Instance) -> bool:
    try:
        inst.token_file.unlink()
        return True
    except OSError:
        return False


# -- the client -------------------------------------------------------------


class Etapi:
    """One user's ETAPI. Every method raises rather than returning a status."""

    def __init__(self, inst: Instance, token: str) -> None:
        self.inst = inst
        self._token = token

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.inst.upstream_port}"

    def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        headers = dict(kw.pop("headers", {}))
        headers["Authorization"] = self._token
        try:
            r = httpx.request(method, f"{self.base}{path}", headers=headers,
                              timeout=TIMEOUT_S, **kw)
        except httpx.HTTPError as e:
            raise EtapiError(f"{method} {path}: {e}") from e
        if r.status_code == 401:
            raise NotAuthorized(
                f"Trilium rejected the stored token for {self.inst.user!r}. It was "
                f"probably revoked in the options screen — run `trilium authorize` "
                f"with a new one.")
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


def login(inst: Instance, password: str, token_name: str = "awm") -> str:
    """Exchange a password for a token. The password is not written anywhere.

    Trilium answers 401 for a wrong password and 400 while the database is
    still uninitialized — a vault nobody has set a password on yet has nothing
    to authenticate against, and the first browser visit is what fixes that.
    """
    url = f"http://127.0.0.1:{inst.upstream_port}/etapi/auth/login"
    try:
        r = httpx.post(url, json={"password": password, "tokenName": token_name},
                       timeout=TIMEOUT_S)
    except httpx.HTTPError as e:
        raise EtapiError(f"POST /etapi/auth/login: {e}") from e
    if r.status_code == 401:
        raise NotAuthorized(f"Trilium rejected the password for {inst.user!r}.")
    if r.status_code >= 400:
        raise EtapiError(
            f"POST /etapi/auth/login -> {r.status_code}: {r.text[:400]} "
            f"(a vault whose password has never been set answers here — open "
            f"the instance in a browser first)")
    token = (r.json() or {}).get("authToken")
    if not token:
        raise EtapiError("Trilium accepted the password and returned no token.")
    return token


def client(inst: Instance) -> Etapi:
    token = read_token(inst)
    if not token:
        raise NotAuthorized(
            f"no ETAPI token for {inst.user!r}. Create one in Trilium under "
            f"Options -> ETAPI and pass it to `trilium authorize`, or pass that "
            f"user's password to the same verb to have one issued.")
    return Etapi(inst, token)
