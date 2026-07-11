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
  (never the peer credential, and never blocking the mint on social being up),
* rewrites the ``$AWM_PEER_CRED`` file with the current peer credential,
* prunes fully-expired generations.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from awm.config import SERVICES_DIR, tokens

from awm.auth import store
from awm.auth.config import CONTRACT

log = logging.getLogger("awm.auth.service")

# The current peer credential is mirrored to this file so a peer can fetch it
# with `ssh <host> 'cat "$AWM_PEER_CRED"'` (see the T2 SSH peer-auth channel).
# $AWM_PEER_CRED in the operator's shell rc points here.
PEER_CRED_FILE = SERVICES_DIR / "auth" / "peer_cred.current"

_HOUR = 3600.0
_DAY = 86400.0


def _settings() -> Any:
    """Current config values (defaults merged with stored overrides)."""
    return CONTRACT.load_model()


async def _push_password_to_discord(login_password: str, expires_at: float) -> None:
    """Best-effort: send the day's login password to Discord #notifications.

    Never raises into the mint — a social outage must not block rotation; the
    next mint (or `awm auth password`) recovers the value.
    """
    s = _settings()
    if not s.push_enabled:
        return
    text = (
        f"🔑 **awm login password** minted\n"
        f"`{login_password}`\n"
        f"valid ~{s.validity_hours:.0f}h. Get it any time on the daemon host with "
        f"`awm auth password`."
    )
    try:
        from awm import gatewayclient
        await gatewayclient.call("social", "send", {
            "account": s.discord_account,
            "channel": s.discord_channel,
            "text": text,
        })
        log.info("auth: pushed login password to Discord %s#%s",
                 s.discord_account, s.discord_channel)
    except Exception as exc:  # noqa: BLE001 — push is best-effort
        log.warning("auth: Discord password push failed (will retry next mint): %s", exc)


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
    await _push_password_to_discord(gen["login_password"], gen["expires_at"])
    return gen


async def _mint_if_stale() -> None:
    """Mint on startup unless a fresh-enough generation already exists."""
    s = _settings()
    latest = store.latest()
    import time
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
    import time
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
    """Adapter ``on_start``: init DB, ensure secret, mint-if-stale, spawn loop."""
    store.init()
    store.ensure_secret()
    await _mint_if_stale()
    asyncio.create_task(_rotation_loop())
    log.info("auth: rotation loop started (peer-cred file: %s)", PEER_CRED_FILE)


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
    """Validate a submitted login password against every valid generation.

    On success mint a signed session token (sliding lifetime); the edge sets it
    as the ``awm_session`` cookie. Constant work on failure — the same set is
    scanned either way (``compare_digest``) to avoid trivial timing leaks.
    """
    import hmac
    submitted = str((args or {}).get("password") or "")
    s = _settings()
    ok = False
    for gen in store.valid_generations():
        if hmac.compare_digest(gen["login_password"], submitted):
            ok = True
    if not ok:
        return {"ok": False}
    secret = store.ensure_secret()
    ttl = s.session_ttl_hours * _HOUR
    token = tokens.mint(secret, sub="operator", ttl=ttl)
    return {"ok": True, "token": token, "session_ttl_seconds": ttl}


def h_edge_material(args: dict) -> dict:
    """Material the httpsfront edge caches to enforce auth offline.

    Returns the signing secret (to verify+slide cookies without an RPC per
    request), the currently-valid peer credentials (to check peer bearers), and
    the session-lifetime knobs. Loopback-only in practice — the edge itself
    blocks this path to any unauthenticated external caller.
    """
    s = _settings()
    return {
        "secret": store.ensure_secret(),
        "peer_credentials": [g["peer_credential"] for g in store.valid_generations()],
        "session_ttl_seconds": s.session_ttl_hours * _HOUR,
        "max_session_seconds": s.max_session_days * _DAY,
    }


async def h_rotate(args: dict) -> dict:
    """Force a mint now (ops/testing). Returns the new generation's window."""
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
        "peer_cred_path": str(PEER_CRED_FILE),
    }
