"""The Penpot credential awm holds on a person's behalf.

The contract these pin: recording a credential stores it and returns no
password; a session is fetched from Penpot once and then cached; ``refresh``
bypasses that cache; a rotation writes the store only after Penpot has agreed;
one wedged account does not stop the rest; and a v2 DB migrates in place.

Penpot itself is a stub — these are about the policy, not the wire. The wire is
proven end to end against a real stack.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


class FakePenpot:
    """A Penpot stack that only knows profiles, passwords and session ids.

    Faithful about the two behaviours the design turns on: a wrong old password
    is refused, and changing a password invalidates every session *except* the
    one that made the change.
    """

    def __init__(self, **profiles: str):
        self.profiles = dict(profiles)          # email -> password
        self.sessions: dict[str, str] = {}      # token -> email
        self.logins = 0
        self._n = 0

    def _mint(self, email: str) -> str:
        self._n += 1
        token = f"tok{self._n}"
        self.sessions[token] = email
        return token

    async def login(self, email: str, password: str) -> str:
        self.logins += 1
        if self.profiles.get(email) != password:
            raise RuntimeError("login failed: HTTP 400: invalid-credentials")
        return self._mint(email)

    async def change_password(self, token: str, old: str, new: str) -> None:
        email = self.sessions.get(token)
        if email is None:
            raise RuntimeError("update-profile-password: HTTP 401: authentication")
        if self.profiles.get(email) != old:
            raise RuntimeError(
                "update-profile-password: HTTP 400: old-password-not-match")
        self.profiles[email] = new
        # invalidate-others: every session for this profile but this one.
        self.sessions = {t: e for t, e in self.sessions.items()
                         if e != email or t == token}


@pytest.fixture()
def auth(awm_workspace, monkeypatch):
    from awm.auth import penpot, service, store
    monkeypatch.delenv("AWM_AUTH_PROFILE", raising=False)
    store.init()
    store.ensure_secret()
    penpot._sessions.clear()
    penpot._locks.clear()
    service._penpot_status.update(
        last_rotation_at=None, last_rotation_ok=None, failures={})
    return service


@pytest.fixture()
def stack(auth, monkeypatch):
    from awm.auth import penpot
    fake = FakePenpot(**{"tony@example.test": "Old-Pass-1"})
    monkeypatch.setattr(penpot, "login", fake.login)
    monkeypatch.setattr(penpot, "change_password", fake.change_password)
    monkeypatch.setattr(penpot, "PenpotError", RuntimeError)
    return fake


def _record(auth, name="tony", email="tony@example.test", password="Old-Pass-1"):
    return auth.h_penpot_record(
        {"username": name, "email": email, "password": password})


# --- recording ---------------------------------------------------------------


def test_record_stores_the_credential_and_returns_no_password(auth):
    from awm.auth import store
    out = _record(auth)
    assert out["username"] == "tony" and out["email"] == "tony@example.test"
    assert "password" not in out
    assert store.penpot_get("tony")["password"] == "Old-Pass-1"


def test_record_overwrites_so_a_drifted_credential_can_be_repaired(auth):
    from awm.auth import store
    _record(auth)
    _record(auth, password="Repaired-1")
    assert store.penpot_get("tony")["password"] == "Repaired-1"


def test_record_rejects_a_bad_username(auth):
    with pytest.raises(ValueError):
        _record(auth, name="Tony")


def test_penpot_list_carries_no_secrets(auth):
    _record(auth)
    entry = auth.h_penpot_list({})["credentials"][0]
    assert entry["username"] == "tony"
    assert "password" not in entry


# --- sessions ----------------------------------------------------------------


async def test_session_logs_in_once_and_then_caches(auth, stack):
    _record(auth)
    first = await auth.h_penpot_session({"username": "tony"})
    assert first["cookie_name"] == "auth-token"
    second = await auth.h_penpot_session({"username": "tony"})
    assert second["token"] == first["token"]
    assert stack.logins == 1


async def test_refresh_bypasses_the_cache(auth, stack):
    _record(auth)
    first = await auth.h_penpot_session({"username": "tony"})
    again = await auth.h_penpot_session({"username": "tony", "refresh": True})
    assert again["token"] != first["token"]
    assert stack.logins == 2


async def test_session_for_an_unrecorded_user_is_an_error(auth, stack):
    with pytest.raises(ValueError):
        await auth.h_penpot_session({"username": "ghost"})


async def test_recording_again_drops_a_stale_cached_session(auth, stack):
    _record(auth)
    await auth.h_penpot_session({"username": "tony"})
    stack.profiles["tony@example.test"] = "Repaired-1"
    _record(auth, password="Repaired-1")
    out = await auth.h_penpot_session({"username": "tony"})
    assert stack.logins == 2 and out["token"] == "tok2"


# --- rotation ----------------------------------------------------------------


async def test_rotation_changes_penpot_first_then_the_store(auth, stack):
    from awm.auth import store
    _record(auth)
    await auth.h_penpot_rotate({"username": "tony"})
    stored = store.penpot_get("tony")["password"]
    assert stored != "Old-Pass-1"
    assert stack.profiles["tony@example.test"] == stored


async def test_a_rotated_password_still_logs_in(auth, stack):
    _record(auth)
    await auth.h_penpot_rotate({})
    from awm.auth import penpot
    penpot.forget("tony")
    out = await auth.h_penpot_session({"username": "tony"})
    assert out["token"] in stack.sessions


async def test_rotation_leaves_the_store_alone_when_penpot_refuses(auth, stack):
    from awm.auth import store
    _record(auth, password="Drifted-1")   # not what the stack holds
    with pytest.raises(RuntimeError):
        await auth.h_penpot_rotate({"username": "tony"})
    assert store.penpot_get("tony")["password"] == "Drifted-1"


async def test_one_wedged_account_does_not_stop_the_others(auth, stack):
    from awm.auth import store
    stack.profiles["steven@example.test"] = "Steven-Pass-1"
    _record(auth, password="Drifted-1")
    _record(auth, name="steven", email="steven@example.test",
            password="Steven-Pass-1")
    out = await auth.h_penpot_rotate({})
    assert out["rotated"] == ["steven"] and "tony" in out["failed"]
    assert store.penpot_get("steven")["password"] != "Steven-Pass-1"
    assert auth.h_status({})["penpot_last_rotation_ok"] is False
    assert "tony" in auth.h_status({})["penpot_failures"]


async def test_rotation_recovers_from_a_session_penpot_forgot(auth, stack):
    """A cached token can stop working; a wrong stored password cannot be
    retried away. The retry must distinguish them, not paper over both."""
    _record(auth)
    await auth.h_penpot_session({"username": "tony"})
    stack.sessions.clear()               # every session gone, password intact
    await auth.h_penpot_rotate({"username": "tony"})
    assert stack.logins == 2


async def test_the_password_generator_satisfies_penpot(auth):
    from awm.auth import penpot
    for _ in range(50):
        pw = penpot.new_password()
        assert len(pw) >= 8 and not any(c.isspace() for c in pw)
        assert any(c.islower() for c in pw) and any(c.isupper() for c in pw)
        assert any(c.isdigit() for c in pw)
        assert any(not c.isalnum() for c in pw)


# --- migration ---------------------------------------------------------------


def test_a_v2_db_gains_the_penpot_table(awm_workspace, monkeypatch):
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
        "INSERT INTO schema_version (version) VALUES (2);")
    conn.commit()
    conn.close()
    store.init()
    assert store.penpot_list() == []
    store.penpot_upsert("tony", email="t@example.test", password="x")
    assert store.penpot_get("tony")["email"] == "t@example.test"


# --- the nightly schedule ----------------------------------------------------


def _at(y, mo, d, h, mi=0):
    import time as _t
    return _t.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


def test_the_schedule_lands_on_the_next_local_occurrence(auth):
    now = _at(2026, 8, 29, 1, 30)
    assert auth._next_rotation_at(4, now) == _at(2026, 8, 29, 4)


def test_a_boundary_already_past_moves_to_tomorrow(auth):
    now = _at(2026, 8, 29, 4, 30)
    assert auth._next_rotation_at(4, now) == _at(2026, 8, 30, 4)


def test_the_boundary_itself_is_not_now(auth):
    """Sleeping zero seconds at the boundary would rotate twice in a row."""
    now = _at(2026, 8, 29, 4)
    assert auth._next_rotation_at(4, now) == _at(2026, 8, 30, 4)


def test_the_schedule_crosses_a_month_end(auth):
    now = _at(2026, 8, 31, 23)
    assert auth._next_rotation_at(4, now) == _at(2026, 9, 1, 4)


def test_a_box_that_missed_the_hour_is_overdue(auth):
    import time as _t
    _record(auth)
    assert auth._penpot_overdue() == []
    from awm.auth import store
    store.penpot_set_password("tony", "Old-Pass-1", now=_t.time() - 90000)
    assert auth._penpot_overdue() == ["tony"]


async def test_the_loop_catches_up_before_it_starts_waiting(auth, stack, monkeypatch):
    """A box that was off at 04:00 rotates on its next start rather than
    skipping a day — the failure mode is silent, so it gets its own test."""
    import asyncio
    import time as _t
    from awm.auth import store
    _record(auth)
    store.penpot_set_password("tony", "Old-Pass-1", now=_t.time() - 90000)

    slept = asyncio.Event()

    async def _sleep(_seconds):
        slept.set()
        await asyncio.Future()          # park here; the test only wants the catch-up

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    task = asyncio.create_task(auth._penpot_rotation_loop())
    await asyncio.wait_for(slept.wait(), timeout=5)
    task.cancel()

    assert store.penpot_get("tony")["password"] != "Old-Pass-1"
    assert auth._penpot_status["last_rotation_ok"] is True


async def test_rotation_can_be_held_still(auth, stack, monkeypatch):
    import time as _t
    from awm.auth import store
    _record(auth)
    store.penpot_set_password("tony", "Old-Pass-1", now=_t.time() - 90000)

    real = auth._settings()

    class _Held:
        def __getattr__(self, name):
            return getattr(real, name)
        penpot_rotation_enabled = False

    monkeypatch.setattr(auth, "_settings", lambda: _Held())
    task = None
    try:
        import asyncio
        task = asyncio.create_task(auth._penpot_rotation_loop())
        await asyncio.sleep(0)
    finally:
        if task is not None:
            task.cancel()
    assert store.penpot_get("tony")["password"] == "Old-Pass-1"


def test_status_reports_the_schedule(auth):
    status = auth.h_status({})
    assert status["penpot_rotation_hour"] == 4
    assert status["penpot_rotation_enabled"] is True
    assert status["penpot_next_rotation_at"] > time.time()
