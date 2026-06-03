"""Tests for the auth service: token bootstrap, TLS cert generation,
pure-key bearer verification, peer-bearer cross-check, one-shot
login challenges."""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.auth, pytest.mark.smoke]

import time
from pathlib import Path

import pytest


@pytest.fixture()
def auth_dir(tmp_path, monkeypatch):
    """Isolate AUTH_TOKEN_FILE, TLS paths, and the peers/ dir under tmp_path."""
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
    _auth._challenges.clear()

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
    def test_local_token_accepted(self, auth_dir):
        from awm.services import auth
        tok = auth.local_token(generate_if_missing=True)
        assert auth.verify_bearer(tok) is True

    def test_bad_token_rejected(self, auth_dir):
        from awm.services import auth
        auth.local_token(generate_if_missing=True)
        assert auth.verify_bearer("garbage") is False
        assert auth.verify_bearer(None) is False
        assert auth.verify_bearer("") is False
        assert auth.verify_bearer("   ") is False

    def test_peer_token_accepted(self, auth_dir):
        from awm.services import auth
        auth.local_token(generate_if_missing=True)
        peers = auth_dir / "peers"
        peers.mkdir()
        (peers / "alpha.token").write_text("peer-alpha-secret\n")
        (peers / "bravo.token").write_text("peer-bravo-secret\n")
        assert auth.verify_bearer("peer-alpha-secret") is True
        assert auth.verify_bearer("peer-bravo-secret") is True
        assert auth.verify_bearer("peer-charlie-secret") is False

    def test_empty_peer_file_does_not_authenticate_empty_bearer(self, auth_dir):
        from awm.services import auth
        auth.local_token(generate_if_missing=True)
        peers = auth_dir / "peers"
        peers.mkdir()
        (peers / "ghost.token").write_text("")
        assert auth.verify_bearer("") is False


class TestVerifyPeerBearer:
    def test_match_accepted(self, auth_dir):
        from awm.services import auth
        peers = auth_dir / "peers"
        peers.mkdir()
        (peers / "alpha.token").write_text("alpha-secret\n")
        assert auth.verify_peer_bearer("alpha-secret", "alpha") is True

    def test_mismatch_rejected(self, auth_dir):
        from awm.services import auth
        peers = auth_dir / "peers"
        peers.mkdir()
        (peers / "alpha.token").write_text("alpha-secret\n")
        (peers / "bravo.token").write_text("bravo-secret\n")
        # bravo's bearer presented but claiming to be alpha → rejected.
        assert auth.verify_peer_bearer("bravo-secret", "alpha") is False

    def test_missing_peer_rejected(self, auth_dir):
        from awm.services import auth
        (auth_dir / "peers").mkdir()
        assert auth.verify_peer_bearer("anything", "unknown") is False

    def test_path_traversal_rejected(self, auth_dir):
        from awm.services import auth
        (auth_dir / "peers").mkdir()
        (auth_dir / "auth.token").write_text("local-token\n")
        # Trying to claim a peer name that escapes the peers/ dir must fail.
        assert auth.verify_peer_bearer("local-token", "../auth") is False
        assert auth.verify_peer_bearer("local-token", ".hidden") is False

    def test_blank_claimed_peer_rejected(self, auth_dir):
        from awm.services import auth
        assert auth.verify_peer_bearer("anything", "") is False
        assert auth.verify_peer_bearer("anything", None) is False  # type: ignore


class TestChallenges:
    def test_round_trip(self, auth_dir):
        from awm.services import auth
        nonce = auth.mint_challenge("tony")
        assert auth.consume_challenge(nonce) == "tony"

    def test_single_use(self, auth_dir):
        from awm.services import auth
        nonce = auth.mint_challenge("operator")
        assert auth.consume_challenge(nonce) == "operator"
        # Already consumed.
        assert auth.consume_challenge(nonce) is None

    def test_expired_returns_none(self, auth_dir):
        from awm.services import auth
        nonce = auth.mint_challenge("operator", ttl=-1)
        assert auth.consume_challenge(nonce) is None

    def test_unknown_nonce_returns_none(self, auth_dir):
        from awm.services import auth
        assert auth.consume_challenge("never-minted") is None
        assert auth.consume_challenge(None) is None
        assert auth.consume_challenge("") is None

    def test_sweep_drops_expired(self, auth_dir):
        from awm.services import auth
        live = auth.mint_challenge("a")
        stale = auth.mint_challenge("b", ttl=-1)
        dropped = auth.sweep_challenges()
        assert dropped >= 1
        # Live nonce still consumable.
        assert auth.consume_challenge(live) == "a"
        assert auth.consume_challenge(stale) is None


class TestBootstrap:
    def test_bootstrap_creates_everything(self, auth_dir):
        from awm.services import auth
        info = auth.bootstrap()
        assert Path(info["token_file"]).exists()
        assert Path(info["tls_cert"]).exists()
        assert Path(info["tls_key"]).exists()
        assert info["tls_fingerprint"]
