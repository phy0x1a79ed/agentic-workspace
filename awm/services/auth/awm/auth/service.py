"""Auth service orchestration — rotation policy and the verb handlers.

The service owns one authoritative credential state (via :mod:`awm.auth.store`)
and is the *authority*; the ``httpsfront`` edge only *enforces*, using material
this service hands it (the signing secret and the device token digests) over loopback.

Rotation contract
-----------------
On startup, mint a pair if none is valid or the newest is older than the cadence
(so a restart never leaves a stale-only state — ``events`` is not relied on).
Then a background loop mints every cadence. Each mint:

* a **loud** log line (so an operator/agent watching a dev session sees it),
* a best-effort push of the *login* password to the operator's Discord DM
  (never the peer credential, and never blocking the mint on social being up
  — it runs detached, and retries a transient failure with backoff before
  giving up; see ``h_status``'s ``push_last_*`` fields),
* prunes fully-expired generations.

Logins
------
The shared rotating password is the only login, and it mints ``sub=operator``.
No account holds a static password; a ``verify`` that names a ``username`` is
refused. Failed logins count per client IP; reaching the threshold locks that
IP for a while.

Device tokens
-------------
An unattended device or app that must not store a password holds a device token
(:mod:`awm.config.usertoken`) instead. This service mints it, keeps only the
SHA-256 of its secret, and hands the edge the digests of the live tokens in
``edge_material``; the edge verifies offline, so a revocation lands with the
edge's next material refresh. The edge reports the tokens it saw on that same
refresh, which makes ``last_used_at`` as coarse as the refresh interval. Every
token belongs to ``operator``, and only the operator (a call with no edge
identity) may mint, list or revoke one.

``AWM_AUTH_PROFILE=public`` (the internet-facing host) turns the shared path
off entirely: no minting, no Discord push, and every ``verify`` fails.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
import urllib.parse
from typing import Any

from awm import config
from awm.config import tokens, usertoken

from awm.auth import store
from awm.auth.config import CONTRACT

log = logging.getLogger("awm.auth.service")

_HOUR = 3600.0
_DAY = 86400.0

_PROFILE_ENV = "AWM_AUTH_PROFILE"


#: The owner of every login session and every device token.
OPERATOR = "operator"

# Singleton re-homing selector (federation). auth runs on every node (each mints
# its own creds), but social is a singleton: a node without a local social
# exports AWM_SOCIAL_PEER=<peer> so the daily password push routes to
# social@<peer>. Unset/empty ⇒ local. Read fresh per push.
_SOCIAL_PEER_ENV = "AWM_SOCIAL_PEER"
# A node without a local reporting service exports AWM_REPORTING_PEER=<peer>.
_REPORTING_PEER_ENV = "AWM_REPORTING_PEER"

# Outcome of the most recent Discord push attempt, surfaced via h_status so an
# operator can see a failure without grepping logs. In-memory only — a
# restart naturally repopulates it via the next mint-if-stale push.
_push_status: dict[str, Any] = {"ok": None, "at": None, "error": None}

# Handle to the in-flight background push task (see _spawn_push), so a mint
# that supersedes an in-progress retry can cancel it instead of racing it.
_push_task: "asyncio.Task[None] | None" = None


def _settings() -> Any:
    """Current config values (defaults merged with stored overrides)."""
    return CONTRACT.load_model()


def profile() -> str:
    return (os.environ.get(_PROFILE_ENV) or "").strip().lower()


def shared_password_enabled() -> bool:
    """The rotating shared password (and everything hanging off it: minting,
    the Discord push, peer credentials) is off on the public profile."""
    return profile() != "public"


def _operator_only(as_: str | None, verb: str) -> None:
    """Refuse a verb that arrived through an edge listener.

    The edge always stamps ``X-Awm-As``; a call from this host's own CLI or
    service has none. The verbs guarded this way hand out the means to act as
    this node or to forge a session, which no caller across an edge may have.
    """
    if as_ is not None:
        raise PermissionError(
            f"{verb} is an operator verb: run it on the host, not through an edge")


# ---------------------------------------------------------------------------
# Logins
# ---------------------------------------------------------------------------


def _lock_keys(client_ip: str | None) -> list[str]:
    return [f"ip:{client_ip}"] if client_ip else []


def _locked_for(keys: list[str], now: float) -> float:
    """Seconds until every lock on ``keys`` has lapsed (0 when none holds)."""
    remaining = 0.0
    for key in keys:
        remaining = max(remaining, store.fail_get(key)["locked_until"] - now)
    return remaining


def _autologin_link(login_password: str) -> str | None:
    """A tappable ``/__auth/link`` URL for this node's own edge, or ``None``.

    The password already travels in the message body; putting it in a URL as
    well is what turns the push from a transcription job into a tap. The edge
    route it points at never renders under that URL — it validates and 302s to a
    clean path with the session cookie set (see ``httpsfront.proxy``).
    """
    base = config.edge_url()
    if not base:
        return None
    return f"{base}/__auth/link?p={urllib.parse.quote(login_password, safe='')}"


async def _push_password_to_discord_attempt(login_password: str) -> None:
    """One attempt to send the day's login password to the operator's Discord DM.

    Raises on failure — the caller (`_push_password_to_discord`) owns retry
    and never-raise semantics. Every lookup that could fail (node name, edge
    URL) is inside this function, not above it: with three nodes pushing into
    one channel the message must say which node it came from, but a retry
    must redo the whole message, not resume a half-built one.
    """
    s = _settings()
    node = config.node_name()
    link = _autologin_link(login_password)
    text = (
        f"🔑 **awm login password** minted on **{node}**\n"
        f"`{login_password}`\n"
        # Bare URL in angle brackets: clickable in every Discord surface
        # (masked-link rendering is not), and the brackets stop Discord from
        # trying to unfurl a URL that carries a live credential.
        + (f"tap to open {node} signed in: <{link}>\n" if link else "")
        + f"valid ~{s.validity_hours:.0f}h. Get it any time on {node} with "
        f"`awm auth password`."
    )
    from awm import gatewayclient
    if not s.push_discord:
        result = await gatewayclient.call("operations", "ops_post", {
                "kind": "password", "node": node,
                "title": f"{node} login password", "body": text,
            }, peer=gatewayclient.peer_env(_REPORTING_PEER_ENV))
        if not (isinstance(result, dict) and result.get("ok")):
            raise RuntimeError(f"reporting ops_post refused the password: {result!r}")
        log.info("auth: posted %s's login password to the Ops note", node)
        return
    operator = config.operator_dm() or ("", "")
    account = s.discord_account or operator[0]
    channel = s.discord_channel or operator[1]
    if not (account and channel):
        raise RuntimeError("no Discord target for the password push: set "
                           "discord_channel or AWM_OPERATOR_DM_CHANNEL")
    await gatewayclient.call("social", "send", {
            "account": account,
            "channel": channel,
            "text": text,
        }, peer=gatewayclient.peer_env(_SOCIAL_PEER_ENV))
    log.info("auth: pushed %s's login password to Discord %s#%s",
             node, account, channel)


async def _push_password_to_discord(login_password: str, expires_at: float) -> None:
    """Best-effort, with retries: push the day's login password to Discord.

    Never raises into the mint — a social outage must not block rotation.
    A single attempt failing is often transient (e.g. a VPN blip to the peer
    hosting the singleton social service), so retry with doubling backoff for
    a few minutes rather than leaving the operator without a password message
    until the next scheduled mint (up to `mint_cadence_hours` later). The mint
    itself has already succeeded by the time this runs (see mint_now) — all
    that is at stake here is how fast the operator gets notified.
    """
    s = _settings()
    if not s.push_enabled:
        return
    attempts = max(1, int(getattr(s, "push_retry_attempts", 1)))
    backoff = float(getattr(s, "push_retry_backoff_seconds", 0.0))
    last_exc: Exception | None = None
    for attempt in range(attempts):
        if attempt:
            await asyncio.sleep(backoff * (2 ** (attempt - 1)))
        try:
            await _push_password_to_discord_attempt(login_password)
        except Exception as exc:  # noqa: BLE001 — push is best-effort
            last_exc = exc
            log.warning(
                "auth: Discord password push failed (attempt %d/%d): %s",
                attempt + 1, attempts, exc)
            continue
        _push_status.update(ok=True, at=time.time(), error=None)
        return
    log.error(
        "auth: Discord password push failed after %d attempt(s); giving up "
        "until the next mint: %s", attempts, last_exc)
    _push_status.update(
        ok=False, at=time.time(),
        error=str(last_exc) if last_exc else None)


def _spawn_push(login_password: str, expires_at: float) -> None:
    """Fire the Discord push (with its own retries) in the background.

    A slow or retrying push must never delay mint_now's return or the
    rotation loop's cadence timing — that invariant only holds if the push
    runs detached. Cancels any still-in-flight push first, so a mint that
    supersedes an in-progress retry does not end up racing it or notifying
    about a since-superseded password.
    """
    global _push_task
    if _push_task is not None and not _push_task.done():
        _push_task.cancel()
    _push_task = asyncio.create_task(
        _push_password_to_discord(login_password, expires_at))


async def mint_now(*, reason: str = "rotation") -> dict[str, Any]:
    """Mint a fresh credential generation, announce it, prune."""
    s = _settings()
    gen = store.mint_generation(validity_seconds=s.validity_hours * _HOUR)
    # LOUD — an operator or agent tailing logs must notice a new password.
    log.warning(
        "=== auth: minted credential generation %d (%s); login password valid ~%.0fh ===",
        gen["generation"], reason, s.validity_hours,
    )
    removed = store.prune_expired()
    if removed:
        log.info("auth: pruned %d expired generation(s)", removed)
    _spawn_push(gen["login_password"], gen["expires_at"])
    return gen


async def _mint_if_stale() -> None:
    """Mint on startup unless a fresh-enough generation already exists."""
    s = _settings()
    latest = store.latest()
    now = time.time()
    if latest is None:
        await mint_now(reason="startup (none existed)")
        return
    age = now - latest["minted_at"]
    if age >= s.mint_cadence_hours * _HOUR:
        await mint_now(reason=f"startup (newest was {age / _HOUR:.1f}h old)")
    else:
        log.info("auth: newest generation %d is %.1fh old (< %.0fh cadence); no mint",
                 latest["generation"], age / _HOUR, s.mint_cadence_hours)


async def _rotation_loop() -> None:
    """Sleep until the next cadence boundary, mint, repeat — forever."""
    while True:
        s = _settings()
        latest = store.latest()
        cadence = s.mint_cadence_hours * _HOUR
        if latest is None:
            delay = 0.0
        else:
            delay = max(0.0, (latest["minted_at"] + cadence) - time.time())
        await asyncio.sleep(delay)
        try:
            await mint_now(reason="scheduled")
        except Exception:  # noqa: BLE001 — never let the loop die on one mint
            log.exception("auth: scheduled mint failed; retrying next cadence")
            # Avoid a tight loop if mint_now itself keeps throwing.
            await asyncio.sleep(60)


async def on_start() -> None:
    """Adapter ``on_start``: init DB, ensure secret, mint-if-stale, spawn loops."""
    store.init()
    store.ensure_secret()
    if not shared_password_enabled():
        log.warning("auth: profile %r — shared password disabled; no login "
                    "is accepted here", profile())
        return
    await _mint_if_stale()
    asyncio.create_task(_rotation_loop())
    log.info("auth: rotation loop started")


# ---------------------------------------------------------------------------
# Verb handlers
# ---------------------------------------------------------------------------


def h_password(args: dict) -> dict:
    """Return the current (newest) login password + its window. Loopback/CLI —
    `awm auth password`. Never reachable unauthenticated through the edge."""
    latest = store.latest()
    if latest is None:
        return {"password": None, "generation": None}
    return {
        "password": latest["login_password"],
        "generation": latest["generation"],
        "minted_at": latest["minted_at"],
        "expires_at": latest["expires_at"],
    }


def h_verify(args: dict) -> dict:
    """Validate a login and mint a signed session token for the edge.

    The only login is the shared rotating password (every valid generation is
    scanned with ``compare_digest``), ``sub="operator"``, and only while the
    shared path is enabled. A call that names a ``username`` is refused: no
    account holds a static password. Failures count per ``client_ip``; a locked
    IP answers ``{ok: false, retry_after}`` without checking the password.
    """
    args = args or {}
    submitted = str(args.get("password") or "")
    username = str(args.get("username") or "").strip() or None
    client_ip = str(args.get("client_ip") or "").strip() or None
    s = _settings()
    now = time.time()

    keys = _lock_keys(client_ip)
    remaining = _locked_for(keys, now)
    if remaining > 0:
        return {"ok": False, "locked": True, "retry_after": int(remaining) + 1}

    ok = False
    if username is None and shared_password_enabled():
        for gen in store.valid_generations(now):
            if hmac.compare_digest(gen["login_password"], submitted):
                ok = True
    sub = OPERATOR

    if not ok:
        lock_seconds = s.lockout_minutes * 60.0
        for key in keys:
            store.fail_record(key, threshold=max(1, int(s.lockout_threshold)),
                              lock_seconds=lock_seconds, now=now)
        return {"ok": False}
    for key in keys:
        store.fail_clear(key)
    secret = store.ensure_secret()
    ttl = s.session_ttl_hours * _HOUR
    token = tokens.mint(secret, sub=sub, ttl=ttl)
    return {"ok": True, "sub": sub, "token": token, "session_ttl_seconds": ttl}


def _record_device_use(used: Any) -> None:
    """Apply the edge's ``{id: epoch_seconds}`` report of device-token use."""
    if not isinstance(used, dict):
        return
    clean = {str(k): float(v) for k, v in used.items()
             if isinstance(v, (int, float)) and not isinstance(v, bool)}
    store.device_touch(clean)


def _edge_devices(enforced: Any) -> list[dict]:
    """The live tokens an edge that enforces ``enforced`` scopes may honour."""
    known = set(enforced) if isinstance(enforced, list) else set()
    out = []
    for t in store.device_live():
        entry = {"id": t["id"], "sub": t["username"], "sha256": t["secret_sha256"]}
        if t["scope"]:
            if t["scope"] not in known:
                continue
            entry["scope"] = t["scope"]
        out.append(entry)
    return out


def h_edge_material(args: dict, as_: str | None = None) -> dict:
    """Material the httpsfront edge caches to enforce auth offline.

    Returns the signing secret (to verify+slide cookies without an RPC per
    request), the digests of the live device tokens (to check user bearers), and
    the session-lifetime knobs. ``device_used`` is the edge's report of the device
    tokens it has accepted since its last fetch. ``token_scopes`` names the scopes
    the edge enforces. A scoped token goes out only to an edge that names its
    scope, because an edge that cannot read the scope would honour it everywhere.
    Operator-only: the edge itself fetches it with no ``as_``, and a call that
    crossed an edge carries one and is refused.
    """
    _operator_only(as_, "edge_material")
    _record_device_use((args or {}).get("device_used"))
    s = _settings()
    return {
        "secret": store.ensure_secret(),
        "device_tokens": _edge_devices((args or {}).get("token_scopes")),
        "session_ttl_seconds": s.session_ttl_hours * _HOUR,
        "max_session_seconds": s.max_session_days * _DAY,
    }


async def h_rotate(args: dict) -> dict:
    """Force a mint now (ops/testing). Returns the new generation's window."""
    if not shared_password_enabled():
        raise ValueError("shared password is disabled on the %r profile" % profile())
    gen = await mint_now(reason="manual rotate")
    return {
        "generation": gen["generation"],
        "minted_at": gen["minted_at"],
        "expires_at": gen["expires_at"],
    }


def h_status(args: dict) -> dict:
    valid = store.valid_generations()
    latest = store.latest()
    s = _settings()
    return {
        "valid_generations": len(valid),
        "latest_generation": latest["generation"] if latest else None,
        "latest_minted_at": latest["minted_at"] if latest else None,
        "latest_expires_at": latest["expires_at"] if latest else None,
        "mint_cadence_hours": s.mint_cadence_hours,
        "validity_hours": s.validity_hours,
        "push_enabled": s.push_enabled,
        "push_last_ok": _push_status["ok"],
        "push_last_attempt_at": _push_status["at"],
        "push_last_error": _push_status["error"],
        "profile": profile() or "default",
        "shared_password_enabled": shared_password_enabled(),
    }


# --- device tokens -----------------------------------------------------------
#
# CLI/HTTP only, and operator-only: every token belongs to ``operator``, and a
# call that crossed an edge is refused.

_LABEL_MAX = 64


def _public_device(t: dict) -> dict:
    return {"id": t["id"], "username": t["username"], "label": t["label"],
            "created_at": t["created_at"], "last_used_at": t["last_used_at"],
            "revoked_at": t["revoked_at"], "scope": t["scope"]}


def h_device_mint(args: dict, as_: str | None = None) -> dict:
    """Mint a device token. The token is returned once, here.

    A device token speaks for one device, as ``device:<id>``, never for the
    operator, and it carries a scope that bounds what the device reaches.
    """
    _operator_only(as_, "device_mint")
    args = args or {}
    label = str(args.get("label") or "").strip()
    if not label:
        raise ValueError("label is required")
    if len(label) > _LABEL_MAX:
        raise ValueError(f"label is longer than {_LABEL_MAX} characters")
    scope = str(args.get("scope") or "").strip()
    if scope not in usertoken.SCOPES:
        raise ValueError(f"a device token needs a scope; known: "
                         f"{', '.join(sorted(usertoken.SCOPES))}")
    token_id, secret, token = usertoken.mint()
    row = store.device_insert(token_id, username=f"device:{token_id}", label=label,
                              secret_sha256=usertoken.digest(secret), scope=scope)
    log.warning("auth: minted device token %s (%s, scope %s)", token_id, label, scope)
    return {**_public_device(row), "token": token}


def h_device_list(args: dict, as_: str | None = None) -> dict:
    """Every device token, revoked ones included. Never the digest."""
    _operator_only(as_, "device_list")
    return {"tokens": [_public_device(t) for t in store.device_list()]}


def h_device_revoke(args: dict, as_: str | None = None) -> dict:
    """Revoke a device token by id. The edge drops it on its next refresh."""
    _operator_only(as_, "device_revoke")
    args = args or {}
    token_id = str(args.get("id") or "").strip()
    if not token_id:
        raise ValueError("id is required")
    if store.device_get(token_id) is None:
        raise ValueError(f"no such device token {token_id!r}")
    store.device_revoke(token_id)
    row = store.device_get(token_id)
    log.warning("auth: revoked device token %s of %s", token_id, row["username"])
    return _public_device(row)  # type: ignore[arg-type]
