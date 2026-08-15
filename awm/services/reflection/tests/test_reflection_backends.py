"""Caller identity, the background-session backend, and the permission-mode verb.

`test_reflection.py` covers the tmux backend through an injected `runner`; this
file covers the pieces added when reflection stopped being tmux-only. The daemon
backend gets the equivalent seam — a fake socket — so none of this needs a live
`claude daemon` to run.
"""
from __future__ import annotations

import json
import struct

import pytest

pytestmark = [pytest.mark.smoke]

from awm.reflection import daemon_inject, permission_mode, session_target


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def frame(kind: int, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + bytes([kind]) + payload


def ctl(obj: dict) -> bytes:
    return frame(0x01, json.dumps(obj).encode())


def raw(text: str) -> bytes:
    return frame(0x00, text.encode())


class FakeSock:
    """A scripted PTY socket. Serves ``inbound`` once, then times out."""

    def __init__(self, inbound: bytes = b""):
        self._inbound = bytearray(inbound)
        self.sent: list[bytes] = []
        self.closed = False

    def settimeout(self, t):  # noqa: D102
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
        self.closed = True

    # -- assertions helpers ------------------------------------------------

    def frames(self) -> list[tuple[int, bytes]]:
        reader = daemon_inject._FrameReader()
        out = []
        for blob in self.sent:
            out.extend(reader.feed(blob))
        return out


HELLO = ctl({"t": "hello", "replPid": 4242, "version": "2.1.223"}) + ctl({"t": "live"})


def target(**over) -> session_target.DaemonTarget:
    base = dict(sock="/tmp/fake.sock", auth="tok", session_id="sid-1",
                repl_pid=4242, name="test", cli_version="2.1.223",
                dec_modes=(2004,))
    base.update(over)
    return session_target.DaemonTarget(**base)


def opener_for(sock: FakeSock):
    return lambda _path: sock


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------

def test_frame_length_excludes_the_type_byte():
    # The single most expensive detail to get wrong: assuming the length covers
    # the type byte misparses everything after the first frame.
    encoded = daemon_inject.frame(0x00, b"abcd")
    assert encoded == b"\x00\x00\x00\x04" + b"\x00" + b"abcd"
    assert struct.unpack(">I", encoded[:4])[0] == 4


def test_handshake_keeps_frames_that_share_a_read_with_live():
    # One read can carry the greeting, the `live` marker, and the first screen
    # paint. Stopping at `live` without finishing the batch drops the paint, and
    # it is not recoverable — the bytes are gone from the socket.
    sock = FakeSock(ctl({"t": "hello", "replPid": 4242}) + ctl({"t": "live"})
                    + raw("⏵⏵ bypass permissions on"))
    conn = daemon_inject._connect(target(), opener=opener_for(sock),
                                  sleep=lambda _s: None)
    assert "bypass permissions on" in conn.screen()


def test_frame_reader_splits_a_coalesced_stream():
    stream = ctl({"t": "hello", "replPid": 4242}) + raw("xy") + ctl({"t": "live"})
    reader = daemon_inject._FrameReader()
    # Deliver it one byte at a time — a reader that assumes whole frames per
    # recv() would fall apart here, and real sockets do coalesce and split.
    got = []
    for i in range(len(stream)):
        got.extend(reader.feed(stream[i:i + 1]))
    assert [k for k, _ in got] == [0x01, 0x00, 0x01]
    assert got[1][1] == b"xy"


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------

def test_authenticates_before_sending_any_input():
    sock = FakeSock(HELLO)
    daemon_inject._paste_and_submit("/compact", target(), enter=True,
                                    opener=opener_for(sock), sleep=lambda _s: None)
    frames = sock.frames()
    kinds = [k for k, _ in frames]
    assert kinds[0] == 0x01, "first frame must be the auth control frame"
    assert json.loads(frames[0][1])["t"] == "auth"
    assert 0x00 not in kinds[:1]


def test_text_is_delivered_as_a_bracketed_paste():
    # Without bracketing, a leading `/` opens the TUI slash menu instead of
    # landing as text — the same reason the tmux path pastes with `-p`.
    sock = FakeSock(HELLO)
    daemon_inject._paste_and_submit("/compact", target(), enter=True,
                                    opener=opener_for(sock), sleep=lambda _s: None)
    payloads = [p for k, p in sock.frames() if k == 0x00]
    assert payloads[0] == b"\x1b[200~/compact\x1b[201~"


def test_enter_is_a_separate_write_after_the_paste():
    sock = FakeSock(HELLO)
    daemon_inject._paste_and_submit("/compact", target(), enter=True,
                                    opener=opener_for(sock), sleep=lambda _s: None)
    payloads = [p for k, p in sock.frames() if k == 0x00]
    assert payloads == [b"\x1b[200~/compact\x1b[201~", b"\r"]


def test_no_enter_when_not_submitting():
    sock = FakeSock(HELLO)
    daemon_inject._paste_and_submit("draft", target(), enter=False,
                                    opener=opener_for(sock), sleep=lambda _s: None)
    payloads = [p for k, p in sock.frames() if k == 0x00]
    assert payloads == [b"\x1b[200~draft\x1b[201~"]


def test_unbracketed_when_the_session_does_not_report_the_mode():
    sock = FakeSock(HELLO)
    daemon_inject._paste_and_submit("hello", target(dec_modes=(1000,)), enter=False,
                                    opener=opener_for(sock), sleep=lambda _s: None)
    payloads = [p for k, p in sock.frames() if k == 0x00]
    assert payloads == [b"hello"]


def test_ping_is_answered_with_a_pong():
    sock = FakeSock(ctl({"t": "hello", "replPid": 4242}) + ctl({"t": "ping"})
                    + ctl({"t": "live"}))
    daemon_inject._paste_and_submit("hi", target(), enter=False,
                                    opener=opener_for(sock), sleep=lambda _s: None)
    sent = [json.loads(p) for k, p in sock.frames() if k == 0x01]
    assert {"t": "pong"} in sent


def test_auth_required_is_surfaced_not_swallowed():
    sock = FakeSock(ctl({"t": "hello", "replPid": 4242}) + ctl({"t": "auth-required"}))
    with pytest.raises(daemon_inject.DaemonError, match="input token"):
        daemon_inject._paste_and_submit("hi", target(), enter=False,
                                        opener=opener_for(sock), sleep=lambda _s: None)


def test_unrecognised_greeting_refuses():
    # A CLI update that moves this private protocol should degrade to a clear
    # refusal, not to writing bytes at a socket we no longer understand.
    sock = FakeSock(ctl({"t": "hello", "somethingElse": True, "version": "9.9.9"}))
    with pytest.raises(daemon_inject.DaemonError, match="unrecognised"):
        daemon_inject._paste_and_submit("hi", target(), enter=False,
                                        opener=opener_for(sock), sleep=lambda _s: None)


def test_send_refuses_modal_and_destructive_commands_on_this_backend_too():
    # The guards are a property of the TUI, not of the transport; a background
    # session must refuse exactly what a tmux one does.
    sock = FakeSock(HELLO)
    res = daemon_inject.send("/status", target=target(), opener=opener_for(sock),
                             sleep=lambda _s: None, spawn=lambda fn: None)
    assert res["refused"] and res["kind"] == "interactive"

    res = daemon_inject.send("/clear", target=target(), opener=opener_for(sock),
                             sleep=lambda _s: None, spawn=lambda fn: None)
    assert res["refused"]
    assert not sock.sent, "nothing may be written when the command is refused"


def test_send_defers_the_followup_for_a_slash_command():
    sock = FakeSock(HELLO)
    scheduled = []
    res = daemon_inject.send("/compact", target=target(), opener=opener_for(sock),
                             sleep=lambda _s: None, spawn=scheduled.append)
    assert res["followup_deferred"] is True
    assert res["followup"] == "Continue with what you were doing."
    assert len(scheduled) == 1, "the resume must be deferred, never co-queued"


def test_send_does_not_follow_up_a_plain_prompt():
    sock = FakeSock(HELLO)
    scheduled = []
    res = daemon_inject.send("just a prompt", target=target(),
                             opener=opener_for(sock), sleep=lambda _s: None,
                             spawn=scheduled.append)
    assert res["followup_deferred"] is False
    assert scheduled == []


# ---------------------------------------------------------------------------
# Deferred follow-up on the daemon path
# ---------------------------------------------------------------------------

def _watch(statuses, *, injected_at_ms=1000, sock=None):
    """Drive the watcher over a scripted sequence of (status, statusUpdatedAt)."""
    sock = sock or FakeSock(HELLO * 40)
    seq = list(statuses)

    def fake_status(_pid):
        return seq.pop(0) if seq else ("idle", injected_at_ms + 10_000)

    orig = daemon_inject._status
    daemon_inject._status = fake_status
    try:
        daemon_inject._await_and_followup(
            "/compact", "resume", target(), opener=opener_for(sock),
            injected_at_ms=injected_at_ms, sleep=lambda _s: None,
            clock=_Clock(), now_ms=lambda: injected_at_ms)
    finally:
        daemon_inject._status = orig
    return sock


class _Clock:
    def __init__(self, step=1.0):
        self.t, self.step = 0.0, step

    def __call__(self):
        v = self.t
        self.t += self.step
        return v


def test_followup_waits_out_the_pre_compaction_idle_beat(monkeypatch):
    # The hazard the whole deferred design exists for: /compact runs at end of
    # turn, so the session dips idle *before* compacting. Firing there resumes on
    # the old context. Only a settled idle counts.
    monkeypatch.setattr(session_target, "resolve",
                        lambda pid, **kw: target())
    sock = _watch([
        ("busy", 1100),
        ("idle", 1200),          # the trap: brief idle before compaction starts
        ("busy", 1300),          # compacting
        ("busy", 1400),
        ("idle", 1500),
        ("idle", 1600),
        ("idle", 1700),          # third settled sample -> fire
    ])
    payloads = [p for k, p in sock.frames() if k == 0x00]
    assert any(b"resume" in p for p in payloads)


def test_followup_ignores_an_idle_older_than_the_injection(monkeypatch):
    # A session that was already idle before we typed has not reacted yet; its
    # stale idle must not be mistaken for the command having finished.
    monkeypatch.setattr(session_target, "resolve", lambda pid, **kw: target())
    fired_after = []

    seq = [("idle", 900)] * 5 + [("busy", 1300), ("idle", 1500),
                                 ("idle", 1600), ("idle", 1700)]
    remaining = list(seq)

    def fake_status(_pid):
        fired_after.append(len(seq) - len(remaining))
        return remaining.pop(0) if remaining else ("idle", 1800)

    monkeypatch.setattr(daemon_inject, "_status", fake_status)
    sock = FakeSock(HELLO * 40)
    daemon_inject._await_and_followup(
        "/compact", "resume", target(), opener=opener_for(sock),
        injected_at_ms=1000, sleep=lambda _s: None, clock=_Clock(),
        now_ms=lambda: 1000)
    # It must have waited through all five stale-idle samples rather than
    # firing on the first of them.
    assert max(fired_after) >= 5
    payloads = [p for k, p in sock.frames() if k == 0x00]
    assert any(b"resume" in p for p in payloads)


def test_followup_fires_even_if_no_busy_sample_is_ever_caught(monkeypatch):
    # Regression: a short conversation compacts in a couple of seconds, so the
    # busy window can close entirely between two polls. A watcher that waits for
    # a busy *sighting* then sits out the full 15-minute cap, leaving the session
    # idle — which is the exact failure the deferred resume exists to prevent.
    monkeypatch.setattr(session_target, "resolve", lambda pid, **kw: target())
    sock = _watch([
        ("idle", 900),           # pre-injection state
        ("idle", 1400),          # reacted: timestamp moved, busy never sampled
        ("idle", 1400),
        ("idle", 1400),
    ])
    payloads = [p for k, p in sock.frames() if k == 0x00]
    assert any(b"resume" in p for p in payloads)


def test_followup_gives_up_if_the_session_disappears(monkeypatch):
    monkeypatch.setattr(session_target, "resolve", lambda pid, **kw: target())
    sock = _watch([("busy", 1100), (None,)][:1] + [None])
    payloads = [p for k, p in sock.frames() if k == 0x00]
    assert payloads == [], "a vanished session must not be typed into"


def test_followup_re_resolves_the_roster_before_injecting(monkeypatch):
    # A background session that respawned mid-wait has a new socket and a new
    # token; the ones captured at injection time are dead.
    fresh = target(sock="/tmp/new.sock", auth="new-token")
    monkeypatch.setattr(session_target, "resolve", lambda pid, **kw: fresh)
    seen_paths = []

    def opener(path):
        seen_paths.append(path)
        return FakeSock(HELLO)

    seq = [("busy", 1100), ("idle", 1500), ("idle", 1600), ("idle", 1700)]
    monkeypatch.setattr(daemon_inject, "_status",
                        lambda _pid: seq.pop(0) if seq else ("idle", 9999))
    daemon_inject._await_and_followup(
        "/compact", "resume", target(), opener=opener, injected_at_ms=1000,
        sleep=lambda _s: None, clock=_Clock(), now_ms=lambda: 1000)
    assert seen_paths == ["/tmp/new.sock"]


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------

def _write_session(tmp_path, pid, **fields):
    record = {"pid": pid, "sessionId": f"sid-{pid}", "kind": "interactive",
              "name": f"s{pid}"}
    record.update(fields)
    (tmp_path / f"{pid}.json").write_text(json.dumps(record))
    return record


def test_missing_session_record_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(session_target, "SESSIONS_DIR", tmp_path)
    with pytest.raises(session_target.ResolveError, match="no Claude Code session"):
        session_target.resolve(1234)


def test_recycled_pid_refuses(tmp_path, monkeypatch):
    # Records are keyed by pid and pids get reused. A record whose procStart
    # disagrees with the live process describes somebody else entirely.
    monkeypatch.setattr(session_target, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(session_target, "_proc_start", lambda pid: "999")
    _write_session(tmp_path, 1234, procStart="111")
    with pytest.raises(session_target.ResolveError, match="stale"):
        session_target.resolve(1234)


def test_dead_pid_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(session_target, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(session_target, "_proc_start", lambda pid: None)
    _write_session(tmp_path, 1234, procStart="111")
    with pytest.raises(session_target.ResolveError, match="is gone"):
        session_target.resolve(1234)


def test_unknown_hosting_kind_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(session_target, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(session_target, "_proc_start", lambda pid: "111")
    _write_session(tmp_path, 1234, procStart="111", kind="something-new")
    with pytest.raises(session_target.ResolveError, match="unrecognised hosting"):
        session_target.resolve(1234)


def _bg_roster(tmp_path, monkeypatch, workers, *, children=None, dead=()):
    """Point resolve() at a fake roster and a fake process table.

    ``children`` is the ppid → [children] map ancestry is checked against; the
    default hangs REPL pid 1234 off host pid 49641, which is the real shape (the
    roster's pid is the bg-pty-host, and the REPL is its child). ``dead`` names
    pids that read as no longer running.
    """
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"workers": workers}))
    monkeypatch.setattr(session_target, "ROSTER_PATH", roster)
    monkeypatch.setattr(session_target, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(session_target, "_proc_start",
                        lambda pid: None if pid in dead else "111")
    monkeypatch.setattr(session_target, "PROCESS_CHILDREN",
                        lambda: {49641: [1234]} if children is None else children)


def test_background_session_joins_the_roster_on_job_id(tmp_path, monkeypatch):
    # jobId names the worker directly, and unlike sessionId it is fixed for the
    # life of the job. The roster's own `pid` is the bg-pty-host, not the REPL,
    # so it is no use as a key — it is what the match is *checked* against.
    _write_session(tmp_path, 1234, procStart="111", kind="bg",
                   sessionId="933ba47f-real", jobId="f65fc961")
    _bg_roster(tmp_path, monkeypatch, {
        "unrelated": {"sessionId": "other", "pid": 777,
                      "ptySock": "/tmp/wrong.sock", "ptyAuth": "no"},
        "f65fc961": {"sessionId": "933ba47f-real", "pid": 49641,
                     "ptySock": "/tmp/right.sock", "ptyAuth": "yes",
                     "decModes": [2004]},
    })
    t = session_target.resolve(1234)
    assert isinstance(t, session_target.DaemonTarget)
    assert t.sock == "/tmp/right.sock" and t.auth == "yes"


def test_background_session_survives_a_session_id_that_drifted(tmp_path, monkeypatch):
    # The observed failure: clearing a conversation rewrites the record with a
    # new sessionId while the roster keeps the id the job was dispatched with.
    # Joining on sessionId loses the session permanently; jobId still names it.
    _write_session(tmp_path, 1234, procStart="111", kind="bg",
                   sessionId="05d84080-after-clear", jobId="f65fc961")
    _bg_roster(tmp_path, monkeypatch, {
        "f65fc961": {"sessionId": "8b5418bc-at-dispatch", "pid": 49641,
                     "ptySock": "/tmp/right.sock", "ptyAuth": "yes"},
    })
    t = session_target.resolve(1234)
    assert t.sock == "/tmp/right.sock"


def test_background_session_without_a_job_id_still_joins_on_session_id(
        tmp_path, monkeypatch):
    # Records written by a CLI old enough not to carry jobId behave as before.
    _write_session(tmp_path, 1234, procStart="111", kind="bg",
                   sessionId="933ba47f-real")
    _bg_roster(tmp_path, monkeypatch, {
        "f65fc961": {"sessionId": "933ba47f-real", "pid": 49641,
                     "ptySock": "/tmp/right.sock", "ptyAuth": "yes"},
    })
    assert session_target.resolve(1234).sock == "/tmp/right.sock"


def test_background_session_falls_back_to_the_host_that_contains_it(
        tmp_path, monkeypatch):
    # Neither key matches, but exactly one listed PTY host has this REPL in its
    # subtree — an observed fact, and a narrower question than "is one of my
    # ancestors listed" (the daemon supervisor is an ancestor of them all).
    _write_session(tmp_path, 1234, procStart="111", kind="bg",
                   sessionId="unknown", jobId="unknown")
    _bg_roster(tmp_path, monkeypatch, {
        "elsewhere": {"sessionId": "other", "pid": 777,
                      "ptySock": "/tmp/wrong.sock", "ptyAuth": "no"},
        "f65fc961": {"sessionId": "stale", "pid": 49641,
                     "ptySock": "/tmp/right.sock", "ptyAuth": "yes"},
    })
    assert session_target.resolve(1234).sock == "/tmp/right.sock"


def test_background_session_refuses_a_live_host_that_does_not_contain_it(
        tmp_path, monkeypatch):
    # The join key matched but the process tree says otherwise: that entry is
    # somebody else's session, and typing into it is the one thing reflection
    # must never do.
    _write_session(tmp_path, 1234, procStart="111", kind="bg",
                   sessionId="933ba47f-real", jobId="f65fc961")
    _bg_roster(tmp_path, monkeypatch, {
        "f65fc961": {"sessionId": "933ba47f-real", "pid": 49641,
                     "ptySock": "/tmp/right.sock", "ptyAuth": "yes"},
    }, children={49641: [5555]})
    with pytest.raises(session_target.ResolveError,
                       match="does not contain pid 1234"):
        session_target.resolve(1234)


def test_background_session_tolerates_a_host_that_is_gone(tmp_path, monkeypatch):
    # Nothing to contradict: a dead host cannot be asked what it contains, and
    # the socket connect that follows fails with an accurate message of its own.
    _write_session(tmp_path, 1234, procStart="111", kind="bg", jobId="f65fc961")
    _bg_roster(tmp_path, monkeypatch, {
        "f65fc961": {"sessionId": "whatever", "pid": 49641,
                     "ptySock": "/tmp/right.sock", "ptyAuth": "yes"},
    }, children={}, dead=(49641,))
    assert session_target.resolve(1234).sock == "/tmp/right.sock"


def test_background_session_with_no_roster_entry_refuses(tmp_path, monkeypatch):
    _write_session(tmp_path, 1234, procStart="111", kind="bg", sessionId="gone",
                   jobId="also-gone")
    _bg_roster(tmp_path, monkeypatch, {}, children={})
    with pytest.raises(session_target.ResolveError, match="no daemon roster entry"):
        session_target.resolve(1234)


def test_no_roster_entry_does_not_blame_the_pty_host(tmp_path, monkeypatch):
    # The message an agent reads and acts on. It used to assert the PTY host had
    # probably exited, which sent a reader looking at a process that was alive.
    _write_session(tmp_path, 1234, procStart="111", kind="bg", jobId="f65fc961")
    _bg_roster(tmp_path, monkeypatch, {}, children={})
    with pytest.raises(session_target.ResolveError) as exc:
        session_target.resolve(1234)
    assert "may have exited" not in str(exc.value)
    assert "f65fc961" in str(exc.value)


# ---------------------------------------------------------------------------
# Permission mode
# ---------------------------------------------------------------------------

def test_classify_reads_the_most_recent_indicator():
    # Both capture paths return text containing earlier paints of the footer, so
    # after a change the old label is still there and only order distinguishes.
    screen = "⏸ plan mode on (shift+tab to cycle)\n⏵⏵ bypass permissions on"
    assert permission_mode.classify(screen) == "bypassPermissions"
    screen = "⏵⏵ bypass permissions on\n⏵⏵ auto mode on (shift+tab to cycle)"
    assert permission_mode.classify(screen) == "auto"


def test_classify_unknown_when_no_indicator_is_visible():
    assert permission_mode.classify("Enter to select · Esc to cancel") == "unknown"


class FakeModeSession:
    """A session whose displayed mode advances through a scripted cycle."""

    label = "fake"

    def __init__(self, cycle):
        self._cycle = list(cycle)
        self._at = 0
        self.presses = 0
        self.closed = False

    def read(self):
        return {"bypassPermissions": "⏵⏵ bypass permissions on",
                "acceptEdits": "⏵⏵ accept edits on",
                "plan": "⏸ plan mode on",
                "auto": "⏵⏵ auto mode on",
                # `default` draws no mode indicator at all — indistinguishable
                # from a footer we failed to read, which is why it classifies as
                # unknown rather than as a mode.
                "default": "❯ ",
                "unknown": "Esc to cancel"}[self._cycle[self._at]]

    def forget(self):
        self.forgot = getattr(self, "forgot", 0) + 1

    def cycle(self):
        self.presses += 1
        self._at = (self._at + 1) % len(self._cycle)

    def close(self):
        self.closed = True


def _run_mode(cycle, monkeypatch):
    sess = FakeModeSession(cycle)
    monkeypatch.setattr(session_target, "resolve", lambda pid, **kw: object())
    monkeypatch.setattr(permission_mode, "_open", lambda *a, **kw: sess)
    res = permission_mode.ensure_bypass(caller_pid=1234, sleep=lambda _s: None)
    return res, sess


def test_mode_is_a_noop_when_already_in_bypass(monkeypatch):
    res, sess = _run_mode(["bypassPermissions", "auto"], monkeypatch)
    assert res["ok"] and res["changed"] is False and res["steps"] == 0
    assert sess.presses == 0


def test_mode_cycles_from_auto_to_bypass(monkeypatch):
    # The real cycle order, entered at `auto` — which is where a plan approved
    # from a phone leaves the session.
    res, sess = _run_mode(
        ["auto", "default", "acceptEdits", "plan", "bypassPermissions"],
        monkeypatch)
    assert res["ok"] and res["mode"] == "bypassPermissions"
    # `default` shows no indicator, so it reads as unknown on the way past —
    # that must not stop the walk.
    assert sess.presses == res["steps"] == 4


def test_mode_gives_up_when_bypass_is_not_in_the_cycle(monkeypatch):
    # A session where bypass is unavailable never offers it; cycling forever is
    # not an option, and neither is leaving the session somewhere random.
    res, sess = _run_mode(["auto", "acceptEdits", "plan"], monkeypatch)
    assert res["ok"] is False
    assert "never offers bypass" in res["error"]
    assert res["mode"] == "auto", "must come back to where it started"
    assert sess.closed


def test_mode_refuses_when_the_indicator_is_not_visible(monkeypatch):
    # A modal covers the footer and swallows keystrokes; pressing blind could
    # select an arbitrary menu entry.
    res, sess = _run_mode(["unknown", "auto"], monkeypatch)
    assert res["ok"] is False and res["mode"] == "unknown"
    assert sess.presses == 0, "nothing may be sent when we cannot see the mode"


def test_mode_refuses_without_a_caller():
    with pytest.raises(session_target.ResolveError, match="acts on the caller"):
        permission_mode.ensure_bypass(caller_pid=None)


def test_mode_discards_buffered_output_before_each_press(monkeypatch):
    # The daemon transport is an append-only byte stream, so every footer ever
    # drawn is still in the buffer. `default` draws no indicator at all, so
    # without discarding first, a session sitting in default reads back as
    # whichever mode it was in before — observed live, and it can stop the walk
    # early by looking like it came full circle.
    res, sess = _run_mode(
        ["auto", "default", "acceptEdits", "plan", "bypassPermissions"],
        monkeypatch)
    assert res["ok"]
    assert sess.forgot == sess.presses, "every press must be preceded by a discard"


def test_daemon_session_forget_clears_the_buffer():
    sock = FakeSock(HELLO + raw("⏵⏵ auto mode on"))
    sess = permission_mode._DaemonSession(target(), opener=opener_for(sock),
                                          sleep=lambda _s: None)
    assert permission_mode.classify(sess.read()) == "auto"
    sess.forget()
    # Nothing new arrives, so the stale label must not resurface.
    assert permission_mode.classify(sess.read()) == "unknown"


# ---------------------------------------------------------------------------
# Identity: is this socket the caller's own session?
# ---------------------------------------------------------------------------

def test_a_socket_hosting_another_repl_refuses():
    # The roster hands out recycled `spare/*.pty.sock` paths, so the socket it
    # named can belong to a different job by the time we dial it. The greeting
    # says whose it is; not checking meant typing a stranger's compact into a
    # stranger's prompt and reporting success.
    sock = FakeSock(ctl({"t": "hello", "replPid": 999, "version": "2.1.223"})
                    + ctl({"t": "live"}))
    with pytest.raises(daemon_inject.DaemonError, match="hosting REPL 999"):
        daemon_inject._paste_and_submit("hi", target(), enter=False,
                                        opener=opener_for(sock),
                                        sleep=lambda _s: None)


def test_a_host_that_never_greets_refuses():
    # This used to pass straight through: the unrecognised-message check was
    # guarded on having received *something*, so silence sailed past it and
    # authentication proceeded into a host that never identified itself.
    with pytest.raises(daemon_inject.DaemonError, match="never greeted"):
        daemon_inject._paste_and_submit("hi", target(), enter=False,
                                        opener=opener_for(FakeSock(b"")),
                                        sleep=lambda _s: None)


def test_enter_refuses_before_authentication():
    conn = daemon_inject.Connection(target(), opener=opener_for(FakeSock(HELLO)),
                                    sleep=lambda _s: None)
    with pytest.raises(daemon_inject.DaemonError, match="before authenticating"):
        conn.press_enter()


# ---------------------------------------------------------------------------
# Rejection observed after the write, not after the socket is closed
# ---------------------------------------------------------------------------

class RejectingSock(FakeSock):
    """A host that greets normally, then discards the first raw frame it gets.

    Modelled on the real failure: an unauthenticated raw frame is dropped
    silently apart from the `auth-required` that follows it, and the write path
    used to close the socket before that frame could arrive.
    """

    def sendall(self, data):
        super().sendall(data)
        if any(k == 0x00 for k, _ in daemon_inject._FrameReader().feed(data)):
            self._inbound += ctl({"t": "auth-required"})


def test_input_the_host_discarded_is_not_reported_as_sent():
    sock = RejectingSock(HELLO)
    with pytest.raises(daemon_inject.DaemonError, match="input token"):
        daemon_inject._paste_and_submit("hi", target(), enter=False,
                                        opener=opener_for(sock),
                                        sleep=lambda _s: None)


# ---------------------------------------------------------------------------
# Crossing a re-host: one re-resolve, one retry
# ---------------------------------------------------------------------------

def _openers(mapping: dict):
    """An opener that serves a different scripted socket per path."""
    def opener(path):
        if path not in mapping:
            raise daemon_inject.DaemonError(f"cannot reach {path}")
        return mapping[path]
    return opener


def test_a_re_homed_session_is_reached_on_the_retry():
    # The session was attached/detached mid-call, so the roster entry we were
    # dispatched with names a socket that is now somebody else's. Re-reading the
    # roster and trying once more is what makes that survivable rather than a
    # silent no-op.
    fresh_sock = FakeSock(HELLO)
    opener = _openers({"/tmp/fresh.sock": fresh_sock})
    moved = target(sock="/tmp/fresh.sock", auth="tok2")

    submitted, used = daemon_inject._deliver(
        "/compact", target(), enter=True, opener=opener,
        sleep=lambda _s: None, resolve=lambda _pid: moved)

    assert submitted is True
    assert used.sock == "/tmp/fresh.sock", "the retry must use the fresh address"
    assert any(k == 0x00 for k, _ in fresh_sock.frames())


def test_the_retry_is_tried_once_and_then_gives_up():
    # One attempt, not a loop: the point is to cross a single re-host, not to
    # grind against a session that is genuinely unreachable.
    attempts = []

    def opener(path):
        attempts.append(path)
        raise daemon_inject.DaemonError(f"cannot reach {path}")

    with pytest.raises(daemon_inject.DaemonError):
        daemon_inject._deliver("/compact", target(), enter=True, opener=opener,
                               sleep=lambda _s: None,
                               resolve=lambda _pid: target(sock="/tmp/b.sock"))
    assert attempts == ["/tmp/fake.sock", "/tmp/b.sock"]


def test_a_session_that_vanished_reports_both_halves():
    def resolve(_pid):
        raise session_target.ResolveError("calling process 4242 is gone")

    with pytest.raises(daemon_inject.DaemonError) as exc:
        daemon_inject._deliver("/compact", target(), enter=True,
                               opener=_openers({}), sleep=lambda _s: None,
                               resolve=resolve)
    # Both why the first attempt failed and why there was no second one.
    assert "cannot reach /tmp/fake.sock" in str(exc.value)
    assert "could not be re-resolved" in str(exc.value)


def test_no_resume_is_promised_for_a_command_that_never_went_in(monkeypatch):
    # The pending record is a promise to inject a resume once the command
    # finishes. Writing one for a command that failed to go in leaves a watcher
    # waiting on something that will never happen, and eventually types a bare
    # "continue" into an untouched session.
    recorded = []
    monkeypatch.setattr(daemon_inject.pending, "record", recorded.append)
    monkeypatch.setattr(daemon_inject.session_target, "resolve",
                        lambda _pid: target())

    with pytest.raises(daemon_inject.DaemonError):
        daemon_inject.send("/compact", target=target(), opener=_openers({}),
                           sleep=lambda _s: None, spawn=lambda fn: None)
    assert recorded == []


def test_a_failure_after_input_was_written_is_not_retried():
    # `send` carries arbitrary text, so re-pasting when we cannot know whether
    # the first copy landed is a worse outcome than failing. Only failures that
    # provably wrote nothing are retried.
    class DiesAfterTheFirstRawFrame(FakeSock):
        def sendall(self, data):
            kinds = [k for k, _ in daemon_inject._FrameReader().feed(data)]
            if 0x00 in kinds and self.sent and any(
                    0x00 in [k for k, _ in daemon_inject._FrameReader().feed(b)]
                    for b in self.sent):
                raise OSError("broken pipe")
            super().sendall(data)

    attempts = []

    def opener(path):
        attempts.append(path)
        return DiesAfterTheFirstRawFrame(HELLO)

    with pytest.raises(daemon_inject.DaemonError, match="writing to the PTY"):
        daemon_inject._deliver("do something twice", target(), enter=True,
                               opener=opener, sleep=lambda _s: None,
                               resolve=lambda _pid: target(sock="/tmp/b.sock"))
    assert attempts == ["/tmp/fake.sock"], "must not retry once input is on the wire"


def test_a_rejected_frame_still_retries():
    # Rejection is the host saying it threw the bytes away, so nothing landed
    # and the retry is safe — this is the common re-host case.
    socks = {"/tmp/fake.sock": RejectingSock(HELLO),
             "/tmp/fresh.sock": FakeSock(HELLO)}
    submitted, used = daemon_inject._deliver(
        "/compact", target(), enter=True, opener=_openers(socks),
        sleep=lambda _s: None,
        resolve=lambda _pid: target(sock="/tmp/fresh.sock"))
    assert submitted is True and used.sock == "/tmp/fresh.sock"
