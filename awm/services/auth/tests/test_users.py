"""Per-user accounts: who gets a session, who gets locked out, and what the
public profile refuses.

The contract these pin: a ``verify`` with a username mints ``sub=<username>``;
the shared password keeps minting ``operator`` on the default profile and mints
nothing on ``AWM_AUTH_PROFILE=public``; the lockout answers ``retry_after``
after the configured number of failures; a v1 DB migrates in place.
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
    # Cheap scrypt for the suite; the parameters are recorded per row anyway.
    monkeypatch.setattr(service, "_SCRYPT_N", 2 ** 4)

    class _S:
        session_ttl_hours = 1.0
        max_session_days = 30.0
        validity_hours = 24.0
        lockout_threshold = 3
        lockout_minutes = 15.0
        mint_cadence_hours = 12.0
        push_enabled = False

    monkeypatch.setattr(service, "_settings", lambda: _S())
    store.init()
    store.ensure_secret()
    return service


def _claims(result):
    from awm.auth import store
    return tokens.verify(store.ensure_secret(), result["token"])


def test_add_then_verify_mints_the_username(auth):
    created = auth.h_user_add({"username": "tony"})
    r = auth.h_verify({"username": "tony", "password": created["password"]})
    assert r["ok"] and r["sub"] == "tony"
    assert _claims(r)["sub"] == "tony"


def test_wrong_password_and_unknown_user_fail_alike(auth):
    auth.h_user_add({"username": "tony"})
    assert auth.h_verify({"username": "tony", "password": "nope"}) == {"ok": False}
    assert auth.h_verify({"username": "ghost", "password": "nope"}) == {"ok": False}


def test_add_refuses_duplicates_and_bad_names(auth):
    auth.h_user_add({"username": "tony"})
    with pytest.raises(ValueError):
        auth.h_user_add({"username": "tony"})
    with pytest.raises(ValueError):
        auth.h_user_add({"username": "Tony"})
    with pytest.raises(ValueError):
        auth.h_user_add({"username": "../etc"})


def test_passwd_replaces_and_disable_refuses(auth):
    first = auth.h_user_add({"username": "steven"})["password"]
    second = auth.h_user_passwd({"username": "steven"})["password"]
    assert first != second
    assert not auth.h_verify({"username": "steven", "password": first})["ok"]
    assert auth.h_verify({"username": "steven", "password": second})["ok"]
    auth.h_user_disable({"username": "steven"})
    assert not auth.h_verify({"username": "steven", "password": second})["ok"]
    auth.h_user_disable({"username": "steven", "disabled": False})
    assert auth.h_verify({"username": "steven", "password": second})["ok"]
    listed = auth.h_user_list({})["users"]
    assert [u["username"] for u in listed] == ["steven"]
    assert "pw_hash" not in listed[0]


def test_lockout_by_username_then_release(auth):
    pw = auth.h_user_add({"username": "tony"})["password"]
    for _ in range(3):
        assert auth.h_verify({"username": "tony", "password": "x"}) == {"ok": False}
    locked = auth.h_verify({"username": "tony", "password": pw})
    assert locked["ok"] is False and locked["locked"] and locked["retry_after"] > 0
    auth.h_user_passwd({"username": "tony"})  # clears the lock
    assert "locked" not in auth.h_verify({"username": "tony", "password": "x"})


def test_lockout_by_client_ip_spans_usernames(auth):
    pw = auth.h_user_add({"username": "tony"})["password"]
    for name in ("a", "b", "c"):
        auth.h_verify({"username": name, "password": "x", "client_ip": "9.9.9.9"})
    r = auth.h_verify({"username": "tony", "password": pw, "client_ip": "9.9.9.9"})
    assert r["ok"] is False and r["locked"]
    assert auth.h_verify({"username": "tony", "password": pw, "client_ip": "1.1.1.1"})["ok"]


def test_success_clears_the_counter(auth):
    pw = auth.h_user_add({"username": "tony"})["password"]
    auth.h_verify({"username": "tony", "password": "x"})
    auth.h_verify({"username": "tony", "password": "x"})
    assert auth.h_verify({"username": "tony", "password": pw})["ok"]
    auth.h_verify({"username": "tony", "password": "x"})
    auth.h_verify({"username": "tony", "password": "x"})
    assert "locked" not in auth.h_verify({"username": "tony", "password": pw})


def test_shared_password_still_mints_operator(auth):
    from awm.auth import store
    gen = store.mint_generation(validity_seconds=3600)
    r = auth.h_verify({"password": gen["login_password"]})
    assert r["ok"] and r["sub"] == "operator"
    assert auth.h_edge_material({})["peer_credentials"] == [gen["peer_credential"]]


def test_public_profile_never_mints_the_shared_path(auth, monkeypatch):
    from awm.auth import store
    monkeypatch.setenv("AWM_AUTH_PROFILE", "public")
    gen = store.mint_generation(validity_seconds=3600)
    assert auth.h_verify({"password": gen["login_password"]}) == {"ok": False}
    assert auth.h_edge_material({})["peer_credentials"] == []
    pw = auth.h_user_add({"username": "tony"})["password"]
    assert auth.h_verify({"username": "tony", "password": pw})["sub"] == "tony"
    assert auth.h_status({})["shared_password_enabled"] is False


async def test_public_profile_on_start_does_not_rotate(auth, monkeypatch):
    from awm.auth import store
    monkeypatch.setenv("AWM_AUTH_PROFILE", "public")
    await auth.on_start()
    assert store.latest() is None


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
    assert store.user_list() == []
    assert store.fail_get("u:x")["fails"] == 0
