"""Sign-in through alcor's approver with the passkey (fleet.md R84, users.md R130).

A browser with no session goes to the approver's ``/portal_login`` with this page
as ``next``. After the passkey the approver sends it back to :data:`CALLBACK` with
a ``portal_login`` grant that alcor signed for this node, this host and this path.
The edge checks the grant offline against alcor's grant key, spends it once, and
starts its own session. It never calls alcor.
"""

from __future__ import annotations

import base64
import json
import os
import time
from urllib.parse import quote, urlsplit

from verify_grant import GrantInvalid, load_public_key, verify_grant

from awm.config import edges

CALLBACK = "/__auth/portal"
GRANT_KEY_ENV = "AWM_APPROVER_GRANT_PUBKEY"
ORIGIN_ENV = "AWM_APPROVER_ORIGIN"
DEFAULT_ORIGIN = "https://auth.tony-xy-liu.com"


def approver_origin() -> str:
    return (os.environ.get(ORIGIN_ENV) or DEFAULT_ORIGIN).rstrip("/")


def login_url(public_url: str) -> str:
    """The approver's login page, sending the browser back to ``public_url`` after."""
    return f"{approver_origin()}/portal_login?next={quote(public_url, safe='')}"


def encode_grant(envelope: dict) -> str:
    """The URL form of a grant envelope: base64url of its JSON."""
    return base64.urlsafe_b64encode(json.dumps(envelope, separators=(",", ":")).encode()
                                    ).decode().rstrip("=")


def _decode(raw: str) -> dict:
    try:
        out = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (ValueError, TypeError):
        raise GrantInvalid("the grant is not base64url JSON") from None
    if not isinstance(out, dict):
        raise GrantInvalid("the grant is not an envelope")
    return out


def local_path(raw: str | None) -> str | None:
    """``raw`` when it is a path on this origin with no query or fragment, else None."""
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return None
    if any(c in raw for c in ("\\", "\n", "\r", "\t", "\x00", "?", "#")):
        return None
    return raw


def host_of(host_header: str | None) -> str:
    return urlsplit(f"//{host_header or ''}").hostname or ""


class Grants:
    """Checks portal grants and remembers each spent one until it expires."""

    def __init__(self, clock=time.time) -> None:
        self.clock = clock
        self._spent: dict[str, float] = {}

    def accept(self, raw: str, host: str, path: str) -> str:
        """The grant's subject, once it is alcor's, for this node, host and path, and unspent."""
        key = (os.environ.get(GRANT_KEY_ENV) or "").strip()
        if not key:
            raise GrantInvalid(f"no {GRANT_KEY_ENV} on this node")
        now = self.clock()
        body = verify_grant(_decode(raw), load_public_key(key), kind="portal_login",
                            node=edges.here(), payload={"site": host, "return": path},
                            now=now)
        self._spent = {g: t for g, t in self._spent.items() if t > now}
        if body["grant_id"] in self._spent:
            raise GrantInvalid("the grant is spent")
        self._spent[body["grant_id"]] = body["not_after"]
        return str(body.get("subject") or "")
