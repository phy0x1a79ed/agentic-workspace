"""The approver on alcor: every action Tony takes passes through here (fleet.md R97, queue.md R64, R65).

Three kinds of proof meet here. A device key signs each message on the device's chain.
A passkey assertion authorizes a request: a batch of actions, a window of a stated
scope, or both. A risk signal closes the windows and asks for the passkey again. Once
an action passes, the approver alone turns it into a store action, a 2fa call, a grant
or an order. The store runs in the same process, so no tool verb reaches its actions.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

from verify_grant import b64u, canon, unb64u

from awm.operations.approver import passkeys
from awm.operations.approver.chain import (MESSAGE_FIELDS, fresh_head, next_head, passkey_challenge,
                                unlock_challenge)
from awm.operations.approver.keys import Keys, device_sig_ok, load_device_key
from awm.operations.store import Store

log = logging.getLogger("awm.approver")

REQUEST_TTL_S = 24 * 3600
ENROL_LINK_TTL_S = 15 * 60
ORDER_TTL_S = 10 * 60
MESSAGE_MAX_AGE_S = 120
IN_FLIGHT_EXPIRY_S = 60
READ_MAX_AGE_S = 60
LOGIN_TTL_S = 5 * 60
LOGIN_GRANT_S = 60
WINDOW_MAX_MIN = 12 * 60
MINUTES_MIN, MINUTES_MAX = 15, 1440

REQUEST_KINDS = ("admin_ssh", "trust_raise", "invite", "browser_signin", "vault_hold",
                 "device_enrol", "plugin_commits")
NODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
REQUEST_ID_RE = re.compile(r"^q[A-Za-z0-9_-]{12}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
PLUGIN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SSH_PUB_RE = re.compile(r"^(ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa) [A-Za-z0-9+/=]+( .*)?$")


class ApproverError(Exception):
    """A refusal with a stable ``code`` the panel can act on."""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code, self.extra = code, extra

    def as_dict(self) -> dict[str, Any]:
        return {"ok": False, "code": self.code, "error": str(self), **self.extra}


class Tools(Protocol):
    async def twofa(self, verb: str, args: dict[str, Any]) -> Any: ...


@dataclass
class Config:
    origin: str
    allowed_orders: dict[str, list[str]] = field(default_factory=dict)
    rate_max: int = 30
    spawn_project: str = "awm"
    spawn_scope: str = "intake"

    @property
    def rp_id(self) -> str:
        host = urlparse(self.origin).hostname
        assert host, "origin needs a host"
        return host


SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (device_id TEXT PRIMARY KEY, name TEXT, device_pub TEXT NOT NULL,
    created REAL, revoked_at REAL, locked_nonce TEXT, seq INTEGER NOT NULL, head TEXT NOT NULL,
    last_ack TEXT, last_result TEXT, last_ip TEXT, enrolled_by TEXT);
CREATE TABLE IF NOT EXISTS credentials (cred_id TEXT PRIMARY KEY, device_id TEXT NOT NULL,
    public_key TEXT NOT NULL, sign_count INTEGER NOT NULL, created REAL, revoked_at REAL);
CREATE TABLE IF NOT EXISTS enrol_links (token_sha TEXT PRIMARY KEY, expires REAL NOT NULL,
    used_at REAL, request_id TEXT);
CREATE TABLE IF NOT EXISTS reg_challenges (reg_id TEXT PRIMARY KEY, challenge TEXT NOT NULL,
    token_sha TEXT NOT NULL, user_id TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS requests (request_id TEXT PRIMARY KEY, task_id TEXT, kind TEXT NOT NULL,
    node TEXT, reason TEXT, minutes INTEGER, payload TEXT, requester_session TEXT,
    created REAL NOT NULL, expires REAL NOT NULL, status TEXT NOT NULL, decided_at REAL,
    device TEXT, grant_env TEXT, cert TEXT, poll_secret_sha TEXT, enrol_token TEXT);
CREATE TABLE IF NOT EXISTS windows (window_id TEXT PRIMARY KEY, device_id TEXT NOT NULL,
    kinds TEXT NOT NULL, cards TEXT NOT NULL, opened REAL NOT NULL, not_after REAL NOT NULL,
    ip TEXT, closed_at REAL, closed_reason TEXT);
CREATE TABLE IF NOT EXISTS action_log (id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT,
    ts REAL, action TEXT);
CREATE TABLE IF NOT EXISTS orders (order_id TEXT PRIMARY KEY, kind TEXT NOT NULL, node TEXT NOT NULL,
    payload TEXT, envelope TEXT NOT NULL, created REAL NOT NULL, not_after REAL NOT NULL,
    pulled_at REAL, done_at REAL, ok INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS logins (nonce TEXT PRIMARY KEY, site TEXT, node TEXT, return_url TEXT,
    created REAL NOT NULL, used_at REAL);
"""

# Actions a device may sign. Each names the card it touches, for window scopes.
CARD_ACTIONS = ("note_new", "spawn", "new_from", "close", "reject")
ACTIONS = CARD_ACTIONS + ("approve", "deny", "order", "twofa_burst", "twofa_approve_all",
                          "twofa_approve", "twofa_deny", "device_revoke")
CARDLESS = ("note_new", "order", "twofa_burst", "twofa_approve_all", "twofa_approve",
            "twofa_deny", "device_revoke")
BURST_COUNTS = (1, 2, 4)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _token(n: int = 24) -> str:
    return secrets.token_urlsafe(n)


@dataclass
class Approver:
    path: Path
    keys: Keys
    config: Config
    store: Store
    tools: Tools
    clock: Callable[[], float] = time.time
    _db: sqlite3.Connection = field(init=False)
    _lock: threading.RLock = field(init=False, default_factory=threading.RLock)
    _device_locks: dict[str, asyncio.Lock] = field(init=False, default_factory=dict)
    _changed: dict[str, asyncio.Event] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    # -- small helpers -------------------------------------------------------------

    def _q(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _x(self, sql: str, *args: Any) -> int:
        with self._lock:
            return self._db.execute(sql, args).rowcount

    def _one(self, sql: str, *args: Any) -> sqlite3.Row | None:
        rows = self._q(sql, *args)
        return rows[0] if rows else None

    def _signal(self, key: str) -> None:
        ev = self._changed.pop(key, None)
        if ev is not None:
            ev.set()

    async def _wait(self, key: str, timeout: float) -> None:
        ev = self._changed.setdefault(key, asyncio.Event())
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    def _device(self, device_id: str) -> sqlite3.Row:
        row = self._one("SELECT * FROM devices WHERE device_id=?", device_id)
        if row is None or row["revoked_at"] is not None:
            raise ApproverError("unknown_device", "no such device, or it was revoked")
        return row

    def _active_credentials(self) -> list[sqlite3.Row]:
        return self._q("SELECT c.* FROM credentials c JOIN devices d USING(device_id) "
                       "WHERE c.revoked_at IS NULL AND d.revoked_at IS NULL")

    def _check_passkey(self, assertion: dict | None, challenge: bytes) -> str:
        """The credential id behind a valid assertion over this challenge."""
        if not isinstance(assertion, dict):
            raise ApproverError("need_passkey", "this needs a passkey assertion")
        cred = self._one("SELECT c.* FROM credentials c JOIN devices d USING(device_id) "
                         "WHERE c.cred_id=? AND c.revoked_at IS NULL AND d.revoked_at IS NULL",
                         assertion.get("id", ""))
        if cred is None:
            raise ApproverError("unknown_passkey", "that passkey is not enrolled")
        try:
            count = passkeys.verify_assertion(assertion, challenge, self.config.origin,
                                              self.config.rp_id, cred["public_key"],
                                              cred["sign_count"])
        except passkeys.PasskeyError as exc:
            raise ApproverError("bad_passkey", str(exc)) from None
        self._x("UPDATE credentials SET sign_count=? WHERE cred_id=?", count, cred["cred_id"])
        return cred["cred_id"]

    def passkey_options(self, challenge: bytes) -> dict[str, Any]:
        return passkeys.assertion_options(self.config.rp_id, challenge,
                                          [c["cred_id"] for c in self._active_credentials()])

    # -- enrolment (R64 item 1, R85) -----------------------------------------------

    def enrol_link(self, minutes: int = 15, request_id: str | None = None) -> dict[str, Any]:
        """A one-time enrolment link. Only the local control socket and an approved request call this."""
        token = _token()
        expires = self.clock() + min(minutes * 60, ENROL_LINK_TTL_S)
        self._x("INSERT INTO enrol_links VALUES (?,?,NULL,?)", _sha(token), expires, request_id)
        return {"url": f"{self.config.origin}/enrol#{token}", "token": token, "expires": expires}

    def _link(self, token: str) -> sqlite3.Row:
        row = self._one("SELECT * FROM enrol_links WHERE token_sha=?", _sha(token or ""))
        if row is None or row["used_at"] is not None or row["expires"] <= self.clock():
            raise ApproverError("bad_link", "the enrolment link is used, expired or unknown")
        return row

    def enrol_begin(self, token: str, name: str) -> dict[str, Any]:
        self._link(token)
        reg_id, challenge, user_id = _token(12), secrets.token_bytes(32), secrets.token_bytes(16)
        self._x("INSERT INTO reg_challenges VALUES (?,?,?,?,?)", reg_id, challenge.hex(),
                _sha(token), b64u(user_id), self.clock())
        opts = passkeys.registration_options(self.config.rp_id, user_id,
                                             f"tony ({name or 'device'})", challenge)
        return {"reg_id": reg_id, "options": opts}

    def enrol_finish(self, token: str, reg_id: str, credential: dict, device_pub: str,
                     name: str, ip: str | None = None) -> dict[str, Any]:
        reg = self._one("SELECT * FROM reg_challenges WHERE reg_id=?", reg_id)
        if reg is None or reg["token_sha"] != _sha(token or "") \
                or reg["created"] < self.clock() - ENROL_LINK_TTL_S:
            raise ApproverError("bad_link", "the enrolment session is unknown or stale")
        link = self._link(token)
        try:
            load_device_key(device_pub)
        except ValueError as exc:
            raise ApproverError("bad_device_key", str(exc)) from None
        try:
            cred_id, pub, count = passkeys.verify_registration(
                credential, bytes.fromhex(reg["challenge"]), self.config.origin,
                self.config.rp_id)
        except passkeys.PasskeyError as exc:
            raise ApproverError("bad_passkey", str(exc)) from None
        with self._lock:
            used = self._db.execute("UPDATE enrol_links SET used_at=? WHERE token_sha=? AND "
                                    "used_at IS NULL", (self.clock(), link["token_sha"])).rowcount
            if used != 1:
                raise ApproverError("bad_link", "the enrolment link is used")
            self._db.execute("DELETE FROM reg_challenges WHERE reg_id=?", (reg_id,))
            device_id = "d" + _token(9)
            head = fresh_head()
            ack = self.keys.sign_ack({"device": device_id, "seq": 0, "head": head})
            self._db.execute(
                "INSERT INTO devices VALUES (?,?,?,?,NULL,NULL,0,?,?,NULL,?,?)",
                (device_id, name or "device", device_pub, self.clock(), head, json.dumps(ack),
                 ip, link["request_id"] or "link"))
            self._db.execute("INSERT INTO credentials VALUES (?,?,?,?,?,NULL)",
                             (cred_id, device_id, pub, count, self.clock()))
        self._close_windows(None, "new device")
        return {"device": device_id, "ack": ack, "ack_pub": self.keys.ack_pub_spki_b64u()}

    async def enrol_ask(self, name: str) -> dict[str, Any]:
        """A new device asks to enrol. A trusted device approves the card (R64 item 1)."""
        secret = _token()
        out = await self.approval_request("device_enrol", node=None,
                                          reason=f"enrol device {name or 'unnamed'}",
                                          payload={"name": name or ""})
        self._x("UPDATE requests SET poll_secret_sha=? WHERE request_id=?", _sha(secret),
                out["request_id"])
        return {**out, "poll_secret": secret}

    def enrol_ask_status(self, request_id: str, poll_secret: str) -> dict[str, Any]:
        row = self._one("SELECT * FROM requests WHERE request_id=? AND kind='device_enrol'",
                        request_id)
        if row is None or not row["poll_secret_sha"] \
                or not hmac.compare_digest(row["poll_secret_sha"], _sha(poll_secret or "")):
            raise ApproverError("unknown_request", "no such enrolment request")
        token = None
        if row["status"] == "approved" and row["enrol_token"]:
            token = row["enrol_token"]
            self._x("UPDATE requests SET enrol_token=NULL WHERE request_id=?", request_id)
        status = "expired" if row["status"] == "pending" and row["expires"] <= self.clock() \
            else row["status"]
        return {"status": status, "token": token}

    # -- the chain (R65 item 4) ------------------------------------------------------

    def _lock_for(self, device_id: str) -> asyncio.Lock:
        return self._device_locks.setdefault(device_id, asyncio.Lock())

    def check_read(self, device_id: str, ts: int, path: str, sig: str) -> sqlite3.Row:
        """A read from the panel: the device key signs ``{device, path, ts}``."""
        dev = self._device(device_id)
        if abs(self.clock() - ts) > READ_MAX_AGE_S:
            raise ApproverError("stale", "the read signature is stale")
        if not device_sig_ok(load_device_key(dev["device_pub"]),
                             canon({"device": device_id, "path": path, "ts": ts}), unb64u(sig)):
            raise ApproverError("bad_signature", "the read signature does not verify")
        return dev

    async def act(self, message: dict, sig: str, assertion: dict | None = None,
                  ip: str | None = None) -> dict[str, Any]:
        """Take one signed message. Returns ``{ok, ack, result}`` or raises ApproverError."""
        if not isinstance(message, dict) or set(message) != MESSAGE_FIELDS:
            raise ApproverError("bad_message", f"a message has exactly {sorted(MESSAGE_FIELDS)}")
        device_id = str(message["device"])
        async with self._lock_for(device_id):
            return await self._act(message, unb64u(sig or ""), assertion, ip)

    async def _act(self, message: dict, sig: bytes, assertion: dict | None,
                   ip: str | None) -> dict[str, Any]:
        dev = self._device(message["device"])
        if dev["locked_nonce"]:
            raise ApproverError("locked", "this device is locked until a passkey unlocks it",
                                nonce=dev["locked_nonce"])
        if message["prev"] != dev["head"] or message["seq"] != dev["seq"] + 1:
            raise ApproverError("stale_head", "the message does not extend alcor's head")
        if not isinstance(message["ts"], int) or abs(self.clock() - message["ts"]) > MESSAGE_MAX_AGE_S:
            raise ApproverError("stale", "the message time is off by more than two minutes")
        if not device_sig_ok(load_device_key(dev["device_pub"]), canon(message), sig):
            raise ApproverError("bad_signature", "the device signature does not verify")
        action, args = message["action"], message["args"]
        if not isinstance(args, dict):
            raise ApproverError("bad_message", "args is an object")
        if action == "authorize":
            self._check_authorize(args)
            self._check_passkey(assertion, passkey_challenge(message["prev"], args))
        else:
            self._check_action(action, args)
            self._check_window(dev, action, args, ip)

        head = next_head(message["prev"], message, sig)
        ack = self.keys.sign_ack({"device": dev["device_id"], "seq": message["seq"], "head": head})
        self._x("UPDATE devices SET head=?, seq=?, last_ack=?, last_result=NULL, last_ip=? "
                "WHERE device_id=?", head, message["seq"], json.dumps(ack), ip, dev["device_id"])
        self._x("INSERT INTO action_log(device_id, ts, action) VALUES (?,?,?)",
                dev["device_id"], self.clock(), action)

        if action == "authorize":
            result = await self._authorize(dev, args, ip)
        else:
            result = await self._dispatch_safe(dev, action, args)
        self._x("UPDATE devices SET last_result=? WHERE device_id=?", json.dumps(result),
                dev["device_id"])
        return {"ok": True, "ack": ack, "result": result}

    async def resume(self, device_id: str, acked_head: str, ts: int, sig: str,
                     in_flight: dict | None = None, ip: str | None = None) -> dict[str, Any]:
        """Reconcile after a drop. The device signs ``{device, acked_head, ts}``.

        ``in_flight`` is the press the device signed and sent with no ack back (R65 item 4).
        """
        async with self._lock_for(device_id):
            dev = self._device(device_id)
            if abs(self.clock() - ts) > READ_MAX_AGE_S or not device_sig_ok(
                    load_device_key(dev["device_pub"]),
                    canon({"device": device_id, "acked_head": acked_head, "ts": ts}), unb64u(sig)):
                raise ApproverError("bad_signature", "the resume signature does not verify")
            if dev["locked_nonce"]:
                return {"state": "locked", "nonce": dev["locked_nonce"]}
            if in_flight is not None:
                msg, psig = in_flight.get("message") or {}, unb64u(in_flight.get("sig") or "")
                try:
                    in_flight_head = next_head(str(msg.get("prev", "")), msg, psig)
                except ValueError:
                    in_flight_head = None
                if in_flight_head == dev["head"] and msg.get("device") == device_id:
                    return {"state": "acked", "ack": json.loads(dev["last_ack"]),
                            "result": json.loads(dev["last_result"] or "null")}
            if acked_head == dev["head"]:
                if in_flight is not None and isinstance(msg.get("ts"), int) \
                        and self.clock() - msg["ts"] < IN_FLIGHT_EXPIRY_S:
                    try:
                        out = await self._act(msg, psig, in_flight.get("assertion"), ip)
                    except ApproverError as exc:
                        return {"state": "refused", **exc.as_dict(),
                                "ack": json.loads(dev["last_ack"])}
                    return {"state": "taken", **out}
                return {"state": "in_sync", "ack": json.loads(dev["last_ack"])}
            nonce = secrets.token_hex(16)
            self._x("UPDATE devices SET locked_nonce=? WHERE device_id=?", nonce, device_id)
            self._close_windows(device_id, "chain mismatch")
            log.warning("device %s locked: its heads do not match alcor's", device_id)
            return {"state": "locked", "nonce": nonce}

    def unlock_options(self, device_id: str) -> dict[str, Any]:
        dev = self._device(device_id)
        if not dev["locked_nonce"]:
            raise ApproverError("not_locked", "the device is not locked")
        return self.passkey_options(unlock_challenge(device_id, dev["locked_nonce"]))

    async def unlock(self, device_id: str, assertion: dict, sig: str) -> dict[str, Any]:
        """A passkey unlocks a locked device and starts a fresh chain from a head alcor issues."""
        async with self._lock_for(device_id):
            dev = self._device(device_id)
            nonce = dev["locked_nonce"]
            if not nonce:
                raise ApproverError("not_locked", "the device is not locked")
            if not device_sig_ok(load_device_key(dev["device_pub"]),
                                 canon({"device": device_id, "nonce": nonce}), unb64u(sig)):
                raise ApproverError("bad_signature", "the unlock signature does not verify")
            self._check_passkey(assertion, unlock_challenge(device_id, nonce))
            head = fresh_head()
            ack = self.keys.sign_ack({"device": device_id, "seq": 0, "head": head})
            self._x("UPDATE devices SET locked_nonce=NULL, head=?, seq=0, last_ack=?, "
                    "last_result=NULL WHERE device_id=?", head, json.dumps(ack), device_id)
            return {"ok": True, "ack": ack}

    # -- authority: passkey requests, windows and risk (R64, decided 2026-11-03) --------

    def _check_action(self, action: str, args: dict) -> None:
        if action not in ACTIONS:
            raise ApproverError("unknown_action", f"unknown action {action!r}")
        if not isinstance(args, dict):
            raise ApproverError("bad_message", "action args are an object")

    def _check_authorize(self, args: dict) -> None:
        batch, window = args.get("batch"), args.get("window")
        if batch is None and window is None:
            raise ApproverError("bad_request", "a request carries a batch, a window or both")
        if set(args) - {"batch", "window", "nonce"}:
            raise ApproverError("bad_request", "a request holds batch, window and nonce only")
        for item in batch or []:
            if not isinstance(item, dict) or set(item) != {"action", "args"}:
                raise ApproverError("bad_request", "a batch item is {action, args}")
            self._check_action(item["action"], item["args"])
        if window is not None:
            kinds, cards, minutes = window.get("kinds"), window.get("cards"), window.get("minutes")
            if not kinds or any(k not in ACTIONS for k in kinds):
                raise ApproverError("bad_request", "a window names known action kinds")
            if cards != "any" and not (isinstance(cards, list) and cards):
                raise ApproverError("bad_request", "a window names cards or \"any\"")
            if not isinstance(minutes, int) or not 1 <= minutes <= WINDOW_MAX_MIN:
                raise ApproverError("bad_request", f"a window lasts 1 to {WINDOW_MAX_MIN} minutes")

    def _card_of(self, action: str, args: dict) -> str | None:
        if action in ("approve", "deny"):
            row = self._one("SELECT task_id FROM requests WHERE request_id=?",
                            args.get("request_id", ""))
            return row["task_id"] if row else None
        return args.get("task_id")

    def _check_window(self, dev: sqlite3.Row, action: str, args: dict, ip: str | None) -> None:
        now = self.clock()
        windows = self._q("SELECT * FROM windows WHERE device_id=? AND closed_at IS NULL "
                          "AND not_after>?", dev["device_id"], now)
        if not windows:
            raise ApproverError("need_passkey", "no window is open: send this as a request")
        recent = self._one("SELECT COUNT(*) n FROM action_log WHERE device_id=? AND ts>?",
                           dev["device_id"], now - 60)["n"]
        if recent >= self.config.rate_max:
            self._close_windows(dev["device_id"], "rate")
            raise ApproverError("need_passkey", "risk: unusual rate, the windows closed")
        for w in windows:
            if w["ip"] is not None and ip != w["ip"]:
                self._close_windows(dev["device_id"], "network")
                raise ApproverError("need_passkey", "risk: new network, the windows closed")
        card = self._card_of(action, args)
        for w in windows:
            cards = json.loads(w["cards"])
            if action in json.loads(w["kinds"]) and (
                    cards == "any" or action in CARDLESS or card in cards):
                return
        raise ApproverError("need_passkey", "the action is outside every open window")

    def _close_windows(self, device_id: str | None, reason: str) -> None:
        if device_id is None:
            self._x("UPDATE windows SET closed_at=?, closed_reason=? WHERE closed_at IS NULL",
                    self.clock(), reason)
        else:
            self._x("UPDATE windows SET closed_at=?, closed_reason=? WHERE closed_at IS NULL "
                    "AND device_id=?", self.clock(), reason, device_id)

    def windows(self, device_id: str) -> list[dict[str, Any]]:
        rows = self._q("SELECT * FROM windows WHERE device_id=? ORDER BY opened DESC LIMIT 20",
                       device_id)
        return [{"window_id": r["window_id"], "kinds": json.loads(r["kinds"]),
                 "cards": json.loads(r["cards"]), "opened": r["opened"],
                 "not_after": r["not_after"], "network_check": r["ip"] is not None,
                 "closed_at": r["closed_at"], "closed_reason": r["closed_reason"]} for r in rows]

    async def _authorize(self, dev: sqlite3.Row, args: dict, ip: str | None) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if args.get("window"):
            w = args["window"]
            wid = "w" + _token(9)
            self._x("INSERT INTO windows VALUES (?,?,?,?,?,?,?,NULL,NULL)", wid,
                    dev["device_id"], json.dumps(sorted(w["kinds"])), json.dumps(w["cards"]),
                    self.clock(), self.clock() + w["minutes"] * 60, ip)
            out["window"] = {"window_id": wid, "not_after": self.clock() + w["minutes"] * 60,
                             "network_check": ip is not None}
        if args.get("batch"):
            out["batch"] = [await self._dispatch_safe(dev, i["action"], i["args"])
                            for i in args["batch"]]
        return out

    # -- dispatch ----------------------------------------------------------------------

    async def _dispatch_safe(self, dev: sqlite3.Row, action: str, args: dict) -> dict[str, Any]:
        try:
            return {"ok": True, "action": action,
                    "result": await self._dispatch(dev, action, args)}
        except ApproverError as exc:
            return {**exc.as_dict(), "action": action}
        except Exception as exc:  # a downstream refusal is the action's result, not the chain's
            log.warning("action %s failed: %s", action, exc)
            return {"ok": False, "code": "failed", "error": str(exc)[:300], "action": action}

    async def _card_action(self, action: str, task_id: str | None, args: dict) -> Any:
        # A thread: spawn reads the note body again through the trilium tool.
        return await asyncio.to_thread(self.store.card_action, action, task_id, args)

    async def _dispatch(self, dev: sqlite3.Row, action: str, args: dict) -> Any:
        if action == "note_new":
            return await self._card_action("note_new", None,
                                           {"title": str(args.get("title", ""))[:200],
                                            "peer": args.get("peer")})
        if action == "spawn":
            peer = self._order_node("spawn", args.get("peer"))
            res = await self._card_action("spawn", args.get("task_id"),
                                          {"peer": peer, "body_sha256": args.get("body_sha256")})
            order = self.issue_order("spawn", peer, {
                "task_id": res["task_id"], "title": res.get("title", ""),
                "body_ref": res.get("body_ref"), "body_sha256": args.get("body_sha256"),
                "body": res.get("body"),
                "project": args.get("project") or self.config.spawn_project,
                "scope": args.get("scope") or self.config.spawn_scope})
            return {**res, "order_id": order["order"]["order_id"]}
        if action == "new_from":
            res = await self._card_action("new_from", args.get("task_id"),
                                          {"peer": args.get("peer"), "title": args.get("title")})
            out = dict(res)
            if res.get("peer"):
                peer = self._order_node("new_from", res["peer"])
                order = self.issue_order("new_from", peer, {
                    "task_id": res["task_id"], "title": args.get("title") or "",
                    "from": res.get("from"), "project": self.config.spawn_project,
                    "scope": self.config.spawn_scope})
                out["order_id"] = order["order"]["order_id"]
            return out
        if action in ("close", "reject"):
            return await self._card_action(action, args.get("task_id"), {})
        if action in ("approve", "deny"):
            return await self.decide(args.get("request_id", ""), action == "approve",
                                     dev["device_id"])
        if action == "order":
            node = self._order_node(args.get("kind", ""), args.get("node"))
            env = self.issue_order(args["kind"], node, args.get("payload") or {})
            return {"order_id": env["order"]["order_id"], "node": node}
        if action == "twofa_burst":
            count = int(args.get("count", 1))
            if count not in BURST_COUNTS:
                raise ApproverError("bad_request", f"a burst approves {BURST_COUNTS} pushes")
            return await self.tools.twofa("burst", {"device": args["device"], "count": count})
        if action == "twofa_approve_all":
            return await self.tools.twofa("approve_all", {"device": args["device"]})
        if action in ("twofa_approve", "twofa_deny"):
            return await self.tools.twofa(action.removeprefix("twofa_"),
                                          {"urgid": args["urgid"], "device": args["device"]})
        if action == "device_revoke":
            return self.device_revoke(args.get("device_id", ""))
        raise ApproverError("unknown_action", f"unknown action {action!r}")

    # -- approval requests (fleet.md R97) ---------------------------------------------------

    def _check_payload(self, kind: str, node: str | None, payload: dict) -> None:
        need = {"admin_ssh": ("pubkey", "nodes"), "trust_raise": ("session_id", "from", "to"),
                "invite": ("perm", "reason", "holder_pubkey"), "browser_signin": ("seat", "site"),
                "vault_hold": (), "device_enrol": ("name",),
                "plugin_commits": ("change", "plugins")}[kind]
        missing = [k for k in need if k not in payload]
        if missing:
            raise ApproverError("bad_request", f"{kind} payload lacks {missing}")
        if kind == "admin_ssh":
            nodes = payload["nodes"]
            if not isinstance(nodes, list) or not nodes or not all(
                    isinstance(n, str) and NODE_RE.match(n) for n in nodes):
                raise ApproverError("bad_request", "nodes is a list of node names")
            if not SSH_PUB_RE.match(str(payload["pubkey"]).strip()):
                raise ApproverError("bad_request", "pubkey is one OpenSSH public key line")
        if kind == "plugin_commits":
            self._check_commits(payload["plugins"])
        if node is not None and not NODE_RE.match(node):
            raise ApproverError("bad_request", "node is a node name")

    @staticmethod
    def _check_commits(plugins: Any) -> None:
        """Each entry pins one plugin to one commit: an install, or a move ``from`` another."""
        if not isinstance(plugins, list) or not plugins:
            raise ApproverError("bad_request", "plugins is a list of {plugin, commit}")
        for p in plugins:
            if not isinstance(p, dict) or set(p) - {"plugin", "commit", "source", "from"} \
                    or not PLUGIN_RE.match(str(p.get("plugin", ""))) \
                    or not COMMIT_RE.match(str(p.get("commit", ""))) \
                    or ("from" in p and not COMMIT_RE.match(str(p["from"]))):
                raise ApproverError("bad_request", "a plugin entry is {plugin, commit, source?, "
                                    "from?} with full 40-hex commits")
        if len({p["plugin"] for p in plugins}) != len(plugins):
            raise ApproverError("bad_request", "a change pins each plugin once")

    async def approval_request(self, kind: str, node: str | None = None, reason: str = "",
                               minutes: int = 15, payload: dict | None = None,
                               requester_session: str | None = None,
                               request_id: str | None = None) -> dict[str, Any]:
        """Open a request and its Needs-you card. A relay may pass the id it minted while alcor was away."""
        if kind not in REQUEST_KINDS:
            raise ApproverError("bad_request", f"kind is one of {REQUEST_KINDS}")
        minutes = int(minutes)
        if not MINUTES_MIN <= minutes <= MINUTES_MAX:
            raise ApproverError("bad_request", f"minutes is {MINUTES_MIN} to {MINUTES_MAX}")
        payload = dict(payload or {})
        self._check_payload(kind, node, payload)
        if request_id is None:
            request_id = "q" + _token(9)
        elif not REQUEST_ID_RE.match(request_id):
            raise ApproverError("bad_request", "request_id is q plus 12 URL-safe characters")
        if self._one("SELECT 1 FROM requests WHERE request_id=?", request_id):
            raise ApproverError("bad_request", f"request id {request_id} is taken")
        now = self.clock()
        where = node or ",".join(payload.get("nodes", [])) or "alcor"
        title = f"{kind} on {where}: {reason}".strip().rstrip(":")[:160]
        meta = {"kind": kind, "minutes": minutes, "reason": reason[:500],
                "requester_session": requester_session, "payload": payload}
        card = await asyncio.to_thread(self.store.approval_card, request_id, title, node=node,
                                       meta=meta)
        self._x("INSERT INTO requests(request_id, task_id, kind, node, reason, minutes, payload, "
                "requester_session, created, expires, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                request_id, card["task_id"], kind, node, reason, minutes, json.dumps(payload),
                requester_session, now, now + REQUEST_TTL_S, "pending")
        return {"request_id": request_id, "task_id": card["task_id"],
                "expires": now + REQUEST_TTL_S}

    def _request(self, request_id: str) -> sqlite3.Row:
        row = self._one("SELECT * FROM requests WHERE request_id=?", request_id)
        if row is None:
            raise ApproverError("unknown_request", "no such request")
        return row

    def approval_get(self, request_id: str) -> dict[str, Any]:
        """A request as its requester sees it. A pending request past its expiry reads expired."""
        r = self._request(request_id)
        out = {k: r[k] for k in ("request_id", "task_id", "kind", "node", "reason", "minutes",
                                 "requester_session", "created", "expires", "status",
                                 "decided_at", "cert")}
        if out["status"] == "pending" and r["expires"] <= self.clock():
            out["status"] = "expired"
        out["payload"] = json.loads(r["payload"] or "{}")
        out["grant"] = json.loads(r["grant_env"]) if r["grant_env"] else None
        out["ssh_cert"] = r["cert"]
        return out

    async def approval_wait(self, request_id: str, wait_s: float = 55) -> dict[str, Any]:
        deadline = self.clock() + min(max(wait_s, 0), 55)
        while True:
            out = self.approval_get(request_id)
            left = deadline - self.clock()
            if out["status"] != "pending" or left <= 0:
                return out
            await self._wait(f"req:{request_id}", left)

    async def approval_cancel(self, request_id: str, session_id: str | None) -> dict[str, Any]:
        """Withdraw a request. Only the session that made it may (fleet.md R97 item 1)."""
        r = self._request(request_id)
        if not session_id or session_id != r["requester_session"]:
            raise ApproverError("not_requester", "only the session that made a request cancels it")
        return await self._finish(request_id, "cancelled")

    async def _finish(self, request_id: str, outcome: str, device: str | None = None,
                      **cols: Any) -> dict[str, Any]:
        r = self._request(request_id)
        sets = ", ".join(f"{k}=?" for k in cols)
        n = self._x(f"UPDATE requests SET status=?, decided_at=?, device=?{', ' + sets if sets else ''} "
                    "WHERE request_id=? AND status='pending'",
                    outcome, self.clock(), device, *cols.values(), request_id)
        if n != 1:
            raise ApproverError("decided", f"the request is already {r['status']}")
        try:
            await asyncio.to_thread(self.store.approval_resolve, r["task_id"], outcome)
        except Exception as exc:  # the decision stands; the card stays behind it
            log.warning("could not move card %s to %s: %s", r["task_id"], outcome, exc)
        self._signal(f"req:{request_id}")
        return self.approval_get(request_id)

    async def decide(self, request_id: str, approve: bool, device: str) -> dict[str, Any]:
        await self.sweep()
        r = self._request(request_id)
        if r["status"] != "pending":
            raise ApproverError("decided", f"the request is already {r['status']}")
        if not approve:
            return await self._finish(request_id, "denied", device)
        payload = json.loads(r["payload"] or "{}")
        now = int(self.clock())
        body: dict[str, Any] = {
            "grant_id": "g" + _token(9), "request_id": request_id, "kind": r["kind"],
            "node": r["node"], "subject": r["requester_session"] or "tony",
            "payload_sha256": hashlib.sha256(canon(payload)).hexdigest(),
            "not_before": now, "not_after": now + r["minutes"] * 60,
            "approved_by_device": device}
        cols: dict[str, Any] = {}
        if r["kind"] == "admin_ssh":
            body["nodes"] = sorted(payload["nodes"])
            cols["cert"] = self.keys.ssh_cert(payload["pubkey"],
                                              [f"admin@{n}" for n in body["nodes"]],
                                              body["not_before"], body["not_after"],
                                              body["grant_id"])
        if r["kind"] == "plugin_commits":
            body["commits"] = sorted(f"{p['plugin']}@{p['commit']}" for p in payload["plugins"])
        if r["kind"] == "device_enrol":
            cols["enrol_token"] = self.enrol_link(request_id=request_id)["token"]
        cols["grant_env"] = json.dumps(self.keys.envelope("grant", body))
        return await self._finish(request_id, "approved", device, **cols)

    async def _expire(self, request_id: str) -> None:
        try:
            await self._finish(request_id, "expired")
        except ApproverError:
            pass

    async def sweep(self) -> None:
        for rid in [r["request_id"] for r in self._q(
                "SELECT request_id FROM requests WHERE status='pending' AND expires<=?",
                self.clock())]:
            await self._expire(rid)

    # -- orders (R97: alcor never acts on another node) ----------------------------------------

    def _order_node(self, kind: str, node: str | None) -> str:
        allowed = self.config.allowed_orders.get(kind, [])
        if not node or not NODE_RE.match(node) or ("*" not in allowed and node not in allowed):
            raise ApproverError("not_allowed", f"no {kind} orders to {node!r}")
        return node

    def issue_order(self, kind: str, node: str, payload: dict) -> dict[str, Any]:
        body = {"order_id": "o" + _token(9), "kind": kind, "node": node, "payload": payload,
                "not_after": int(self.clock()) + ORDER_TTL_S}
        env = self.keys.envelope("order", body)
        self._x("INSERT INTO orders VALUES (?,?,?,?,?,?,?,NULL,NULL,NULL,NULL)",
                body["order_id"], kind, node, json.dumps(payload), json.dumps(env), self.clock(),
                body["not_after"])
        self._signal(f"orders:{node}")
        return env

    async def order_pull(self, node: str, kinds: list[str] | None = None,
                         wait_s: float = 0) -> list[dict[str, Any]]:
        deadline = self.clock() + min(max(wait_s, 0), 55)
        while True:
            rows = self._q("SELECT * FROM orders WHERE node=? AND done_at IS NULL AND not_after>? "
                           "ORDER BY created", node, self.clock())
            rows = [r for r in rows if not kinds or r["kind"] in kinds]
            left = deadline - self.clock()
            if rows or left <= 0:
                for r in rows:
                    self._x("UPDATE orders SET pulled_at=COALESCE(pulled_at, ?) WHERE order_id=?",
                            self.clock(), r["order_id"])
                return [json.loads(r["envelope"]) for r in rows]
            await self._wait(f"orders:{node}", left)

    def order_done(self, order_id: str, ok: bool, note: str = "") -> dict[str, Any]:
        n = self._x("UPDATE orders SET done_at=?, ok=?, note=? WHERE order_id=? AND done_at IS NULL",
                    self.clock(), int(bool(ok)), (note or "")[:1000], order_id)
        return {"order_id": order_id, "recorded": n == 1}

    def order_get(self, order_id: str) -> dict[str, Any] | None:
        r = self._one("SELECT * FROM orders WHERE order_id=?", order_id)
        if r is None:
            return None
        return {k: r[k] for k in ("order_id", "kind", "node", "created", "not_after",
                                  "pulled_at", "done_at", "ok", "note")}

    # -- devices --------------------------------------------------------------------------------

    def devices(self) -> list[dict[str, Any]]:
        rows = self._q("SELECT * FROM devices ORDER BY created")
        return [{"device_id": r["device_id"], "name": r["name"], "created": r["created"],
                 "revoked_at": r["revoked_at"], "locked": bool(r["locked_nonce"]),
                 "seq": r["seq"], "enrolled_by": r["enrolled_by"]} for r in rows]

    def device_revoke(self, device_id: str) -> dict[str, Any]:
        n = self._x("UPDATE devices SET revoked_at=? WHERE device_id=? AND revoked_at IS NULL",
                    self.clock(), device_id)
        self._x("UPDATE credentials SET revoked_at=? WHERE device_id=? AND revoked_at IS NULL",
                self.clock(), device_id)
        self._close_windows(device_id, "revoked")
        return {"device_id": device_id, "revoked": n == 1}

    # -- portal login (fleet.md R84) --------------------------------------------------------------

    def login_begin(self, site: str, node: str, return_url: str) -> dict[str, Any]:
        if not NODE_RE.match(node or ""):
            raise ApproverError("bad_request", "node is a node name")
        nonce = secrets.token_hex(16)
        self._x("INSERT INTO logins VALUES (?,?,?,?,?,NULL)", nonce, site, node, return_url,
                self.clock())
        return {"nonce": nonce, "options": self.passkey_options(self._login_challenge(
            nonce, site, node, return_url))}

    @staticmethod
    def _login_challenge(nonce: str, site: str, node: str, return_url: str) -> bytes:
        return hashlib.sha256(b"login\0" + canon({"nonce": nonce, "site": site, "node": node,
                                                  "return": return_url})).digest()

    def login_finish(self, nonce: str, assertion: dict) -> dict[str, Any]:
        row = self._one("SELECT * FROM logins WHERE nonce=?", nonce)
        if row is None or row["used_at"] is not None or row["created"] < self.clock() - LOGIN_TTL_S:
            raise ApproverError("bad_login", "the login is unknown, used or stale")
        cred = self._check_passkey(assertion, self._login_challenge(
            nonce, row["site"], row["node"], row["return_url"]))
        if self._x("UPDATE logins SET used_at=? WHERE nonce=? AND used_at IS NULL",
                   self.clock(), nonce) != 1:
            raise ApproverError("bad_login", "the login is used")
        device = self._one("SELECT device_id FROM credentials WHERE cred_id=?", cred)["device_id"]
        now = int(self.clock())
        body = {"grant_id": "g" + _token(9), "request_id": "", "kind": "portal_login",
                "node": row["node"], "subject": "tony",
                "payload_sha256": hashlib.sha256(canon({"site": row["site"],
                                                        "return": row["return_url"]})).hexdigest(),
                "not_before": now, "not_after": now + LOGIN_GRANT_S,
                "approved_by_device": device}
        return {"grant": self.keys.envelope("grant", body), "return": row["return_url"]}

    # -- the 2fa strip (R137) ------------------------------------------------------------------

    async def twofa_state(self) -> dict[str, Any]:
        devices = await self.tools.twofa("devices", {})
        pending = await self.tools.twofa("pending", {})
        return {"devices": devices, "pending": pending}

