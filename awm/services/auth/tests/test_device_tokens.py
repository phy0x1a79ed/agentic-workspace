"""Device tokens: a revocable bearer an unattended device or app holds instead
of a password.

What these pin: ``device_mint`` returns an ``awmut1.<id>.<secret>`` token once,
owned by ``device:<id>`` and bound to a scope, and stores only the secret's SHA-256; ``device_list``
never returns the digest; ``device_revoke`` takes a token out of
``edge_material`` while keeping its row; ``edge_material`` carries every live
token and records the use the edge reports; a scoped token goes only to an
edge that names its scope; only the operator manages tokens, and every caller
that crossed an edge manages none; and a v3 or v4 DB gains the table and its
scope in place.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from awm.config import usertoken

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
        push_enabled = False

    monkeypatch.setattr(service, "_settings", lambda: _S())
    store.init()
    store.ensure_secret()
    return service


def _mint(auth, label="kiosk", as_=None):
    return auth.h_device_mint({"label": label, "scope": usertoken.AVATAR_STAGE}, as_)


def _live(auth):
    return {t["id"]: t for t in auth.h_edge_material(
        {"token_scopes": [usertoken.AVATAR_STAGE]})["device_tokens"]}


# --- mint ----------------------------------------------------------------------


def test_mint_returns_the_token_once_and_stores_only_its_digest(auth):
    from awm.auth import store
    out = _mint(auth)
    token_id, secret = usertoken.parse(out["token"])
    assert out["id"] == token_id and len(token_id) == 16
    assert out["username"] == f"device:{token_id}" and out["label"] == "kiosk"
    assert out["last_used_at"] is None and out["revoked_at"] is None
    row = store.device_get(token_id)
    assert row["secret_sha256"] == usertoken.digest(secret)
    assert secret not in json.dumps(row)
    listed = auth.h_device_list({})["tokens"]
    assert "token" not in listed[0] and "secret_sha256" not in listed[0]
    assert set(listed[0]) == {"id", "username", "label", "created_at",
                              "last_used_at", "revoked_at", "scope"}
    assert listed[0]["scope"] == usertoken.AVATAR_STAGE


def test_each_mint_is_a_new_token(auth):
    a, b = _mint(auth), _mint(auth)
    assert a["id"] != b["id"] and a["token"] != b["token"]


@pytest.mark.parametrize("args,match", [
    ({"scope": usertoken.AVATAR_STAGE}, "label"),
    ({"label": "   ", "scope": usertoken.AVATAR_STAGE}, "label"),
    ({"label": "x" * 65, "scope": usertoken.AVATAR_STAGE}, "longer"),
    ({"label": "phone"}, "needs a scope"),
])
def test_mint_refuses_bad_arguments(auth, args, match):
    with pytest.raises(ValueError, match=match):
        auth.h_device_mint(args, None)


def test_mint_refuses_an_unknown_scope(auth):
    with pytest.raises(ValueError, match="needs a scope"):
        auth.h_device_mint({"label": "phone", "scope": "everything"}, None)


def test_a_scoped_mint_records_its_scope(auth):
    out = auth.h_device_mint({"label": "phone",
                              "scope": usertoken.AVATAR_STAGE}, None)
    assert out["scope"] == usertoken.AVATAR_STAGE
    assert auth.h_device_list({})["tokens"][0]["scope"] == usertoken.AVATAR_STAGE


def test_a_device_token_never_speaks_for_the_operator(auth):
    out = auth.h_device_mint({"username": "operator", "label": "kiosk",
                              "scope": usertoken.AVATAR_STAGE}, None)
    assert out["username"] == f"device:{out['id']}"


# --- edge material ---------------------------------------------------------------


def test_edge_material_carries_live_tokens_as_id_sub_digest(auth):
    out = _mint(auth)
    _, secret = usertoken.parse(out["token"])
    assert _live(auth) == {out["id"]: {"id": out["id"], "sub": f"device:{out['id']}",
                                       "sha256": usertoken.digest(secret),
                                       "scope": usertoken.AVATAR_STAGE}}


def test_a_scoped_token_goes_only_to_an_edge_that_enforces_its_scope(auth):
    """An edge that cannot read the scope would honour the token everywhere."""
    scoped = _mint(auth, label="phone")
    for scopes in ([], ["other"], "avatar-stage", None):
        got = auth.h_edge_material({"token_scopes": scopes})["device_tokens"]
        assert got == [], scopes
    assert _live(auth)[scoped["id"]]["scope"] == usertoken.AVATAR_STAGE


def test_revoke_drops_the_token_from_the_material_and_keeps_the_row(auth):
    keep, gone = _mint(auth, label="a"), _mint(auth, label="b")
    revoked = auth.h_device_revoke({"id": gone["id"]})
    assert revoked["revoked_at"] is not None
    assert set(_live(auth)) == {keep["id"]}
    listed = {t["id"]: t for t in auth.h_device_list({})["tokens"]}
    assert listed[gone["id"]]["revoked_at"] == revoked["revoked_at"]
    again = auth.h_device_revoke({"id": gone["id"]})
    assert again["revoked_at"] == revoked["revoked_at"]


def test_revoke_of_an_unknown_id_is_an_error(auth):
    with pytest.raises(ValueError, match="no such device token"):
        auth.h_device_revoke({"id": "0123456789abcdef"})
    with pytest.raises(ValueError, match="id is required"):
        auth.h_device_revoke({})


def test_reported_use_moves_last_used_forward_only(auth):
    out = _mint(auth)
    auth.h_edge_material({"device_used": {out["id"]: 2000.0}})
    assert auth.h_device_list({})["tokens"][0]["last_used_at"] == 2000.0
    auth.h_edge_material({"device_used": {out["id"]: 1000.0, "junk": "x",
                                          "0123456789abcdef": 5.0}})
    assert auth.h_device_list({})["tokens"][0]["last_used_at"] == 2000.0


def test_a_report_that_is_not_a_mapping_is_ignored(auth):
    _mint(auth)
    assert auth.h_edge_material({"device_used": ["x"],
                                 "token_scopes": [usertoken.AVATAR_STAGE]})["device_tokens"]


def test_edge_material_still_refuses_an_edge_caller_with_a_report(auth):
    with pytest.raises(PermissionError):
        auth.h_edge_material({"device_used": {}}, "user:tony")


# --- who may manage tokens ------------------------------------------------------------


def test_the_operator_manages_every_token(auth):
    a, b = _mint(auth, "a"), _mint(auth, "b")
    assert {t["id"] for t in auth.h_device_list({})["tokens"]} == {a["id"], b["id"]}
    auth.h_device_revoke({"id": a["id"]})
    assert set(_live(auth)) == {b["id"]}


@pytest.mark.parametrize("as_", ["user:tony", "peer", "peer:mira", "user:operator",
                                 "operator", "agent-slug", "user:Not A Name", ""])
def test_every_caller_across_an_edge_manages_nothing(auth, as_):
    out = _mint(auth)
    with pytest.raises(PermissionError):
        _mint(auth, as_=as_)
    with pytest.raises(PermissionError):
        auth.h_device_list({}, as_)
    with pytest.raises(PermissionError):
        auth.h_device_revoke({"id": out["id"]}, as_)
    assert out["id"] in _live(auth)


def test_the_handlers_take_the_caller_identity_from_the_adapter():
    import inspect
    from awm.auth import service
    for fn in (service.h_device_mint, service.h_device_list, service.h_device_revoke):
        assert len(inspect.signature(fn).parameters) >= 2, fn.__name__


def test_the_verbs_stay_off_the_agent_surface():
    from awm.auth import hub_adapter
    names = {f["name"]: f for f in hub_adapter.API_MANIFEST["functions"]}
    for verb in ("device_mint", "device_list", "device_revoke"):
        assert names[verb]["surfaces"] == ["cli", "http"], verb
        assert "username" not in {p["name"] for p in names[verb]["params"]}, verb
    assert names["device_mint"]["effect"] == "secret"


# --- migration ------------------------------------------------------------------


def test_a_v4_db_gains_the_scope_column(awm_workspace):
    from awm.auth import store
    from awm.persistence.databases import service_db_path
    path = service_db_path("auth")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE awm_users (username TEXT PRIMARY KEY, pw_hash TEXT NOT "
        "NULL, pw_salt TEXT NOT NULL, pw_params TEXT NOT NULL, created_at REAL "
        "NOT NULL, disabled INTEGER NOT NULL DEFAULT 0);"
        "CREATE TABLE awm_device_tokens (id TEXT PRIMARY KEY, username TEXT NOT "
        "NULL, label TEXT NOT NULL, secret_sha256 TEXT NOT NULL, created_at "
        "REAL NOT NULL, last_used_at REAL, revoked_at REAL);"
        "CREATE TABLE schema_version (version INTEGER NOT NULL);"
        "INSERT INTO schema_version (version) VALUES (4);"
        "INSERT INTO awm_users VALUES ('tony', 'h', 's', 'p', 0, 0);"
        "INSERT INTO awm_device_tokens VALUES ('0123456789abcdef', 'tony', 'x', "
        "'d', 0, NULL, NULL);")
    conn.commit()
    conn.close()
    store.init()
    assert store.device_get("0123456789abcdef")["scope"] is None
    store.device_insert("fedcba9876543210", username="tony", label="y",
                        secret_sha256="e", scope=usertoken.AVATAR_STAGE)
    assert store.device_get("fedcba9876543210")["scope"] == usertoken.AVATAR_STAGE


def test_a_v3_db_gains_the_device_token_table(awm_workspace):
    from awm.auth import store
    from awm.persistence.databases import service_db_path
    path = service_db_path("auth")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE awm_secret (id INTEGER PRIMARY KEY CHECK (id = 1), "
        "secret TEXT NOT NULL);"
        "CREATE TABLE awm_credentials (generation INTEGER PRIMARY KEY "
        "AUTOINCREMENT, login_password TEXT NOT NULL, peer_credential TEXT "
        "NOT NULL, minted_at REAL NOT NULL, expires_at REAL NOT NULL);"
        "CREATE TABLE awm_users (username TEXT PRIMARY KEY, pw_hash TEXT NOT "
        "NULL, pw_salt TEXT NOT NULL, pw_params TEXT NOT NULL, created_at REAL "
        "NOT NULL, disabled INTEGER NOT NULL DEFAULT 0);"
        "CREATE TABLE awm_login_fail (key TEXT PRIMARY KEY, fails INTEGER NOT "
        "NULL DEFAULT 0, locked_until REAL NOT NULL DEFAULT 0, last_at REAL "
        "NOT NULL DEFAULT 0);"
        "CREATE TABLE schema_version (version INTEGER NOT NULL);"
        "INSERT INTO schema_version (version) VALUES (3);"
        "INSERT INTO awm_users VALUES ('tony', 'h', 's', 'p', 0, 0);")
    conn.commit()
    conn.close()
    store.init()
    assert store.device_list() == []
    store.device_insert("0123456789abcdef", username="tony", label="x",
                        secret_sha256="d")
    assert [t["id"] for t in store.device_live()] == ["0123456789abcdef"]


def test_the_v6_migration_drops_the_users_and_the_plaintext_penpot_tables(awm_workspace):
    from awm.auth import store
    from awm.persistence.databases import service_db_path
    path = service_db_path("auth")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE awm_users (username TEXT PRIMARY KEY, pw_hash TEXT NOT "
        "NULL, pw_salt TEXT NOT NULL, pw_params TEXT NOT NULL, created_at REAL "
        "NOT NULL, disabled INTEGER NOT NULL DEFAULT 0);"
        "CREATE TABLE awm_device_tokens (id TEXT PRIMARY KEY, username TEXT NOT "
        "NULL, label TEXT NOT NULL, secret_sha256 TEXT NOT NULL, created_at "
        "REAL NOT NULL, last_used_at REAL, revoked_at REAL, scope TEXT);"
        "CREATE TABLE awm_penpot (username TEXT PRIMARY KEY, email TEXT NOT NULL,"
        " password TEXT NOT NULL, rotated_at REAL NOT NULL, created_at REAL NOT NULL);"
        "INSERT INTO awm_penpot VALUES ('tony', 't@x', 'plaintext', 0, 0);"
        "CREATE TABLE schema_version (version INTEGER NOT NULL);"
        "INSERT INTO schema_version (version) VALUES (5);")
    conn.commit()
    conn.close()
    store.init()
    conn = sqlite3.connect(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    conn.close()
    assert not tables & {"awm_penpot", "awm_users"}
    assert version == 6
