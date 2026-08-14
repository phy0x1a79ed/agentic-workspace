"""The promised resume must outlive the process that promised it.

The deferred follow-up is what makes a self-directed ``/compact`` safe, and until
these tests it existed only as a thread. The gateway drains and respawns its
services routinely, and the wait can run for minutes — so a restart in that
window took the watcher with it and left the session idle forever, with the
caller already told the resume was on its way. That is what is covered here: the
promise is written down before the watcher starts, replayed on boot, and only
ever replayed at the session that actually earned it.
"""
from __future__ import annotations

import json
import struct
import time

import pytest

pytestmark = [pytest.mark.smoke]

from awm.reflection import daemon_inject, inject, pending, session_target


# ---------------------------------------------------------------------------
# Fakes (the daemon-socket seam, same shape as test_reflection_backends)
# ---------------------------------------------------------------------------

def _frame(kind: int, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + bytes([kind]) + payload


def _ctl(obj: dict) -> bytes:
    return _frame(0x01, json.dumps(obj).encode())


HELLO = _ctl({"t": "hello", "replPid": 4242, "version": "2.1.223"}) + _ctl({"t": "live"})


class FakeSock:
    def __init__(self, inbound: bytes = b""):
        self._inbound = bytearray(inbound)
        self.sent: list[bytes] = []

    def settimeout(self, t):
        pass

    def recv(self, n):
        if not self._inbound:
            raise TimeoutError()
        chunk = bytes(self._inbound[:n])
        del self._inbound[:n]
        return chunk

    def sendall(self, data):
        self.sent.append(data)

    def close(self):
        pass

    def payloads(self) -> list[bytes]:
        reader = daemon_inject._FrameReader()
        out = []
        for blob in self.sent:
            out.extend(reader.feed(blob))
        return [p for k, p in out if k == 0x00]


def _promise(**over) -> pending.Pending:
    """A promise stamped *now*.

    Stamping it matters: :func:`pending.load_all` drops anything past
    ``MAX_AGE_MS``, so a fixture with a toy timestamp makes every "the record is
    gone" assertion pass for the wrong reason.
    """
    base = dict(repl_pid=4242, proc_start="999", session_id="sid-1",
                text="/compact", followup="resume",
                injected_at_ms=int(time.time() * 1000), name="test")
    base.update(over)
    return pending.Pending(**base)


def _target(**over) -> session_target.DaemonTarget:
    base = dict(sock="/tmp/fake.sock", auth="tok", session_id="sid-1",
                repl_pid=4242, name="test", cli_version="2.1.223",
                dec_modes=(2004,))
    base.update(over)
    return session_target.DaemonTarget(**base)


class _Clock:
    def __init__(self, step=1.0):
        self.t, self.step = 0.0, step

    def __call__(self):
        v = self.t
        self.t += self.step
        return v


@pytest.fixture(autouse=True)
def _pending_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(pending, "PENDING_DIR", tmp_path / "pending")
    yield tmp_path / "pending"


# ---------------------------------------------------------------------------
# Recording the promise
# ---------------------------------------------------------------------------

def test_a_deferred_resume_is_written_down_before_the_watcher_starts(monkeypatch):
    # The ordering is the whole point: if the record were written by the watcher
    # thread, a restart could land between the paste and the first write and the
    # promise would exist nowhere at all.
    monkeypatch.setattr(session_target, "_proc_start", lambda pid: "999")
    written = []
    daemon_inject.send("/compact", target=_target(), opener=lambda _p: FakeSock(HELLO),
                       sleep=lambda _s: None,
                       spawn=lambda fn: written.append(pending.load_all()))
    assert len(written) == 1
    [item] = written[0]
    assert item.repl_pid == 4242
    assert item.text == "/compact"
    assert item.followup == "Continue with what you were doing."
    assert item.proc_start == "999"


def test_a_plain_prompt_promises_nothing(monkeypatch):
    monkeypatch.setattr(session_target, "_proc_start", lambda pid: "999")
    daemon_inject.send("just a prompt", target=_target(),
                       opener=lambda _p: FakeSock(HELLO), sleep=lambda _s: None,
                       spawn=lambda fn: None)
    assert pending.load_all() == []


def test_the_promise_is_cleared_once_the_resume_is_delivered(monkeypatch):
    monkeypatch.setattr(session_target, "_proc_start", lambda pid: "999")
    monkeypatch.setattr(session_target, "resolve", lambda pid, **kw: _target())
    pending.record(_promise())
    seq = [("busy", 1100), ("idle", 1500), ("idle", 1600), ("idle", 1700)]
    monkeypatch.setattr(daemon_inject, "_status",
                        lambda _pid: seq.pop(0) if seq else ("idle", 9999))
    sock = FakeSock(HELLO * 40)
    daemon_inject._await_and_followup(
        "/compact", "resume", _target(), opener=lambda _p: sock,
        injected_at_ms=1000, sleep=lambda _s: None, clock=_Clock(),
        now_ms=lambda: 1000)
    assert any(b"resume" in p for p in sock.payloads())
    assert pending.load_all() == [], "a delivered resume must not be replayed"


def test_a_vanished_session_clears_its_promise(monkeypatch):
    monkeypatch.setattr(session_target, "resolve", lambda pid, **kw: _target())
    pending.record(_promise())
    assert len(pending.load_all()) == 1
    monkeypatch.setattr(daemon_inject, "_status", lambda _pid: None)
    daemon_inject._await_and_followup(
        "/compact", "resume", _target(), opener=lambda _p: FakeSock(HELLO),
        injected_at_ms=1000, sleep=lambda _s: None, clock=_Clock(),
        now_ms=lambda: 1000)
    assert pending.load_all() == []


def test_a_stale_promise_is_dropped_rather_than_replayed():
    # Past the watcher's own hard cap the session has long since been left to its
    # own devices; resuming it then would interrupt whatever it moved on to.
    pending.record(_promise(injected_at_ms=0))
    assert pending.load_all(now_ms=pending.MAX_AGE_MS + 1) == []


def test_an_unreadable_record_is_discarded_not_carried_forever(_pending_dir):
    _pending_dir.mkdir(parents=True, exist_ok=True)
    junk = _pending_dir / "4242.json"
    junk.write_text("{not json")
    assert pending.load_all() == []
    assert not junk.exists()


# ---------------------------------------------------------------------------
# Replay on boot
# ---------------------------------------------------------------------------

def test_replay_re_arms_the_watcher_without_re_running_the_command(monkeypatch):
    # This is the bug: reflection was killed mid-wait by a gateway restart and
    # the session sat idle forever. Replay must pick the *wait* back up — and
    # must not paste `/compact` a second time.
    monkeypatch.setattr(session_target, "_proc_start", lambda pid: "999")
    monkeypatch.setattr(session_target, "resolve", lambda pid, **kw: _target())
    pending.record(_promise())
    armed = []
    monkeypatch.setattr(daemon_inject, "resume_watch",
                        lambda item, tgt, **kw: armed.append(item))
    assert inject.replay_pending() == {"pending": 1, "resumed": 1}
    assert [i.text for i in armed] == ["/compact"]
    assert [i.followup for i in armed] == ["resume"]
    # Still owed until the re-armed watcher actually delivers it.
    assert len(pending.load_all()) == 1


def test_replay_carries_the_original_injection_time(monkeypatch):
    # The "has the session reacted yet?" test compares the session's status
    # timestamp against when the command went in. Re-stamping it at boot would
    # make a session that already finished compacting look like it never started.
    monkeypatch.setattr(session_target, "_proc_start", lambda pid: "999")
    monkeypatch.setattr(session_target, "resolve", lambda pid, **kw: _target())
    stamp = int(time.time() * 1000) - 60_000      # injected a minute before the restart
    pending.record(_promise(injected_at_ms=stamp))
    seen = []
    monkeypatch.setattr(daemon_inject, "_await_and_followup",
                        lambda *a, **kw: seen.append(kw["injected_at_ms"]))
    inject.replay_pending()
    assert seen == [stamp]


def test_replay_refuses_a_recycled_pid(monkeypatch):
    # A pid outlives nothing. If the number was reused, the record describes a
    # process that is gone and the resume would land in a stranger's prompt.
    monkeypatch.setattr(session_target, "_proc_start", lambda pid: "different")
    monkeypatch.setattr(session_target, "resolve",
                        lambda pid, **kw: pytest.fail("must not resolve"))
    pending.record(_promise())
    assert inject.replay_pending() == {"pending": 1, "resumed": 0}
    assert pending.load_all() == []


def test_replay_drops_a_session_that_is_simply_gone(monkeypatch):
    monkeypatch.setattr(session_target, "_proc_start", lambda pid: None)
    pending.record(_promise())
    assert inject.replay_pending() == {"pending": 1, "resumed": 0}
    assert pending.load_all() == []


def test_replay_drops_a_session_it_can_no_longer_reach(monkeypatch):
    # E.g. the daemon roster no longer lists it. Keeping the record would retry
    # the same refusal on every boot forever.
    monkeypatch.setattr(session_target, "_proc_start", lambda pid: "999")
    monkeypatch.setattr(session_target, "resolve", lambda pid, **kw: (_ for _ in ()).throw(
        session_target.ResolveError("gone from the roster")))
    pending.record(_promise())
    assert inject.replay_pending() == {"pending": 1, "resumed": 0}
    assert pending.load_all() == []
