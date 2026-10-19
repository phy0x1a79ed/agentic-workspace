"""Device tokens: a revocable bearer an unattended device or app holds to act
as a user without storing a password.

What these pin: ``device_mint`` returns an ``awmut1.<id>.<secret>`` token once
and stores only the secret's SHA-256; ``device_list`` never returns the digest;
``device_revoke`` takes a token out of ``edge_material`` while keeping its row;
``edge_material`` carries only live tokens of enabled users and records the
use the edge reports; the operator manages anyone's tokens, a user only their
own, and every other caller none; and a v3 DB gains the table in place.
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
    monkeypatch.setattr(service, "_SCRYPT_N", 2 ** 4)

    class _S:
        session_ttl_hours = 1.0
        max_session_days = 30.0
        validity_hours = 24.0
        mint_cadence_hours = 12.0
        push_enabled = False
        penpot_rotation_hour = 4
        penpot_rotation_enabled = True

    monkeypatch.setattr(service, "_settings", lambda: _S())
    store.init()
    store.ensure_secret()
    service.set_password("tony")
    service.set_password("ada")
    return service


def _mint(auth, username="tony", label="kiosk", as_=None):
    return auth.h_device_mint({"username": username, "label": label}, as_)


def _live(auth):
    return {t["id"]: t for t in auth.h_edge_material({})["device_tokens"]}


# --- mint ----------------------------------------------------------------------


def test_mint_returns_the_token_once_and_stores_only_its_digest(auth):
    from awm.auth import store
    out = _mint(auth)
    token_id, secret = usertoken.parse(out["token"])
    assert out["id"] == token_id and len(token_id) == 16
    assert out["username"] == "tony" and out["label"] == "kiosk"
    assert out["last_used_at"] is None and out["revoked_at"] is None
    row = store.device_get(token_id)
    assert row["secret_sha256"] == usertoken.digest(secret)
    assert secret not in json.dumps(row)
    listed = auth.h_device_list({})["tokens"]
    assert "token" not in listed[0] and "secret_sha256" not in listed[0]
    assert set(listed[0]) == {"id", "username", "label", "created_at",
                              "last_used_at", "revoked_at"}


def test_each_mint_is_a_new_token(auth):
    a, b = _mint(auth), _mint(auth)
    assert a["id"] != b["id"] and a["token"] != b["token"]


@pytest.mark.parametrize("args,match", [
    ({"username": "tony"}, "label"),
    ({"username": "tony", "label": "x" * 65}, "longer"),
    ({"label": "kiosk"}, "username"),
    ({"username": "nobody", "label": "kiosk"}, "no such user"),
    ({"username": "Not A Name", "label": "kiosk"}, "username"),
    ({"username": "operator", "label": "kiosk"}, "machine"),
    ({"username": "peer", "label": "kiosk"}, "machine"),
])
def test_mint_refuses_bad_arguments(auth, args, match):
    with pytest.raises(ValueError, match=match):
        auth.h_device_mint(args, None)


def test_mint_refuses_a_disabled_user(auth):
    auth.h_user_disable({"username": "ada"})
    with pytest.raises(ValueError, match="disabled"):
        _mint(auth, "ada")


# --- edge material ---------------------------------------------------------------


def test_edge_material_carries_live_tokens_as_id_sub_digest(auth):
    out = _mint(auth)
    _, secret = usertoken.parse(out["token"])
    assert _live(auth) == {out["id"]: {"id": out["id"], "sub": "tony",
                                       "sha256": usertoken.digest(secret)}}


def test_revoke_drops_the_token_from_the_material_and_keeps_the_row(auth):
    keep, gone = _mint(auth, label="a"), _mint(auth, label="b")
    revoked = auth.h_device_revoke({"id": gone["id"]})
    assert revoked["revoked_at"] is not None
    assert set(_live(auth)) == {keep["id"]}
    listed = {t["id"]: t for t in auth.h_device_list({})["tokens"]}
    assert listed[gone["id"]]["revoked_at"] == revoked["revoked_at"]
    again = auth.h_device_revoke({"id": gone["id"]})
    assert again["revoked_at"] == revoked["revoked_at"]


def test_a_disabled_users_tokens_leave_the_material_until_re_enabled(auth):
    out = _mint(auth, "ada")
    auth.h_user_disable({"username": "ada"})
    assert out["id"] not in _live(auth)
    auth.h_user_disable({"username": "ada", "disabled": False})
    assert out["id"] in _live(auth)


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
    assert auth.h_edge_material({"device_used": ["x"]})["device_tokens"]


def test_edge_material_still_refuses_an_edge_caller_with_a_report(auth):
    with pytest.raises(PermissionError):
        auth.h_edge_material({"device_used": {}}, "user:tony")


# --- who may manage whose tokens ------------------------------------------------------


def test_a_user_mints_lists_and_revokes_their_own(auth):
    out = _mint(auth, "tony", as_="user:tony")
    assert out["username"] == "tony"
    _mint(auth, "ada")
    listed = auth.h_device_list({}, "user:tony")["tokens"]
    assert [t["id"] for t in listed] == [out["id"]]
    assert auth.h_device_list({"username": "tony"}, "user:tony")["tokens"] == listed
    assert auth.h_device_revoke({"id": out["id"]}, "user:tony")["revoked_at"]


def test_a_user_cannot_touch_another_users_tokens(auth):
    adas = _mint(auth, "ada")
    with pytest.raises(PermissionError):
        _mint(auth, "ada", as_="user:tony")
    with pytest.raises(PermissionError):
        auth.h_device_list({"username": "ada"}, "user:tony")
    # Indistinguishable from an id that does not exist.
    with pytest.raises(ValueError, match="no such device token"):
        auth.h_device_revoke({"id": adas["id"]}, "user:tony")
    assert adas["id"] in _live(auth)


def test_the_operator_manages_anyones_tokens(auth):
    a, b = _mint(auth, "ada"), _mint(auth, "tony")
    assert {t["id"] for t in auth.h_device_list({})["tokens"]} == {a["id"], b["id"]}
    assert [t["id"] for t in auth.h_device_list({"username": "ada"})["tokens"]] == [a["id"]]
    auth.h_device_revoke({"id": a["id"]})
    assert set(_live(auth)) == {b["id"]}


@pytest.mark.parametrize("as_", ["peer", "peer:mira", "user:operator", "user:peer",
                                 "agent-slug", "user:Not A Name", ""])
def test_machines_and_unknown_callers_manage_nothing(auth, as_):
    out = _mint(auth)
    with pytest.raises(PermissionError):
        _mint(auth, as_=as_)
    with pytest.raises(PermissionError):
        auth.h_device_list({}, as_)
    with pytest.raises(PermissionError):
        auth.h_device_revoke({"id": out["id"]}, as_)


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
    assert names["device_mint"]["effect"] == "secret"


# --- migration ------------------------------------------------------------------


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
        "CREATE TABLE awm_penpot (username TEXT PRIMARY KEY, email TEXT NOT "
        "NULL, password TEXT NOT NULL, rotated_at REAL NOT NULL, created_at "
        "REAL NOT NULL);"
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
