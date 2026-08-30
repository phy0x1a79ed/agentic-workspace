"""Auth service orchestration — rotation policy, the verb handlers, and the
``$AWM_PEER_CRED`` file the SSH peer-auth channel reads.

The service owns one authoritative credential state (via :mod:`awm.auth.store`)
and is the *authority*; the ``httpsfront`` edge only *enforces*, using material
this service hands it (the signing secret + the currently-valid peer credentials)
over loopback.

Rotation contract
-----------------
On startup, mint a pair if none is valid or the newest is older than the cadence
(so a restart never leaves a stale-only state — ``events`` is not relied on).
Then a background loop mints every cadence. Each mint:

* a **loud** log line (so an operator/agent watching a dev session sees it),
* a best-effort push of the *login* password to Discord ``#notifications``
  (never the peer credential, and never blocking the mint on social being up
  — it runs detached, and retries a transient failure with backoff before
  giving up; see ``h_status``'s ``push_last_*`` fields),
* rewrites the ``$AWM_PEER_CRED`` file with the current peer credential,
* prunes fully-expired generations.

User accounts
-------------
Beside the shared rotating password there are static per-user passwords
(``store.awm_users``), scrypt-hashed, set only through the admin CLI verbs
(``user_add`` / ``user_passwd`` generate the password server-side so it never
travels as a shell argument). ``verify`` with a ``username`` takes that path and
mints ``sub=<username>``. Failed logins count per username and per client IP;
reaching the threshold locks the key for a while.

Penpot credentials
------------------
Penpot keeps accounts of its own, so this service also holds one Penpot
credential per person — recorded once when the account is created, exchanged
for a Penpot session whenever the edge needs one, and replaced nightly. That is
a *foreign* credential this service presents, not an awm claim it verifies, so
it is stored in the clear and never touches the session-token codec. The
mechanics are in :mod:`awm.auth.penpot`; the policy (when to rotate, what a
failure means) is here.

``AWM_AUTH_PROFILE=public`` (the internet-facing host) turns the shared path
off entirely: no minting, no Discord push, ``verify`` without a username fails,
and the edge receives no peer credentials. It does **not** turn off the Penpot
rotation — that guards per-user foreign credentials, which the public host is
precisely the one that has.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Any

from awm import config
from awm.config import SERVICES_DIR, tokens

from awm.auth import penpot, store
from awm.auth.config import CONTRACT

log = logging.getLogger("awm.auth.service")

# The current peer credential is mirrored to this file so a peer can fetch it
# with `ssh <host> 'cat "$AWM_PEER_CRED"'` (see the T2 SSH peer-auth channel).
# $AWM_PEER_CRED in the operator's shell rc points here.
PEER_CRED_FILE = SERVICES_DIR / "auth" / "peer_cred.current"

_HOUR = 3600.0
_DAY = 86400.0

_PROFILE_ENV = "AWM_AUTH_PROFILE"

# Same shape ``awm.config.userroot`` accepts: the username doubles as a
# directory name under projects/userdata/.
USERNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# scrypt cost. Tuned for a 2-vCPU host: ~50 ms per hash, 16 MiB memory.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1
_PW_PARAMS = f"scrypt:n={_SCRYPT_N},r={_SCRYPT_R},p={_SCRYPT_P}"
_DUMMY_SALT = secrets.token_hex(16)

# Singleton re-homing selector (federation). auth runs on every node (each mints
# its own creds), but social is a singleton: a node without a local social
# exports AWM_SOCIAL_PEER=<peer> so the daily password push routes to
# social@<peer>. Unset/empty ⇒ local. Read fresh per push.
_SOCIAL_PEER_ENV = "AWM_SOCIAL_PEER"

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


# ---------------------------------------------------------------------------
# Per-user passwords
# ---------------------------------------------------------------------------


def _hash_password(password: str, salt_hex: str) -> str:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32,
    ).hex()


def _check_username(username: Any) -> str:
    name = str(username or "").strip()
    if not USERNAME_RE.match(name):
        raise ValueError(
            "username must match ^[a-z][a-z0-9_-]{0,31}$ (got %r)" % name)
    return name


def set_password(username: str, password: str | None = None) -> str:
    """Create ``username`` or replace its password. Returns the password."""
    name = _check_username(username)
    password = password or secrets.token_urlsafe(12)
    salt = secrets.token_hex(16)
    store.user_upsert(name, pw_hash=_hash_password(password, salt),
                      pw_salt=salt, pw_params=_PW_PARAMS)
    return password


def check_password(username: str, password: str) -> bool:
    """Constant work whether or not the user exists or is disabled."""
    user = store.user_get(username)
    if user is None:
        _hash_password(password, _DUMMY_SALT)
        return False
    computed = _hash_password(password, user["pw_salt"])
    ok = hmac.compare_digest(computed, user["pw_hash"])
    return ok and not user["disabled"]


def _lock_keys(username: str | None, client_ip: str | None) -> list[str]:
    keys = []
    if username:
        keys.append(f"u:{username}")
    if client_ip:
        keys.append(f"ip:{client_ip}")
    return keys


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
    """One attempt to send the day's login password to Discord #notifications.

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
    await gatewayclient.call_maybe_peer(
        gatewayclient.peer_env(_SOCIAL_PEER_ENV),
        "social", "send", {
            "account": s.discord_account,
            "channel": s.discord_channel,
            "text": text,
        })
    log.info("auth: pushed %s's login password to Discord %s#%s",
             node, s.discord_account, s.discord_channel)


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


def _write_peer_cred_file(peer_credential: str) -> None:
    """Mirror the current peer credential to $AWM_PEER_CRED's target file."""
    try:
        PEER_CRED_FILE.parent.mkdir(parents=True, exist_ok=True)
        PEER_CRED_FILE.write_text(peer_credential + "\n")
        PEER_CRED_FILE.chmod(0o600)
    except OSError as exc:
        log.error("auth: could not write peer-cred file %s: %s", PEER_CRED_FILE, exc)


async def mint_now(*, reason: str = "rotation") -> dict[str, Any]:
    """Mint a fresh credential pair, announce it, refresh the peer file, prune."""
    s = _settings()
    gen = store.mint_generation(validity_seconds=s.validity_hours * _HOUR)
    # LOUD — an operator or agent tailing logs must notice a new password.
    log.warning(
        "=== auth: minted credential generation %d (%s); login password valid ~%.0fh ===",
        gen["generation"], reason, s.validity_hours,
    )
    _write_peer_cred_file(gen["peer_credential"])
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
        # Still fresh — just make sure the peer-cred file matches the live value.
        _write_peer_cred_file(latest["peer_credential"])
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
    # Before the profile check, and supervised rather than bare: the Penpot
    # credentials are per-user foreign credentials, so the public host — the
    # one profile that switches the shared password off — is precisely the host
    # that has them. The catch-up inside the loop talks to Penpot over the
    # network, which is why it is spawned rather than awaited: a stack that is
    # slow to come up must not hold up this service's registration.
    from awm.gatewayclient import spawn_supervised
    spawn_supervised("auth:penpot-rotation", _penpot_rotation_loop)
    if not shared_password_enabled():
        log.warning("auth: profile %r — shared password disabled, per-user "
                    "accounts only (%d on file)", profile(), len(store.user_list()))
        return
    await _mint_if_stale()
    asyncio.create_task(_rotation_loop())
    log.info("auth: rotation loop started (peer-cred file: %s)", PEER_CRED_FILE)


# ---------------------------------------------------------------------------
# Penpot credentials
# ---------------------------------------------------------------------------

#: Rotation outcome, surfaced through ``h_status`` so a wedged credential is
#: visible before somebody needs it. In-memory: a restart repopulates it on the
#: catch-up rotation. ``failures`` is keyed by username and holds Penpot's own
#: error code, which is the whole diagnosis (see ``penpot._error_code``).
_penpot_status: dict[str, Any] = {
    "last_rotation_at": None, "last_rotation_ok": None, "failures": {}}


async def penpot_session(username: str, *, refresh: bool = False,
                         stale_token: str | None = None) -> dict[str, Any]:
    """A Penpot session token for ``username``, logging in if needed."""
    name = _check_username(username)
    cred = store.penpot_get(name)
    if cred is None:
        raise ValueError(f"no Penpot credential recorded for {name!r}")
    token = await penpot.session_for(name, cred, refresh=refresh,
                                     stale_token=stale_token)
    return {"username": name, "email": cred["email"],
            "cookie_name": penpot.COOKIE_NAME, "token": token}


async def rotate_penpot_user(username: str, now: float | None = None) -> dict[str, Any]:
    """Replace one person's Penpot password, storing it only once Penpot agrees.

    The order is the whole point: Penpot is changed first and the store second,
    because a store that ran ahead would hold a password Penpot never accepted
    and there is no HTTP path back from that (see :mod:`awm.auth.penpot`).

    Penpot's ``update-profile-password`` invalidates every *other* session for
    the profile, so the token used to make the change is the one that survives —
    it is left in the cache deliberately. What it invalidates is the cookie in
    the person's browser, which the edge re-mints on their next page load.
    """
    cred = store.penpot_get(username)
    if cred is None:
        raise ValueError(f"no Penpot credential recorded for {username!r}")
    new_password = penpot.new_password()
    token = await penpot.session_for(username, cred)
    try:
        await penpot.change_password(token, cred["password"], new_password)
    except penpot.PenpotError:
        # The cached token may simply have stopped working; a stale session is
        # a routine outcome here, a wrong stored password is not. Distinguish
        # them by trying once with a session we know is fresh.
        token = await penpot.session_for(username, cred, refresh=True)
        await penpot.change_password(token, cred["password"], new_password)
    store.penpot_set_password(username, new_password, now=now)
    return {"username": username, "email": cred["email"]}


async def rotate_penpot_all(*, reason: str = "scheduled",
                            now: float | None = None,
                            usernames: list[str] | None = None) -> dict[str, Any]:
    """Rotate every recorded Penpot credential, one failure at a time.

    Users are rotated independently: one account whose stored password has
    drifted must not stop everyone else's from being replaced.
    """
    creds = store.penpot_list()
    if usernames is not None:
        wanted = set(usernames)
        creds = [c for c in creds if c["username"] in wanted]
    rotated: list[str] = []
    failures: dict[str, str] = {}
    for cred in creds:
        name = cred["username"]
        try:
            await rotate_penpot_user(name, now=now)
        except Exception as exc:  # noqa: BLE001 — per-user isolation is the point
            failures[name] = str(exc)
            log.error("auth: Penpot rotation failed for %s: %s", name, exc)
        else:
            rotated.append(name)
    _penpot_status.update(last_rotation_at=time.time(),
                          last_rotation_ok=not failures, failures=failures)
    if creds:
        log.warning("=== auth: rotated %d/%d Penpot credential(s) (%s) ===",
                    len(rotated), len(creds), reason)
    return {"rotated": rotated, "failed": failures, "total": len(creds)}


def _next_rotation_at(hour: int, now: float) -> float:
    """The next occurrence of ``hour``:00 *local* time, strictly after ``now``.

    Local rather than UTC because the point of the hour is that nobody is
    drawing at it, and that is a fact about where the people are.
    ``mktime`` normalises a day-of-month one past the end of the month and
    resolves the DST flag itself, so neither needs handling here.
    """
    lt = time.localtime(now)
    hour = int(hour) % 24
    target = time.mktime(
        (lt.tm_year, lt.tm_mon, lt.tm_mday, hour, 0, 0, 0, 0, -1))
    if target <= now:
        target = time.mktime(
            (lt.tm_year, lt.tm_mon, lt.tm_mday + 1, hour, 0, 0, 0, 0, -1))
    return target


def _penpot_overdue(now: float | None = None) -> list[str]:
    """Credentials whose last rotation is over a day old.

    A box that was off at the rotation hour must catch up on its next start
    rather than skip a day — a credential that quietly stops rotating is worth
    exactly as much as one that was never rotated at all.
    """
    now = time.time() if now is None else now
    return [c["username"] for c in store.penpot_list()
            if now - c["rotated_at"] >= _DAY]


async def _penpot_rotation_loop() -> None:
    """Catch up on anything overdue, then rotate at the configured hour."""
    overdue = _penpot_overdue()
    if overdue and _settings().penpot_rotation_enabled:
        await rotate_penpot_all(reason="startup catch-up", usernames=overdue)
    while True:
        s = _settings()
        now = time.time()
        await asyncio.sleep(max(0.0, _next_rotation_at(s.penpot_rotation_hour, now) - now))
        if not _settings().penpot_rotation_enabled:
            log.info("auth: Penpot rotation is disabled; skipping this hour")
            # Past the boundary but still inside the same hour — sleeping to
            # the *next* boundary from here would land on this one again.
            await asyncio.sleep(_HOUR)
            continue
        await rotate_penpot_all(reason="scheduled")


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


def h_peer_credential(args: dict) -> dict:
    """Return the current peer credential + the file path it is mirrored to."""
    latest = store.latest()
    return {
        "peer_credential": latest["peer_credential"] if latest else None,
        "path": str(PEER_CRED_FILE),
    }


def h_verify(args: dict) -> dict:
    """Validate a login and mint a signed session token for the edge.

    With ``username``: the static per-user password, ``sub=<username>``.
    Without: the shared rotating password (every valid generation is scanned
    with ``compare_digest``), ``sub="operator"`` — only while the shared path
    is enabled. Failures count per username and per ``client_ip``; a locked
    key answers ``{ok: false, retry_after}`` without checking the password.
    """
    args = args or {}
    submitted = str(args.get("password") or "")
    username = str(args.get("username") or "").strip() or None
    client_ip = str(args.get("client_ip") or "").strip() or None
    s = _settings()
    now = time.time()

    keys = _lock_keys(username, client_ip)
    remaining = _locked_for(keys, now)
    if remaining > 0:
        return {"ok": False, "locked": True, "retry_after": int(remaining) + 1}

    if username is not None:
        ok = USERNAME_RE.match(username) is not None and check_password(username, submitted)
        sub = username
    elif shared_password_enabled():
        ok = False
        for gen in store.valid_generations(now):
            if hmac.compare_digest(gen["login_password"], submitted):
                ok = True
        sub = "operator"
    else:
        ok = False
        sub = ""

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


def h_edge_material(args: dict) -> dict:
    """Material the httpsfront edge caches to enforce auth offline.

    Returns the signing secret (to verify+slide cookies without an RPC per
    request), the currently-valid peer credentials (to check peer bearers), and
    the session-lifetime knobs. Loopback-only in practice — the edge itself
    blocks this path to any unauthenticated external caller.
    """
    s = _settings()
    peers = ([g["peer_credential"] for g in store.valid_generations()]
             if shared_password_enabled() else [])
    return {
        "secret": store.ensure_secret(),
        "peer_credentials": peers,
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
        "peer_cred_path": str(PEER_CRED_FILE),
        "profile": profile() or "default",
        "shared_password_enabled": shared_password_enabled(),
        "users": len(store.user_list()),
        "penpot_credentials": len(store.penpot_list()),
        "penpot_rotation_enabled": s.penpot_rotation_enabled,
        "penpot_rotation_hour": s.penpot_rotation_hour,
        "penpot_next_rotation_at": _next_rotation_at(
            s.penpot_rotation_hour, time.time()),
        "penpot_overdue": _penpot_overdue(),
        "penpot_last_rotation_at": _penpot_status["last_rotation_at"],
        "penpot_last_rotation_ok": _penpot_status["last_rotation_ok"],
        "penpot_failures": dict(_penpot_status["failures"]),
    }


def _public_user(u: dict) -> dict:
    return {"username": u["username"], "created_at": u["created_at"],
            "disabled": u["disabled"]}


def h_user_add(args: dict) -> dict:
    """Create a user with a server-generated password, returned once."""
    name = _check_username((args or {}).get("username"))
    if store.user_get(name) is not None:
        raise ValueError(f"user {name!r} exists; use user_passwd to reset")
    password = set_password(name)
    return {"username": name, "password": password}


def h_user_passwd(args: dict) -> dict:
    """Replace a user's password with a fresh server-generated one."""
    name = _check_username((args or {}).get("username"))
    if store.user_get(name) is None:
        raise ValueError(f"no such user {name!r}")
    password = set_password(name)
    store.fail_clear(f"u:{name}")
    return {"username": name, "password": password}


def h_user_disable(args: dict) -> dict:
    args = args or {}
    name = _check_username(args.get("username"))
    disabled = args.get("disabled", True)
    if not isinstance(disabled, bool):
        disabled = str(disabled).lower() not in ("0", "false", "no")
    if not store.user_set_disabled(name, disabled):
        raise ValueError(f"no such user {name!r}")
    return _public_user(store.user_get(name))  # type: ignore[arg-type]


def h_user_list(args: dict) -> dict:
    return {"users": [_public_user(u) for u in store.user_list()]}


# --- Penpot credential verbs -------------------------------------------------
#
# CLI/HTTP only, like the other credential verbs. ``penpot_session`` hands back
# a live foreign credential and ``penpot_record`` takes one; neither belongs on
# a surface an agent can reach.


def h_penpot_record(args: dict) -> dict:
    """Record the Penpot credential awm holds for a user.

    Called once, by ``add-user.sh``, right after it creates the Penpot profile
    with the same password. Re-recording is how a drifted credential is
    repaired, so this deliberately overwrites. The password is never echoed
    back: nothing downstream has a use for it, and the only guarantee worth
    making about it is that no human ever sees it.
    """
    args = args or {}
    name = _check_username(args.get("username"))
    email = str(args.get("email") or "").strip()
    password = str(args.get("password") or "")
    if not email:
        raise ValueError("email is required")
    if not password:
        raise ValueError("password is required")
    cred = store.penpot_upsert(name, email=email, password=password)
    penpot.forget(name)
    return {"username": name, "email": cred["email"],
            "rotated_at": cred["rotated_at"]}


async def h_penpot_session(args: dict) -> dict:
    """Hand the edge a usable Penpot session for a named user.

    ``stale_token`` is the token the browser presented and the edge believes is
    dead; it re-logs-in only if the cache still holds that same value, so a
    page's worth of simultaneously-failing requests costs one login and not one
    each. ``refresh`` is the unconditional form, for ops.
    """
    args = args or {}
    refresh = args.get("refresh", False)
    if not isinstance(refresh, bool):
        refresh = str(refresh).lower() not in ("0", "false", "no", "")
    stale = str(args.get("stale_token") or "") or None
    return await penpot_session(str(args.get("username") or ""),
                                refresh=refresh, stale_token=stale)


async def h_penpot_rotate(args: dict) -> dict:
    """Rotate one user's Penpot password, or everyone's."""
    args = args or {}
    username = str(args.get("username") or "").strip()
    if username:
        result = await rotate_penpot_user(_check_username(username))
        return {"rotated": [result["username"]], "failed": {}, "total": 1}
    return await rotate_penpot_all(reason="manual rotate")


def h_penpot_list(args: dict) -> dict:
    """The recorded Penpot credentials, without the passwords."""
    return {"credentials": [
        {"username": c["username"], "email": c["email"],
         "rotated_at": c["rotated_at"], "created_at": c["created_at"]}
        for c in store.penpot_list()]}
