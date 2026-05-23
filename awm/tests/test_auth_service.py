"""Tests for the auth service: token bootstrap, TLS cert generation,
bearer verification, session minting."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest


@pytest.fixture()
def auth_dir(tmp_path, monkeypatch):
    """Isolate AUTH_TOKEN_FILE and TLS paths under tmp_path."""
    awm_dir = tmp_path / ".awm"
    awm_dir.mkdir()
    tls_dir = awm_dir / "tls"

    monkeypatch.setattr("awm.config.AWM_DIR", awm_dir)
    monkeypatch.setattr("awm.config.AUTH_TOKEN_FILE", awm_dir / "auth.token")
    monkeypatch.setattr("awm.config.TLS_DIR", tls_dir)
    monkeypatch.setattr("awm.config.TLS_CERT", tls_dir / "cert.pem")
    monkeypatch.setattr("awm.config.TLS_KEY", tls_dir / "key.pem")
    monkeypatch.delenv("AWM_AUTH_TOKEN", raising=False)

    # Drop any cached token state from previous tests.
    from awm.services import auth as _auth
    _auth._token_cache.clear()
    _auth._token_cache["value"] = None
    _auth._sessions.clear()

    return awm_dir


class TestLocalToken:
    def test_missing_raises_when_not_generating(self, auth_dir):
        from awm.services import auth
        with pytest.raises(auth.TokenMissing):
            auth.local_token(generate_if_missing=False)

    def test_generate_creates_file_with_secure_mode(self, auth_dir):
        from awm.services import auth
        token = auth.local_token(generate_if_missing=True)
        assert token
        token_file = auth_dir / "auth.token"
        assert token_file.exists()
        mode = token_file.stat().st_mode & 0o777
        assert mode == 0o600

    def test_env_overrides_file(self, auth_dir, monkeypatch):
        from awm.services import auth
        (auth_dir / "auth.token").write_text("file-token\n")
        monkeypatch.setenv("AWM_AUTH_TOKEN", "env-token")
        assert auth.local_token() == "env-token"

    def test_file_rewrite_picked_up_immediately(self, auth_dir):
        from awm.services import auth
        (auth_dir / "auth.token").write_text("first\n")
        assert auth.local_token() == "first"
        (auth_dir / "auth.token").write_text("second\n")
        assert auth.local_token() == "second"


class TestTLSBootstrap:
    def test_bootstrap_creates_cert_and_key(self, auth_dir):
        from awm.services import auth
        cert, key = auth.bootstrap_tls(generate_if_missing=True)
        assert cert.exists()
        assert key.exists()
        # Key file should be 0600.
        assert (key.stat().st_mode & 0o777) == 0o600

    def test_bootstrap_is_idempotent(self, auth_dir):
        from awm.services import auth
        cert1, key1 = auth.bootstrap_tls(generate_if_missing=True)
        mtime1 = cert1.stat().st_mtime
        time.sleep(0.01)
        cert2, key2 = auth.bootstrap_tls(generate_if_missing=True)
        assert cert1 == cert2 and key1 == key2
        # No regeneration on second call.
        assert cert2.stat().st_mtime == mtime1

    def test_fingerprint_changes_when_cert_changes(self, auth_dir):
        from awm.services import auth
        auth.bootstrap_tls(generate_if_missing=True)
        fp1 = auth.tls_fingerprint()
        # Force regen.
        auth_dir.joinpath("tls", "cert.pem").unlink()
        auth_dir.joinpath("tls", "key.pem").unlink()
        auth.bootstrap_tls(generate_if_missing=True)
        fp2 = auth.tls_fingerprint()
        assert fp1 and fp2 and fp1 != fp2


class TestVerifyBearer:
    def test_local_token_resolves_to_local_identity(self, auth_dir):
        from awm.services import auth
        tok = auth.local_token(generate_if_missing=True)
        ident = auth.verify_bearer(tok)
        assert ident is not None
        assert ident.kind == "local"
        assert ident.name == "operator"

    def test_bad_token_returns_none(self, auth_dir):
        from awm.services import auth
        auth.local_token(generate_if_missing=True)
        assert auth.verify_bearer("garbage") is None
        assert auth.verify_bearer(None) is None
        assert auth.verify_bearer("") is None


class TestSessions:
    def test_mint_and_resolve(self, auth_dir):
        from awm.services import auth
        ident = auth.Identity(kind="local", name="operator")
        sid = auth.mint_session(ident)
        resolved = auth.verify_bearer(sid)
        assert resolved is not None
        assert resolved.kind == "session"

    def test_drop_invalidates(self, auth_dir):
        from awm.services import auth
        ident = auth.Identity(kind="local", name="operator")
        sid = auth.mint_session(ident)
        assert auth.drop_session(sid) is True
        assert auth.verify_bearer(sid) is None

    def test_sweep_expired_drops_old(self, auth_dir):
        from awm.services import auth
        ident = auth.Identity(kind="local", name="operator")
        sid = auth.mint_session(ident, ttl=-1)  # immediately stale
        dropped = auth.sweep_expired()
        assert dropped >= 1
        assert auth.verify_bearer(sid) is None


class TestBootstrap:
    def test_bootstrap_creates_everything(self, auth_dir):
        from awm.services import auth
        info = auth.bootstrap()
        assert Path(info["token_file"]).exists()
        assert Path(info["tls_cert"]).exists()
        assert Path(info["tls_key"]).exists()
        assert info["tls_fingerprint"]
