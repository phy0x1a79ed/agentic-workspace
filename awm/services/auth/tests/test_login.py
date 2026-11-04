"""Logins: who gets a session, who gets locked out, and what the public profile
refuses.

The contract these pin: the shared password mints ``operator`` on the default
profile and nothing on ``AWM_AUTH_PROFILE=public``; a ``verify`` that names a
username is refused and counts against its client IP; the lockout is keyed by
client IP and answers ``retry_after`` after the configured number of failures;
a v1 DB migrates in place, and a v5 DB loses its static accounts.
"""

from __future__ import annotations

import sqlite3

import pytest

from awm.config import tokens

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture()
def auth(awm_workspace, monkeypatch):
    from awm.auth import service, store
    monkeypatch.delenv("AWM_AUTH_PROFILE", raising=False)

    class _S:
        session_ttl_hours = 1.0
        max_session_days = 30.0
        validity_hours = 24.0
        lockout_threshold = 3
        lockout_minutes = 15.0
        mint_cadence_hours = 12.0
        push_enabled = False
        penpot_rotation_hour = 4
        penpot_rotation_enabled = True

    monkeypatch.setattr(service, "_settings", lambda: _S())
    store.init()
    store.ensure_secret()
    return service


@pytest.fixture()
def shared(auth):
    from awm.auth import store
    return store.mint_generation(validity_seconds=3600)["login_password"]


def test_shared_password_mints_operator(auth):
    from awm.auth import store
    gen = store.mint_generation(validity_seconds=3600)
    r = auth.h_verify({"password": gen["login_password"]})
    assert r["ok"] and r["sub"] == "operator"
    assert tokens.verify(store.ensure_secret(), r["token"])["sub"] == "operator"
    assert "peer_credentials" not in auth.h_edge_material({})


def test_a_verify_naming_a_username_is_refused(auth, shared):
    assert auth.h_verify({"username": "tony", "password": shared}) == {"ok": False}
    assert auth.h_verify({"username": "operator", "password": shared}) == {"ok": False}


def test_a_username_verify_counts_against_its_client_ip(auth, shared):
    for _ in range(3):
        r = auth.h_verify({"username": "tony", "password": shared,
                           "client_ip": "9.9.9.9"})
        assert r == {"ok": False}
    locked = auth.h_verify({"password": shared, "client_ip": "9.9.9.9"})
    assert locked["ok"] is False and locked["locked"] and locked["retry_after"] > 0


def test_lockout_is_by_client_ip(auth, shared):
    for _ in range(3):
        assert auth.h_verify({"password": "x", "client_ip": "9.9.9.9"}) == {"ok": False}
    r = auth.h_verify({"password": shared, "client_ip": "9.9.9.9"})
    assert r["ok"] is False and r["locked"]
    assert auth.h_verify({"password": shared, "client_ip": "1.1.1.1"})["ok"]


def test_no_client_ip_never_locks(auth, shared):
    for _ in range(5):
        assert auth.h_verify({"password": "x"}) == {"ok": False}
    assert auth.h_verify({"password": shared})["ok"]


def test_success_clears_the_counter(auth, shared):
    ip = {"client_ip": "9.9.9.9"}
    auth.h_verify({"password": "x", **ip})
    auth.h_verify({"password": "x", **ip})
    assert auth.h_verify({"password": shared, **ip})["ok"]
    auth.h_verify({"password": "x", **ip})
    auth.h_verify({"password": "x", **ip})
    assert "locked" not in auth.h_verify({"password": shared, **ip})


def test_status_counts_no_users(auth):
    assert "users" not in auth.h_status({})


def test_public_profile_accepts_no_login(auth, monkeypatch):
    from awm.auth import store
    monkeypatch.setenv("AWM_AUTH_PROFILE", "public")
    gen = store.mint_generation(validity_seconds=3600)
    assert auth.h_verify({"password": gen["login_password"]}) == {"ok": False}
    assert auth.h_verify({"username": "tony", "password": "x"}) == {"ok": False}
    assert auth.h_status({})["shared_password_enabled"] is False


async def test_public_profile_on_start_does_not_rotate(auth, monkeypatch):
    from awm.auth import store
    monkeypatch.setenv("AWM_AUTH_PROFILE", "public")
    await auth.on_start()
    assert store.latest() is None


def test_the_user_account_verbs_are_gone():
    from awm.auth import hub_adapter, service
    names = {f["name"]: f for f in hub_adapter.API_MANIFEST["functions"]}
    for verb in ("user_add", "user_passwd", "user_disable", "user_list"):
        assert verb not in names and verb not in hub_adapter.HANDLERS
        assert not hasattr(service, f"h_{verb}")
    assert [p["name"] for p in names["verify"]["params"]] == ["password", "client_ip"]


# --- migration -------------------------------------------------------------------


def _tables(path):
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()


def test_a_fresh_db_has_no_user_table(awm_workspace):
    from awm.auth import store
    from awm.persistence.databases import service_db_path
    store.init()
    assert "awm_users" not in _tables(service_db_path("auth"))


def test_v1_db_migrates_in_place(awm_workspace):
    from awm.auth import store
    from awm.persistence.databases import service_db_path
    path = service_db_path("auth")
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE awm_secret (id INTEGER PRIMARY KEY CHECK (id = 1), secret TEXT NOT NULL);"
        "CREATE TABLE awm_credentials (generation INTEGER PRIMARY KEY AUTOINCREMENT,"
        " login_password TEXT NOT NULL, peer_credential TEXT NOT NULL,"
        " minted_at REAL NOT NULL, expires_at REAL NOT NULL);"
        "CREATE TABLE schema_version (version INTEGER NOT NULL);"
        "INSERT INTO schema_version VALUES (1);"
        "INSERT INTO awm_secret VALUES (1, 'keep-me');")
    conn.commit(); conn.close()
    store.init()
    assert store.ensure_secret() == "keep-me"
    assert store.fail_get("ip:1.2.3.4")["fails"] == 0
    assert "awm_users" not in _tables(path)


def test_a_v5_db_drops_its_static_accounts(awm_workspace):
    """The accounts go, and so does every unscoped token: it spoke for a user
    account. A scoped token of a live owner stays live."""
    from awm.auth import store
    from awm.persistence.databases import service_db_path
    path = service_db_path("auth")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE awm_users (username TEXT PRIMARY KEY, pw_hash TEXT NOT "
        "NULL, pw_salt TEXT NOT NULL, pw_params TEXT NOT NULL, created_at REAL "
        "NOT NULL, disabled INTEGER NOT NULL DEFAULT 0);"
        "CREATE TABLE awm_login_fail (key TEXT PRIMARY KEY, fails INTEGER NOT "
        "NULL DEFAULT 0, locked_until REAL NOT NULL DEFAULT 0, last_at REAL "
        "NOT NULL DEFAULT 0);"
        "CREATE TABLE awm_device_tokens (id TEXT PRIMARY KEY, username TEXT NOT "
        "NULL, label TEXT NOT NULL, secret_sha256 TEXT NOT NULL, created_at "
        "REAL NOT NULL, last_used_at REAL, revoked_at REAL, scope TEXT);"
        "CREATE TABLE schema_version (version INTEGER NOT NULL);"
        "INSERT INTO schema_version (version) VALUES (5);"
        "INSERT INTO awm_users VALUES ('tony', 'h', 's', 'p', 0, 0);"
        "INSERT INTO awm_users VALUES ('ada', 'h', 's', 'p', 0, 1);"
        "INSERT INTO awm_login_fail VALUES ('ip:9.9.9.9', 2, 0, 0);"
        "INSERT INTO awm_device_tokens VALUES "
        "('aaaaaaaaaaaaaaaa', 'tony', 'live', 'd', 1, NULL, NULL, 'avatar-stage'),"
        "('eeeeeeeeeeeeeeee', 'tony', 'unscoped', 'd', 5, NULL, NULL, NULL),"
        "('bbbbbbbbbbbbbbbb', 'ada', 'disabled owner', 'd', 2, NULL, NULL, NULL),"
        "('cccccccccccccccc', 'ghost', 'missing owner', 'd', 3, NULL, NULL, NULL),"
        "('dddddddddddddddd', 'tony', 'revoked', 'd', 4, NULL, 7.0, NULL);")
    conn.commit()
    conn.close()
    store.init()
    assert "awm_users" not in _tables(path)
    assert store.fail_get("ip:9.9.9.9")["fails"] == 2
    assert [t["id"] for t in store.device_live()] == ["aaaaaaaaaaaaaaaa"]
    rows = {t["id"]: t for t in store.device_list()}
    assert rows["bbbbbbbbbbbbbbbb"]["revoked_at"] is not None
    assert rows["cccccccccccccccc"]["revoked_at"] is not None
    assert rows["dddddddddddddddd"]["revoked_at"] == 7.0
    assert rows["eeeeeeeeeeeeeeee"]["revoked_at"] is not None
