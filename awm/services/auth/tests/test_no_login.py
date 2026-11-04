"""No login lives in auth: the passkey grant replaced the shared password.

The contract these pin: ``password`` and ``verify`` are gone, so is every
account verb, the public profile rotates nothing, and every older DB migrates
in place to a credential table with no login password and no lockout table
(fleet.md R84, users.md R130).
"""

from __future__ import annotations

import sqlite3

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture()
def auth(awm_workspace, monkeypatch):
    from awm.auth import service, store
    monkeypatch.delenv("AWM_AUTH_PROFILE", raising=False)

    class _S:
        session_ttl_hours = 1.0
        max_session_days = 30.0
        validity_hours = 24.0
        mint_cadence_hours = 12.0

    monkeypatch.setattr(service, "_settings", lambda: _S())
    store.init()
    store.ensure_secret()
    return service


def test_the_login_verbs_are_gone():
    from awm.auth import hub_adapter, service
    names = {f["name"] for f in hub_adapter.API_MANIFEST["functions"]}
    for verb in ("password", "verify", "user_add", "user_passwd", "user_disable", "user_list"):
        assert verb not in names and verb not in hub_adapter.HANDLERS
        assert not hasattr(service, f"h_{verb}")


def test_a_generation_holds_no_login_password(auth):
    from awm.auth import store
    gen = store.mint_generation(validity_seconds=3600)
    assert "login_password" not in gen and "login_password" not in store.latest()


def test_public_profile_hands_out_no_peer_credentials(auth, monkeypatch):
    from awm.auth import store
    monkeypatch.setenv("AWM_AUTH_PROFILE", "public")
    store.mint_generation(validity_seconds=3600)
    assert auth.h_edge_material({})["peer_credentials"] == []
    assert auth.h_status({})["rotation_enabled"] is False


async def test_public_profile_on_start_does_not_rotate(auth, monkeypatch):
    from awm.auth import store
    monkeypatch.setenv("AWM_AUTH_PROFILE", "public")
    await auth.on_start()
    assert store.latest() is None


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
    assert {"awm_users", "awm_login_fail"}.isdisjoint(_tables(path))


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
    assert {"awm_users", "awm_login_fail"}.isdisjoint(_tables(path))
    assert [t["id"] for t in store.device_live()] == ["aaaaaaaaaaaaaaaa"]
    rows = {t["id"]: t for t in store.device_list()}
    assert rows["bbbbbbbbbbbbbbbb"]["revoked_at"] is not None
    assert rows["cccccccccccccccc"]["revoked_at"] is not None
    assert rows["dddddddddddddddd"]["revoked_at"] == 7.0
    assert rows["eeeeeeeeeeeeeeee"]["revoked_at"] is not None


def test_a_v6_db_drops_its_login_passwords(awm_workspace):
    from awm.auth import store
    from awm.persistence.databases import service_db_path
    path = service_db_path("auth")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE awm_credentials (generation INTEGER PRIMARY KEY AUTOINCREMENT,"
        " login_password TEXT NOT NULL, peer_credential TEXT NOT NULL,"
        " minted_at REAL NOT NULL, expires_at REAL NOT NULL);"
        "CREATE TABLE awm_login_fail (key TEXT PRIMARY KEY, fails INTEGER NOT "
        "NULL DEFAULT 0, locked_until REAL NOT NULL DEFAULT 0, last_at REAL "
        "NOT NULL DEFAULT 0);"
        "CREATE TABLE awm_device_tokens (id TEXT PRIMARY KEY, username TEXT NOT "
        "NULL, label TEXT NOT NULL, secret_sha256 TEXT NOT NULL, created_at "
        "REAL NOT NULL, last_used_at REAL, revoked_at REAL, scope TEXT);"
        "CREATE TABLE schema_version (version INTEGER NOT NULL);"
        "INSERT INTO schema_version (version) VALUES (6);"
        "INSERT INTO awm_credentials VALUES (1, 'typed-daily', 'peer-x', 0, 9e12);")
    conn.commit()
    conn.close()
    store.init()
    assert "awm_login_fail" not in _tables(path)
    assert store.latest() == {"generation": 1, "peer_credential": "peer-x",
                              "minted_at": 0, "expires_at": 9e12}
