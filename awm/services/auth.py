"""Auth service — the single answer to "is this caller permitted?"

This module owns:

- The local-daemon bearer token (``$AWM_DIR/auth.token``) — readability of
  the file IS the access boundary for CLI / MCP / in-process callers.
- The self-signed TLS cert+key at ``$AWM_DIR/tls/{cert,key}.pem`` — both
  bootstrapped on daemon startup if missing.
- ``verify_bearer(token)`` — single resolver mapping a bearer token to an
  identity. Used by the FastAPI auth middleware.
- ``client_kwargs()`` — internal httpx wiring (headers + verify) so every
  caller (CLI, MCP, in-process) gets identical, transparent auth without
  knowing what's in those kwargs.
- Short-lived web-UI session cookies — minted by ``/auth/exchange`` from
  a one-shot bearer, validated by the auth middleware on subsequent
  requests so the bearer never has to leave the URL hash.

Design intent: auth is a service-layer concern. Transports (HTTPS) just
carry credentials; whether a caller is allowed lives here.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from awm import config


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


@dataclass
class Identity:
    """Who the caller is. Returned by :func:`verify_bearer`."""

    kind: str  # "local" | "peer" | "session"
    name: str  # local: "operator"; peer: peer_id; session: session_id
    expires_at: Optional[float] = None  # epoch seconds, sessions only


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
# Session cookies (web UI)
# ---------------------------------------------------------------------------


SESSION_COOKIE = "awm_session"
SESSION_TTL_SECONDS = 24 * 60 * 60

_sessions: dict[str, Identity] = {}
_sessions_lock = threading.Lock()


def mint_session(identity: Identity, *, ttl: int = SESSION_TTL_SECONDS) -> str:
    """Create a session for ``identity`` and return its opaque id."""
    sid = secrets.token_urlsafe(32)
    expires = time.time() + ttl
    entry = Identity(kind="session", name=identity.name, expires_at=expires)
    with _sessions_lock:
        _sessions[sid] = entry
    return sid


def drop_session(sid: str) -> bool:
    with _sessions_lock:
        return _sessions.pop(sid, None) is not None


def get_session(sid: str) -> Identity | None:
    with _sessions_lock:
        entry = _sessions.get(sid)
        if entry is None:
            return None
        if entry.expires_at and entry.expires_at < time.time():
            _sessions.pop(sid, None)
            return None
        return entry


def sweep_expired() -> int:
    now = time.time()
    dropped = 0
    with _sessions_lock:
        for sid in list(_sessions):
            entry = _sessions[sid]
            if entry.expires_at and entry.expires_at < now:
                _sessions.pop(sid, None)
                dropped += 1
    return dropped


# ---------------------------------------------------------------------------
# Bearer verification (server side)
# ---------------------------------------------------------------------------


def verify_bearer(token: str | None) -> Identity | None:
    """Resolve ``token`` to an :class:`Identity`, or None on miss.

    Order: local-token → registered peer tokens → live sessions.
    """
    if not token:
        return None
    token = token.strip()
    if not token:
        return None

    # 1) Local operator token.
    try:
        expected = local_token(generate_if_missing=False)
    except TokenMissing:
        expected = None
    if expected and hmac.compare_digest(token, expected):
        return Identity(kind="local", name="operator")

    # 2) Live web-UI sessions.
    sess = get_session(token)
    if sess is not None:
        return sess

    return None


# ---------------------------------------------------------------------------
# Client wiring — every internal HTTPS caller gets these kwargs
# ---------------------------------------------------------------------------


def client_kwargs(*, timeout: float = 30.0) -> dict:
    """Return httpx kwargs that authenticate to the local daemon.

    ``verify=False`` is intentional: the daemon binds loopback with a
    self-signed cert; the bearer provides authn/authz. Add pinning later
    if cross-host clients need it.
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
