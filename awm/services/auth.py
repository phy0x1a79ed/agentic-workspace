"""Auth service — the single answer to "is this caller permitted?"

This module owns:

- The local-daemon bearer token (``$AWM_DIR/auth.token``) — readability of
  the file IS the access boundary for CLI / MCP / in-process callers.
- The self-signed TLS cert+key at ``$AWM_DIR/tls/{cert,key}.pem`` — both
  bootstrapped on daemon startup if missing.
- ``verify_bearer(token)`` — pure-key membership check over
  ``{local_token} ∪ {peer_tokens}``. Returns ``True`` if the token is a
  valid key, ``False`` otherwise. **Auth is not identity.**
- ``verify_peer_bearer(token, claimed_peer)`` — cross-check used on
  ``/peer/*`` routes: the token must match the specific peer claimed in
  the ``X-Awm-From`` header. This prevents peer A's bearer from being
  presented while claiming to be peer B.
- ``client_kwargs()`` — internal httpx wiring (headers + verify) so every
  caller (CLI, MCP, in-process) gets identical, transparent auth without
  knowing what's in those kwargs.
- One-shot login challenges — short-lived single-use nonces minted by the
  Discord bot / ``awm login`` CLI, consumed by ``/auth/bootstrap`` to set
  the bearer as an HttpOnly cookie. The long-lived bearer never leaves
  the daemon.

Design intent: auth is a service-layer concern, and a bearer is just a
key. Identity (which user is acting, which peer is calling) is a separate
claim carried by ``X-Awm-As`` / ``X-Awm-From`` headers — see
``awm/api/peer.py`` for how peer identity is cross-checked.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from pathlib import Path

from awm import config


# ---------------------------------------------------------------------------
# Token: load from env, file, or auto-generate (daemon only)
# ---------------------------------------------------------------------------


# Cache exists only so we can clear it from tests + to keep "rotation =
# rewrite the file" cheap-ish. The token file is small (<100 bytes); we
# re-read on every call so rotation is instant and there's no stale-cache
# class of bug. The cache is just a deduped tag for testing convenience.
_token_cache: dict[str, object] = {"value": None}
_token_lock = threading.Lock()


class TokenMissing(RuntimeError):
    """Raised by :func:`local_token` when no token can be resolved.

    Clients (CLI / MCP) should surface this as a hint to run the daemon.
    The daemon never raises this — it auto-bootstraps a token.
    """


def local_token(*, generate_if_missing: bool = False) -> str:
    """Return the canonical local-daemon bearer token.

    Resolution order:
      1. ``$AWM_AUTH_TOKEN`` env var (escape hatch).
      2. File at :data:`config.AUTH_TOKEN_FILE` (re-read on every call;
         rotation = rewrite the file, no restart needed).
      3. If ``generate_if_missing`` (daemon startup), create one at the
         file path with mode 0600 and return it.

    Clients pass ``generate_if_missing=False`` (default). The daemon's
    bootstrap step passes ``True``.
    """
    env_tok = os.environ.get(config.AUTH_TOKEN_ENV)
    if env_tok and env_tok.strip():
        return env_tok.strip()

    path: Path = config.AUTH_TOKEN_FILE
    with _token_lock:
        if path.exists():
            try:
                value = path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                if not generate_if_missing:
                    raise TokenMissing(
                        f"could not read auth token at {path}: {exc}"
                    ) from exc
                value = ""
            if value:
                _token_cache["value"] = value
                return value

        if not generate_if_missing:
            raise TokenMissing(
                f"awm auth token not found at {path}. "
                "The daemon hasn't initialized yet — run `awm serve` once."
            )

        path.parent.mkdir(parents=True, exist_ok=True)
        new_value = secrets.token_urlsafe(32)
        path.write_text(new_value + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        _token_cache["value"] = new_value
        return new_value


# ---------------------------------------------------------------------------
# TLS — auto-bootstrap self-signed cert/key
# ---------------------------------------------------------------------------


def bootstrap_tls(*, generate_if_missing: bool = True) -> tuple[Path, Path]:
    """Ensure :data:`config.TLS_CERT` and :data:`config.TLS_KEY` exist.

    If both are present, returns them. Otherwise generates a self-signed
    cert (CN=awm-daemon, valid 10 years) at the configured paths and
    returns them. Permissions: cert 0644, key 0600.
    """
    cert_path: Path = config.TLS_CERT
    key_path: Path = config.TLS_KEY

    if cert_path.exists() and key_path.exists():
        return cert_path, key_path
    if not generate_if_missing:
        raise FileNotFoundError(
            f"TLS cert/key not found at {cert_path} / {key_path}"
        )

    # Late import — cryptography is a soft requirement for the daemon
    # only. CLI/MCP can verify_bearer with no TLS work.
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import datetime

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "awm-daemon"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "awm"),
    ])
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=365 * 10))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.DNSName("awm-daemon"),
            ]),
            critical=False,
        )
        .sign(private_key=key, algorithm=hashes.SHA256())
    )

    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    try:
        os.chmod(cert_path, 0o644)
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return cert_path, key_path


def tls_fingerprint() -> str | None:
    """SHA-256 fingerprint of the local TLS cert, lowercase hex."""
    cert_path: Path = config.TLS_CERT
    if not cert_path.exists():
        return None
    try:
        return hashlib.sha256(cert_path.read_bytes()).hexdigest()
    except OSError:
        return None


def bootstrap() -> dict[str, str]:
    """Idempotent daemon-startup bootstrap.

    Ensures both the auth token and TLS cert/key exist. Returns a dict
    with paths and the cert fingerprint for logging.
    """
    token = local_token(generate_if_missing=True)
    cert, key = bootstrap_tls(generate_if_missing=True)
    return {
        "token_file": str(config.AUTH_TOKEN_FILE),
        "tls_cert": str(cert),
        "tls_key": str(key),
        "tls_fingerprint": tls_fingerprint() or "",
        # Token is intentionally not returned here — callers who need it
        # call local_token() directly so it doesn't leak into log lines.
        "token_len": str(len(token)),
    }


# ---------------------------------------------------------------------------
# Cookie name (bearer-as-cookie; no server-side session state)
# ---------------------------------------------------------------------------

# Browser bearer is stored verbatim in this HttpOnly cookie. Rotation =
# rewrite ``$AWM_DIR/auth.token``; existing cookies stop validating on the
# next request. There is no ``_sessions`` dict, no TTL, no expiry-sweep —
# the bearer itself is the credential.
SESSION_COOKIE = "awm_session"

# Non-HttpOnly companion cookie carrying the operator's display name.
# The SPA reads this to populate the ``X-Awm-As`` header. Not a credential.
AS_COOKIE = "awm_as"


# ---------------------------------------------------------------------------
# Bearer verification (server side)
# ---------------------------------------------------------------------------


def verify_bearer(token: str | None) -> bool:
    """Pure-key membership check: is ``token`` a valid bearer?

    Returns ``True`` iff ``token`` matches either:

      1. the local-daemon token (``$AWM_DIR/auth.token``), or
      2. any registered peer token (``$AWM_DIR/peers/*.token``).

    No identity is returned — auth and identity are separate. Callers who
    need to know *who* the caller is must read ``X-Awm-As`` /
    ``X-Awm-From``, and ``/peer/*`` routes must use
    :func:`verify_peer_bearer` to cross-check.
    """
    if not token:
        return False
    token = token.strip()
    if not token:
        return False

    # 1) Local operator token.
    try:
        expected = local_token(generate_if_missing=False)
    except TokenMissing:
        expected = None
    if expected and hmac.compare_digest(token, expected):
        return True

    # 2) Registered peer tokens. Iterate $AWM_DIR/peers/*.token directly
    # so we don't depend on the DB peers table at auth time (DB may be
    # locked, peers table may be empty for a fresh install). Token files
    # are the source of truth: install_peer_token writes them, remove_peer
    # deletes them.
    peers_dir = config.AWM_DIR / "peers"
    if peers_dir.is_dir():
        try:
            entries = list(peers_dir.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            if not entry.is_file() or not entry.name.endswith(".token"):
                continue
            try:
                value = entry.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if value and hmac.compare_digest(token, value):
                return True

    return False


def verify_peer_bearer(token: str | None, claimed_peer: str) -> bool:
    """Cross-check: does ``token`` match the specific peer's token file?

    Used by ``/peer/*`` routes after the ``X-Awm-From`` header is parsed.
    Prevents peer A from presenting its own bearer while claiming
    ``X-Awm-From: B`` — federation routes need to know which peer they're
    talking to, not just that *some* registered peer authenticated.
    """
    if not token or not claimed_peer:
        return False
    token = token.strip()
    if not token:
        return False

    # Path traversal guard — peer ids should be simple ascii but a stray
    # ``../`` shouldn't even be considered. We never let claimed_peer
    # escape the peers/ directory.
    if "/" in claimed_peer or "\\" in claimed_peer or claimed_peer.startswith("."):
        return False

    peer_token_path = config.AWM_DIR / "peers" / f"{claimed_peer}.token"
    if not peer_token_path.is_file():
        return False
    try:
        expected = peer_token_path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not expected:
        return False
    return hmac.compare_digest(token, expected)


# ---------------------------------------------------------------------------
# Client wiring — every internal HTTPS caller gets these kwargs
# ---------------------------------------------------------------------------


def client_kwargs(*, timeout: float = 30.0) -> dict:
    """Return httpx kwargs for HTTPS calls to the local daemon.

    ``verify=False`` disables **TLS server-certificate verification** —
    not auth. The daemon binds a self-signed cert that no public CA has
    signed, so an httpx default of ``verify=True`` would reject it. The
    trust boundary is the transport (loopback for CLI/MCP, SSH tunnel for
    peer-to-peer federation), not the cert chain.

    Auth is independent: the bearer in the ``Authorization`` header is
    what proves the caller is permitted; TLS just keeps the bearer off
    the wire in cleartext.

    For non-loopback, non-tunneled HTTPS in the future, switch to
    ``verify=str(config.TLS_CERT)`` to pin the daemon's cert.
    """
    token = local_token()  # raises TokenMissing on client misconfigure
    return {
        "headers": {"Authorization": f"Bearer {token}"},
        "verify": False,
        "timeout": timeout,
    }


def base_url() -> str:
    """Canonical HTTPS base URL for the local daemon."""
    host = config.EXPOSED_HOST
    if host == "0.0.0.0":
        host = "127.0.0.1"
    return f"https://{host}:{config.EXPOSED_PORT}"


# ---------------------------------------------------------------------------
# One-shot login challenges (phase 2 — Discord-DM / `awm login` flow)
# ---------------------------------------------------------------------------

# ``nonce → (awm_user, expires_at_epoch)``. Single-use: consume_challenge
# pops the entry atomically. The sweeper drops expired entries.
_challenges: dict[str, tuple[str, float]] = {}
_challenges_lock = threading.Lock()

CHALLENGE_TTL_SECONDS = 60


def mint_challenge(awm_user: str, *, ttl: int = CHALLENGE_TTL_SECONDS) -> str:
    """Mint a single-use login nonce for ``awm_user`` and return it.

    Caller (Discord bot, ``awm login`` CLI) builds the URL
    ``{base_url()}/auth/bootstrap?ot={nonce}`` and hands it to the
    operator. ``/auth/bootstrap`` consumes the nonce, sets the bearer as
    an HttpOnly cookie, and redirects to ``/ui/``.
    """
    nonce = secrets.token_urlsafe(32)
    expires = time.time() + ttl
    with _challenges_lock:
        _challenges[nonce] = (awm_user, expires)
    return nonce


def consume_challenge(nonce: str | None) -> str | None:
    """Pop ``nonce`` atomically. Returns the bound ``awm_user`` on
    success, ``None`` if the nonce is missing, already used, or expired.
    """
    if not nonce:
        return None
    with _challenges_lock:
        entry = _challenges.pop(nonce, None)
    if entry is None:
        return None
    awm_user, expires = entry
    if expires < time.time():
        return None
    return awm_user


def sweep_challenges() -> int:
    """Drop expired challenges. Returns the number dropped."""
    now = time.time()
    dropped = 0
    with _challenges_lock:
        for nonce in list(_challenges):
            _, expires = _challenges[nonce]
            if expires < now:
                _challenges.pop(nonce, None)
                dropped += 1
    return dropped
