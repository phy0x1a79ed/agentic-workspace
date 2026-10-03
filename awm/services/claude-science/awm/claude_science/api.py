"""Acting on the workbench through its own HTTP API, as its owner.

Some of the workbench's configuration has no CLI: the local MCP servers it
launches, and the host directories it will let Claude read. Both were assumed
to be browser-only work, and neither is — the UI reaches them over the same
loopback API this module speaks, and the service can already mint the
credential that API wants.

**Owning the session.** ``claude-science url`` prints a single-use nonce, good
for about three minutes; posting it to ``/api/auth/nonce`` exchanges it for the
owner token. The daemon accepts that token two ways, and the difference is not
cosmetic: as a cookie it is a *browser* and so must also carry an ``Origin`` the
daemon recognises plus a matching double-submit CSRF header, while as
``Authorization: Bearer`` it is a program and the CSRF hook is skipped outright.
We are a program. That is the whole reason this module is thirty lines and not a
cookie-jar-plus-CSRF-dance.

A nonce burns on use, so a session is minted per call rather than cached; three
minutes is not a lifetime worth managing, and a stale one fails as a 401 far
from here.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from awm.claude_science import daemon

log = logging.getLogger("awm.claude_science.api")

#: The nonce inside `claude-science url` output.
NONCE_RE = re.compile(r"nonce=([a-f0-9]+)")

#: Cookie the nonce exchange sets; its value is also the bearer token.
AUTH_COOKIE = "operon_auth"


class WorkbenchError(RuntimeError):
    """A refusal from the daemon, carrying what it said.

    The daemon's own validation is the point of going through the API rather
    than writing its state files — a symlinked path, a read-only grant under an
    existing read-write one, a name that collides with a bundled server — so its
    ``detail`` is the useful part of the failure and is preserved verbatim.
    """

    def __init__(self, message: str, *, status: int | None = None,
                 detail: str | None = None):
        super().__init__(message)
        self.status = status
        self.detail = detail


def nonce_from(login_url: str) -> str:
    """The nonce inside a `claude-science url` line."""
    match = NONCE_RE.search(login_url)
    if not match:
        raise WorkbenchError(
            f"no nonce in claude-science url output: {login_url[:120]!r}")
    return match.group(1)


def base_url() -> str:
    return f"http://127.0.0.1:{daemon.PORT}"


class Owner:
    """An authenticated client for one exchange's worth of owner token."""

    def __init__(self, token: str, *, timeout: float = 60.0,
                 transport: httpx.BaseTransport | None = None):
        self.token = token
        self._client = httpx.Client(
            base_url=base_url(), timeout=timeout, transport=transport,
            headers={"authorization": f"Bearer {token}"},
        )

    def __enter__(self) -> Owner:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def request(self, method: str, path: str, **kw: Any) -> Any:
        try:
            resp = self._client.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise WorkbenchError(
                f"{method} {path} failed: {type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 400:
            detail = None
            try:
                body = resp.json()
                if isinstance(body, dict):
                    detail = body.get("detail") or body.get("error")
            except ValueError:
                detail = (resp.text or "").strip()[:200]
            raise WorkbenchError(
                f"{method} {path} → {resp.status_code}"
                + (f": {detail}" if detail else ""),
                status=resp.status_code, detail=detail)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {"text": resp.text}

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> Any:
        return self.request("POST", path, **kw)

    def delete(self, path: str, **kw: Any) -> Any:
        return self.request("DELETE", path, **kw)


def connect(login_url: str, *, timeout: float = 60.0,
            transport: httpx.BaseTransport | None = None) -> Owner:
    """Exchange a fresh sign-in URL for an authenticated client."""
    nonce = nonce_from(login_url)
    with httpx.Client(base_url=base_url(), timeout=timeout,
                      transport=transport, follow_redirects=False) as client:
        try:
            resp = client.post("/api/auth/nonce", data={"nonce": nonce,
                                                        "dest": "/"})
        except httpx.HTTPError as exc:
            raise WorkbenchError(
                f"nonce exchange failed: {type(exc).__name__}: {exc}") from exc
        # The route answers with a redirect into the app on success; what we
        # want is the cookie it sets on the way.
        token = resp.cookies.get(AUTH_COOKIE)
        if not token:
            raise WorkbenchError(
                f"nonce exchange set no {AUTH_COOKIE} cookie "
                f"(HTTP {resp.status_code}) — the nonce may already be spent",
                status=resp.status_code)
    return Owner(token, timeout=timeout, transport=transport)


# ---------------------------------------------------------------------------
# The two surfaces this module exists for
# ---------------------------------------------------------------------------

#: Prefix the daemon puts on a local server's id.
LOCAL_ID_PREFIX = "local:"

def local_store_path():
    """Where the daemon persists local servers.

    Read — never written — because the listing API projects a server without
    its command, args or env, and those are exactly what decides whether a
    re-registration would change anything.
    """
    return daemon.DATA_DIR / "mcp" / "local-mcp.json"


def stored_local_server(name: str) -> dict[str, Any] | None:
    """The persisted definition of one local server, if it exists."""
    try:
        doc = json.loads(local_store_path().read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("local-mcp.json unreadable (%s); treating as empty", exc)
        return None
    for entry in doc.get("servers") or []:
        if entry.get("name") == name:
            return entry
    return None


def list_local_servers(owner: Owner) -> list[dict[str, Any]]:
    """The local (stdio) servers, out of every connector the daemon knows.

    ``/api/mcp-servers`` is the *custom remote* connector table and answers an
    empty list on a node that has none — which reads exactly like "the local
    server you just registered is not there". ``/connectors`` is the union:
    bundled, directory and local-stdio together.
    """
    rows = owner.get("/api/mcp-servers/connectors")
    if isinstance(rows, dict):
        rows = rows.get("connectors")
    return [s for s in (rows or []) if s.get("source") == "local-stdio"]


def probe_tools(owner: Owner, server_id: str) -> dict[str, Any]:
    """Whether the server actually launches and answers ``tools/list``.

    Registering and *connecting* are different facts, and only the second one
    means the connector works: a bad command registers perfectly and then fails
    at connect time, where the error is a string in this payload rather than a
    non-2xx anywhere. Reporting one as the other is how a broken connector gets
    called done.
    """
    try:
        out = owner.get(f"/api/mcp-servers/{server_id}/tool-permissions",
                        params={"source": "local-stdio"}, timeout=90.0)
    except WorkbenchError as exc:
        return {"tools": 0, "error": str(exc)}
    return {"tools": len(out.get("tools") or []), "error": out.get("error")}


def add_local_server(owner: Owner, *, name: str, command: str,
                     args: list[str] | None = None,
                     env: dict[str, str] | None = None,
                     description: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"name": name, "command": command,
                            "args": list(args or []), "env": dict(env or {})}
    if description:
        body["description"] = description
    return owner.post("/api/mcp-servers/local", json=body)


def list_grants(owner: Owner) -> list[dict[str, Any]]:
    out = owner.get("/api/preferences/host-grants")
    return list(out.get("grants") or [])


def add_grant(owner: Owner, *, path: str, mode: str) -> dict[str, Any]:
    return owner.post("/api/preferences/host-grants",
                      json={"path": path, "mode": mode})
