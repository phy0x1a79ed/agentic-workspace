"""Reading the library from Zotero's own service.

**Zotero is already a sync system, and this is a reader of it, not a second
one.** Every desktop signed into the account uploads about three seconds after
an edit and is pushed other machines' changes over a websocket. So the library
this reads is the same copy every client converges on, and awm never has to
decide which desktop to trust, never has to wait for one to wake up, and cannot
make two machines disagree.

That is a change from how this service began. It read the desktop's own copy of
this interface over `ssh` and `curl`, which was sound while there was one
machine and became unsound the moment there were two: a desktop that had not
finished syncing reported a lower version, and the mirror rewrote itself
backwards from it.

**Three facts that decided the move, none of them guessable.**

*A locally saved item is invisible until it uploads.* An item Zotero has not yet
sent carries version `0`, and the version a library reports is the one the
service assigned, so a paper saved a second ago is in no `since` window at all.
Reading a desktop sooner than the service therefore buys nothing: the paper is
not there to read.

*The desktop cannot report a deletion.* Its copy of this interface has no
`deleted` route — not an empty one, absent. This one has it. The mirror does not
call it, and the reason is worth recording: with one reader against the
authoritative copy, re-reading a library that moved makes absence mean what it
says, and a library is a handful of pages. Reading only what changed would be
fewer requests and would need the parent of every changed attachment fetched
back, which is the class of bug that turns one missing file into a paper that
never gets one. The route is there when a library grows large enough to want it.

*This module only ever issues GET.* The method is not a parameter anywhere
below, and `_get` is the single seam. The key this runs under may hold write
access for something later, and on a public host the difference between "does
not write" and "cannot write from here" is worth the two lines it costs: a
deletion made with that key propagates to every machine the account syncs.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Iterator

log = logging.getLogger("awm.zotero.source")

#: Where the library lives. Overridable only so a test can point somewhere else.
API = os.environ.get("ZOTERO_API_BASE", "https://api.zotero.org").rstrip("/")

#: The key. Read-only is all this needs; see the module docstring.
KEY = os.environ.get("ZOTERO_API_KEY", "").strip()

#: The account, as the service numbers it. Discovered from the key when unset,
#: because a key already knows whose it is and a second setting is a second
#: thing to get wrong.
USER = os.environ.get("ZOTERO_USER", "").strip()

#: What the bundle and every note in the vault call the personal library.
#:
#: **Not the account number, and that is a migration rather than a preference.**
#: The desktop's copy of this interface numbers the signed-in user `0`, because
#: locally there is only one. Every note this mirror has ever written carries
#: `#zoteroKey=users/0/<key>`, and the bundle identifies its items the same way.
#: Switching to the account's real number would change the identity of all 823
#: of them at once: the pass would find no note for any new reference, create a
#: second copy of the whole library, and then delete the first for being absent.
#:
#: So `users/0` stays the name, and the number appears only in a URL. Groups
#: need none of this — a group is numbered the same by both.
PERSONAL = "users/0"

#: Whether to mirror the group libraries as well as the personal one.
#:
#: On by default, and that default is load-bearing rather than generous. On this
#: account the personal library holds almost no attached papers and one shared
#: group holds most of them, so mirroring the personal library alone produces a
#: bibliography that looks broken with nothing anywhere saying why.
GROUPS = os.environ.get("ZOTERO_GROUPS", "1") not in ("0", "false", "no")

#: One page of items. The service's own cap is 100.
PAGE = 100

TIMEOUT_S = float(os.environ.get("ZOTERO_TIMEOUT_S", "30"))

#: The version of the interface this speaks. Pinned rather than left to default,
#: because the default is whatever the service decides it is today.
API_VERSION = "3"


class ZoteroUnavailable(RuntimeError):
    """The library could not be reached.

    Its own class because it is an ordinary outcome rather than a defect: a
    network drops, a service restarts. A tick that hits this reports and waits.
    """


class ZoteroError(RuntimeError):
    """Zotero answered, and said no."""


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body else None


# -- the connection ----------------------------------------------------------
#
# One socket, held across calls and rebuilt when the far end drops it. A pass
# over a large library is dozens of requests, and a handshake each is most of
# the wall clock.

_conn: http.client.HTTPSConnection | None = None
_lock = threading.Lock()
#: Earliest the next request may go out. The service asks for politeness by
#: header rather than by refusing, so honouring it is on us.
_not_before = 0.0


def _connect() -> http.client.HTTPSConnection:
    global _conn
    if _conn is None:
        parsed = urllib.parse.urlsplit(API)
        _conn = http.client.HTTPSConnection(parsed.netloc, timeout=TIMEOUT_S)
    return _conn


def _drop() -> None:
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except OSError:
            pass
        _conn = None


def _wait_turn() -> None:
    delay = _not_before - time.monotonic()
    if delay > 0:
        log.info("zotero: holding off %.1fs, as asked", delay)
        time.sleep(min(delay, 300.0))


def _note_backoff(headers: dict[str, str]) -> None:
    """Record how long the service asked us to wait.

    `Backoff` means "keep going, but slower"; `Retry-After` comes with a refusal.
    Both are seconds, both are advisory in the sense that nothing enforces them,
    and ignoring either is how a well-behaved client becomes a blocked one.
    """
    global _not_before
    for name in ("backoff", "retry-after"):
        raw = headers.get(name)
        if raw:
            try:
                _not_before = max(_not_before, time.monotonic() + float(raw))
            except ValueError:
                pass


def _get(path: str, params: dict[str, Any] | None = None) -> Response:
    """One read. The only request this module makes, and the only one it can.

    There is no method parameter here or anywhere above it. See the module
    docstring: the key may carry write access for something later, and this is
    what keeps that a deliberate change rather than an accident.
    """
    if not KEY:
        raise ZoteroError(
            "no ZOTERO_API_KEY: the mirror reads the library from Zotero's own "
            "service, so it needs a key. Make a read-only one at "
            "zotero.org/settings/keys and put it where this node reads its "
            "settings.")
    query = urllib.parse.urlencode(params or {})
    url = f"{path}?{query}" if query else path
    headers = {"Zotero-API-Key": KEY, "Zotero-API-Version": API_VERSION,
               "Accept": "application/json", "Connection": "keep-alive"}

    with _lock:
        _wait_turn()
        for attempt in (1, 2):
            try:
                conn = _connect()
                conn.request("GET", url, headers=headers)
                raw = conn.getresponse()
                body = raw.read()
                got = Response(status=raw.status,
                               headers={k.lower(): v for k, v in
                                        raw.getheaders()},
                               body=body)
                break
            except (http.client.RemoteDisconnected,
                    http.client.BadStatusLine,
                    http.client.CannotSendRequest,
                    ConnectionError) as e:
                # A keep-alive socket the far end closed between requests
                # surfaces here on the *next* request. That is housekeeping, not
                # an outage, so the first one is retried without comment.
                _drop()
                if attempt == 2:
                    raise ZoteroUnavailable(
                        f"cannot reach {API}: {e}") from e
            except OSError as e:
                _drop()
                raise ZoteroUnavailable(f"cannot reach {API}: {e}") from e

    _note_backoff(got.headers)
    return got


def _checked(res: Response, what: str) -> Response:
    if res.status == 403:
        raise ZoteroError(
            f"{what}: refused. The key does not reach this library — check it "
            f"has read access to the personal library and to the groups.")
    if res.status == 429 or res.status == 503:
        raise ZoteroUnavailable(
            f"{what}: asked to back off ({res.status}); "
            f"waiting {res.headers.get('retry-after', '?')}s")
    if res.status >= 400:
        raise ZoteroError(f"{what}: {res.status} "
                          f"{res.body.decode('utf-8', 'replace')[:200]}")
    return res


# -- what the account holds --------------------------------------------------


def whoami() -> dict[str, Any]:
    """The account behind the key, and what the key may do.

    Also the cheapest liveness check there is, and the one `status` uses: it
    needs no library and no version, so it answers on an account with nothing
    in it.
    """
    got = _checked(_get("/keys/current"), "key")
    body = got.json() or {}
    access = body.get("access") or {}
    return {"user_id": str(body.get("userID") or ""),
            "username": body.get("username") or "",
            "writes": bool((access.get("user") or {}).get("write")),
            "groups_read": bool(((access.get("groups") or {}).get("all")
                                 or {}).get("library"))}


def user() -> str:
    """The personal library's id, discovered once from the key if unset."""
    global USER
    if not USER:
        USER = whoami()["user_id"]
    return USER


def personal() -> str:
    """The personal library's name, which is not its address. See `PERSONAL`."""
    return PERSONAL


def name_of(path: str) -> str:
    """What a library is called, given the address something used for it.

    The inverse of `path_of`, and it exists because the event stream names its
    topics by address. Without it the stream reports the personal library under
    a name nothing else in this service uses, and anything that ever keys off
    that name silently matches nothing.
    """
    if not path.startswith("users/"):
        # A group is numbered the same by both, so this answers without asking
        # anybody anything. That matters: this is called from the event stream,
        # which can deliver a frame before the first request has been made.
        return path
    try:
        mine = user()
    except (ZoteroError, ZoteroUnavailable):
        # The account is not known yet and this is a naming question, not a
        # reason to fail. The address is a worse name than `users/0` and a
        # perfectly correct one.
        return path
    return PERSONAL if path == f"users/{mine}" else path


def path_of(library: str) -> str:
    """Where a library is reached, given what it is called.

    The one place the two spellings meet. Everything above this works in names
    so that a name can go on a note and stay put; everything below works in
    addresses so that a request can be made.
    """
    return f"users/{user()}" if library == PERSONAL else library


def groups() -> list[dict]:
    """The shared libraries this account is a member of."""
    if not GROUPS:
        return []
    got = _checked(_get(f"/{path_of(PERSONAL)}/groups", {"limit": 100}), "groups")
    return [{"id": f"groups/{g['id']}",
             "name": (g.get("data") or {}).get("name") or str(g["id"])}
            for g in got.json() or []]


def libraries() -> list[dict]:
    """Everything to mirror: the personal library, then each group."""
    return [{"id": personal(), "name": "My Library"}, *groups()]


def library_version(library: str | None = None) -> int:
    """Where one library is now. One request, and the whole cost of a tick that
    has nothing to do."""
    got = _checked(_get(f"/{path_of(library or PERSONAL)}/items", {"limit": 1}),
                   "library version")
    return int(got.headers.get("last-modified-version") or 0)


def versions() -> dict[str, int]:
    """Every library's version, which together are the sync cursor.

    A dict rather than one number because the libraries move independently: a
    paper added to a shared group changes nothing about the personal library.
    """
    return {lib["id"]: library_version(lib["id"]) for lib in libraries()}


def _paged(path: str, params: dict[str, Any] | None = None) -> Iterator[dict]:
    start = 0
    while True:
        got = _checked(
            _get(path, {**(params or {}), "limit": PAGE, "start": start,
                        "format": "json"}),
            f"GET {path}")
        page = got.json() or []
        yield from page
        total = int(got.headers.get("total-results") or 0)
        start += PAGE
        if start >= total or not page:
            return


def collections(library: str | None = None,
                since: int | None = None) -> list[dict]:
    params = {"since": since} if since else {}
    return list(_paged(f"/{path_of(library or PERSONAL)}/collections", params))


def items(library: str | None = None, since: int | None = None) -> list[dict]:
    """Every item, or everything that moved since a version.

    Not `/items/top`: an attachment is where a file is recorded and a child note
    is an annotation somebody wrote. Folding the three together belongs to
    whatever builds the bundle, which can see all of them.

    **`since` is a cursor over what the service assigned, not over wall clock.**
    An item created locally and not yet uploaded carries version `0` and appears
    in no `since` window. That is a fact about the account rather than a
    limitation here, and it is why nothing tries to read a desktop sooner.
    """
    params = {"since": since} if since else {}
    return list(_paged(f"/{path_of(library or PERSONAL)}/items", params))
