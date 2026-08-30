"""The Penpot credential awm holds on each person's behalf.

Penpot keeps its own accounts and has no "trust the proxy" mode the way the
shared Trilium vault does, so an awm session alone cannot open a design file.
Rather than hand every person a second password, awm keeps one *for* them: a
random string no human is ever shown, recorded when the account is created,
replaced every night, and exchanged for a Penpot session on demand.

**Why the session is fetched and not forged.** Penpot's ``auth-token`` cookie is
a JWE over a transit-encoded payload, keyed by a derivation of
``PENPOT_SECRET_KEY``, and it carries only a *session id* — ``wrap-authz`` then
looks that id up as a row in ``http_session_v2``. Minting one from outside means
reimplementing two Penpot internals and opening a Postgres that publishes no
port. Calling Penpot's own ``login-with-password`` on loopback gets the same
cookie through the front door, which is already how ``penpot-view``
authenticates its service account.

**Why the token is cached.** Penpot's session garbage collector only deletes
from the retired v1 table, so a v2 session never expires on its own. A cached
token therefore stays good until something deletes its row — and the only thing
that does is ``update-profile-password``, which calls ``invalidate-others``.
That makes a rotation the one invalidation that matters, and it is ours, so the
cache is dropped in the same breath.

**When the stored password and Penpot's disagree.** The stored value is what a
rotation offers Penpot as its *old* password, so a drift wedges rotation
permanently and there is no HTTP path back: ``update-profile-password`` needs the
old password it no longer has, and recovery-by-email is refused at the edge. The
repair is host-side, on the box running the stack::

    docker compose -p awm-penpot \\
        -f /etc/awm/penpot/docker-compose.yml \\
        -f /etc/awm/penpot/docker-compose.sirius.yml \\
        exec -T penpot-backend python3 manage.py update-profile -e <email> -p <new>
    awm auth penpot-record --username <name> --email <email> --password <new>

Both halves, in that order. Recording without resetting Penpot leaves the same
drift pointing the other way. ``update-profile`` and not
``update-profile-password``: the latter is the RPC, which needs the old password
nobody has; ``manage.py`` writes the row directly over the backend's PREPL, sets
only the keys it was given, and does not end anyone's live sessions.

``scripts/sirius/add-user.sh`` runs exactly these two steps by itself when it
finds a Penpot profile that awm holds no credential for, so re-running it for a
person is the supported repair and this is what it does.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from typing import Any

import httpx

log = logging.getLogger("awm.auth.penpot")

#: Penpot's frontend nginx on this host. The stack publishes it on loopback
#: only (Docker's DNAT is consulted before ufw's filter rules, so a bare ``-p``
#: mapping would be public), which is why this default is a localhost port and
#: not the edge's public origin.
_BASE_URL_ENV = "PENPOT_BASE_URL"
_DEFAULT_BASE_URL = "http://localhost:9001"

RPC_PREFIX = "/api/rpc/command"
LOGIN_PATH = f"{RPC_PREFIX}/login-with-password"
CHANGE_PASSWORD_PATH = f"{RPC_PREFIX}/update-profile-password"

#: The name Penpot sets, and the only credential its exporter accepts.
COOKIE_NAME = "auth-token"

LOGIN_TIMEOUT = float(os.environ.get("PENPOT_LOGIN_TIMEOUT", "15"))

#: Penpot's ``app.auth.passwords/validate-password`` demands 8+ characters with
#: at least one lowercase, one uppercase, one digit and one special — so a bare
#: ``token_urlsafe`` is rejected roughly whenever it happens to draw no digit.
#: The alphabet omits quotes, backslashes and whitespace: this value is spliced
#: into JSON, into a shell argument during provisioning, and into Penpot's
#: ``word-string`` schema, and each of those has an opinion about those bytes.
_LOWER = "abcdefghijkmnopqrstuvwxyz"
_UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_DIGIT = "23456789"
_SPECIAL = "!#%*+-=?@^_"
_ALPHABET = _LOWER + _UPPER + _DIGIT + _SPECIAL
_LENGTH = 28


class PenpotError(RuntimeError):
    """A Penpot call did not do what was asked."""


def base_url() -> str:
    return (os.environ.get(_BASE_URL_ENV) or _DEFAULT_BASE_URL).rstrip("/")


def new_password() -> str:
    """A password Penpot will accept, that no human will ever type."""
    while True:
        pw = "".join(secrets.choice(_ALPHABET) for _ in range(_LENGTH))
        if (any(c in _LOWER for c in pw) and any(c in _UPPER for c in pw)
                and any(c in _DIGIT for c in pw)
                and any(c in _SPECIAL for c in pw)):
            return pw


# ---------------------------------------------------------------------------
# The RPC leg
# ---------------------------------------------------------------------------

#: Penpot's RPC layer parses ``application/json`` request bodies with a
#: kebab-casing key function and answers in JSON when asked to, so none of the
#: hand-rolled transit codec ``penpot-view`` needs for the exporter is required
#: here — every call on this path is a plain map of strings.
_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json",
}


async def _rpc(path: str, payload: dict[str, Any], *,
               token: str | None = None) -> httpx.Response:
    url = f"{base_url()}{path}"
    cookies = {COOKIE_NAME: token} if token else None
    try:
        async with httpx.AsyncClient(timeout=LOGIN_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=_HEADERS,
                                     cookies=cookies)
    except httpx.HTTPError as exc:
        raise PenpotError(f"{path} against {base_url()} failed: {exc}") from exc
    # Any 2xx, not 200. ``update-profile-password`` returns nil, which Penpot's
    # response formatter renders as **204 with no body** — so a check for 200
    # reads a successful rotation as a failure, retries it with a password
    # Penpot has already replaced, and wedges the credential it was maintaining.
    # Found against a real stack; no stub would have said so.
    if not 200 <= resp.status_code < 300:
        raise PenpotError(
            f"{path} answered HTTP {resp.status_code}: {_error_code(resp)}")
    return resp


def _error_code(resp: httpx.Response) -> str:
    """Penpot's own error code, which is the whole diagnosis on this path.

    ``old-password-not-match`` means the stored credential has drifted;
    ``weak-password`` means the generator broke. Both are unrecoverable inside
    the loop and both must reach the operator by name — see the module
    docstring for the repair.
    """
    try:
        body = resp.json()
    except ValueError:
        return "(unparseable body)"
    if isinstance(body, dict):
        return str(body.get("code") or body.get("type") or body)
    return str(body)


async def login(email: str, password: str) -> str:
    """Exchange a Penpot credential for a session token."""
    resp = await _rpc(LOGIN_PATH, {"email": email, "password": password})
    token = resp.cookies.get(COOKIE_NAME)
    if not token:
        raise PenpotError(
            f"{LOGIN_PATH} answered HTTP 200 but set no {COOKIE_NAME!r} cookie")
    return token


async def change_password(token: str, old: str, new: str) -> None:
    """Replace a profile's password, authenticated as that profile.

    Penpot invalidates every *other* session for the profile as it does this,
    which is why the caller must drop its cached token in the same step.
    """
    await _rpc(CHANGE_PASSWORD_PATH, {"oldPassword": old, "password": new},
               token=token)


# ---------------------------------------------------------------------------
# The per-user session cache
# ---------------------------------------------------------------------------

#: username → Penpot session token. In-memory only: a restart costs one login
#: per person on their next page load, and persisting it would put a live
#: foreign credential on disk for no gain.
_sessions: dict[str, str] = {}

#: One lock per user, so a burst of asset requests behind a single cold page
#: load does not stampede Penpot with a login each.
_locks: dict[str, asyncio.Lock] = {}


def _lock_for(username: str) -> asyncio.Lock:
    lock = _locks.get(username)
    if lock is None:
        lock = _locks[username] = asyncio.Lock()
    return lock


def cached(username: str) -> str | None:
    return _sessions.get(username)


def forget(username: str) -> None:
    """Drop the cached token — after a rotation, or when it stopped working."""
    _sessions.pop(username, None)


async def session_for(username: str, credential: dict[str, Any], *,
                      refresh: bool = False,
                      stale_token: str | None = None) -> str:
    """A usable Penpot session token for ``username``.

    ``refresh`` skips the cache unconditionally — the ops escape hatch.

    ``stale_token`` is the caller's evidence that a *particular* token has
    stopped working, and it makes the re-login conditional on the cache still
    holding that same token. A rotation deletes the row an old cookie named, so
    several of a page's requests can fail at once carrying the same dead value;
    without this each of them would drive its own login, and Penpot rate-limits
    logins globally. With it, the first one logs in and the rest are handed its
    result.
    """
    async with _lock_for(username):
        if not refresh:
            token = _sessions.get(username)
            if token and (stale_token is None or token != stale_token):
                return token
        token = await login(credential["email"], credential["password"])
        _sessions[username] = token
        log.info("auth: minted a Penpot session for %s", username)
        return token
