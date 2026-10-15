"""Caller identity, the background-session backend, and the permission-mode verb.

`test_reflection.py` covers the tmux backend through an injected `runner`; this
file covers the pieces added when reflection stopped being tmux-only. The daemon
backend gets the equivalent seam — a fake socket — so none of this needs a live
`claude daemon` to run.

The wire protocol itself moved to the `awm.claudedaemon` component along with
its tests when a second service began speaking it. What stayed here is what
reflection owns: resolving a caller to one session, refusing anything else, and
the contract both lanes answer to.
"""
from __future__ import annotations

import json
import struct

import pytest

pytestmark = [pytest.mark.smoke]

from awm import claudedaemon
from awm.reflection import (oc_inject, permission_mode, session_target,
                            tmux_inject)


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
        reader = claudedaemon._FrameReader()
        out = []
        for blob in self.sent:
            out.extend(reader.feed(blob))
        return out


HELLO = ctl({"t": "hello", "replPid": 4242, "version": "2.1.223"}) + ctl({"t": "live"})


def target(**over) -> session_target.DaemonLane:
    base = dict(sock="/tmp/fake.sock", auth="tok", session_id="sid-1",
                repl_pid=4242, name="test", cli_version="2.1.223",
                dec_modes=(2004,))
    base.update(over)
    return session_target.DaemonLane(**base)


def opener_for(sock: FakeSock):
    return lambda _path: sock


def paste_and_submit(text, tgt, *, enter=True, opener):
    """Write ``text`` down the lane the way the sender does, minus the retrying.

    The sender owns detection, verification and retrying; this file owns the
    protocol. Going through ``open_lane`` rather than poking the connection
    directly keeps the handshake and the identity check in the path, which is
    where several of the tests below expect their refusals to come from.
    """
    with claudedaemon.open_lane(tgt, opener=opener) as conn:
        conn.write(text)
        # Not optional garnish: pumping the socket is what surfaces an
        # `auth-required`, and it is the one verb on the writer protocol a lane
        # may use to say it discarded what it was handed. Skipping it here would
        # make a host that dropped our input look like a clean write.
        conn.check_not_rejected()
        if enter:
            conn.commit()




def test_no_lane_offers_the_sender_a_screen_to_judge_a_write_by():
    # There is no read-back on the writer protocol and no flag saying whether to
    # believe one. Both used to exist because the two lanes hand back different
    # KINDS of thing — `capture-pane` renders the current screen, this is a byte
    # stream of the TUI's repaint deltas — and the flag was how the sender knew
    # which silence to trust. It turned out neither is trustworthy: the TUI
    # repaints the composer when it feels like it (21ms into an empty composer,
    # not at all within five seconds into one that already held text, both times
    # delivered), and Claude Code paints no composer at all while it compacts.
    sock = FakeSock(ctl({"t": "hello", "replPid": 4242}))
    with claudedaemon.open_lane(target(), opener=opener_for(sock)) as conn:
        assert not hasattr(conn, "read_back")
        assert not hasattr(conn, "read_back_is_evidence")
    assert not hasattr(tmux_inject._TmuxWriter, "read_back")
    assert not hasattr(tmux_inject._TmuxWriter, "read_back_is_evidence")


def test_every_lane_answers_the_rejection_check():
    # The sender calls this between the write and the commit and must not be able
    # to tell which lane it is holding. Only the daemon lane has anything to
    # report; the others answer by not raising, which is the contract.
    sock = FakeSock(ctl({"t": "hello", "replPid": 4242}))
    with claudedaemon.open_lane(target(), opener=opener_for(sock)) as conn:
        conn.check_not_rejected()
    for cls in (tmux_inject._TmuxWriter, oc_inject._ServeWriter):
        assert callable(getattr(cls, "check_not_rejected"))


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


def test_expect_session_refuses_a_pid_that_resolved_elsewhere(tmp_path, monkeypatch):
    # The hook lane's failure mode: an ancestry walk past a session whose record
    # is missing or not yet written lands on whoever launched it. The caller
    # already knows its own sessionId, so a mismatch refuses here rather than
    # typing into a stranger's prompt.
    monkeypatch.setattr(session_target, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(session_target, "_proc_start", lambda pid: "111")
    _write_session(tmp_path, 1234, procStart="111", sessionId="the-launcher")
    with pytest.raises(session_target.ResolveError, match="different conversation"):
        session_target.resolve(1234, expect_session="mine")


def test_expect_session_can_only_refuse_never_redirect(tmp_path, monkeypatch):
    # Naming a session that exists does not fetch it: the pid still decides, so
    # the check is a narrowing step and not a way to address another session.
    monkeypatch.setattr(session_target, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(session_target, "_proc_start", lambda pid: "111")
    monkeypatch.setattr(tmux_inject, "pane_for_pid", lambda pid, **kw: "%3")
    _write_session(tmp_path, 1234, procStart="111", sessionId="mine")
    _write_session(tmp_path, 5678, procStart="111", sessionId="theirs")
    assert session_target.resolve(1234, expect_session="mine").session_id == "mine"
    with pytest.raises(session_target.ResolveError, match="different conversation"):
        session_target.resolve(1234, expect_session="theirs")


def test_no_expect_session_leaves_resolution_as_it_was(tmp_path, monkeypatch):
    monkeypatch.setattr(session_target, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(session_target, "_proc_start", lambda pid: "111")
    monkeypatch.setattr(tmux_inject, "pane_for_pid", lambda pid, **kw: "%3")
    _write_session(tmp_path, 1234, procStart="111", sessionId="whatever")
    assert session_target.resolve(1234).session_id == "whatever"


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
    assert isinstance(t, session_target.DaemonLane)
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


def _run_mode(cycle, monkeypatch, lane=None):
    sess = FakeModeSession(cycle)
    lane = lane or session_target.TmuxLane(pane="%1", session_id="sid",
                                           repl_pid=1234)
    monkeypatch.setattr(session_target, "resolve", lambda pid, **kw: lane)
    monkeypatch.setattr(permission_mode, "_open", lambda *a, **kw: sess)
    res = permission_mode.ensure_bypass(caller_pid=1234, sleep=lambda _s: None)
    return res, sess


def test_mode_reports_which_lane_it_was_on(monkeypatch):
    # An unreadable footer means different things per lane — a modal over a tmux
    # pane, which passes, versus a pty stream with no footer paint in it, which
    # does not — so the caller needs to know which one it got.
    daemon = session_target.DaemonLane(sock="/tmp/s", auth="t", session_id="sid",
                                       repl_pid=1234)
    res, sess = _run_mode(["unknown"], monkeypatch, lane=daemon)
    assert res["hosting"] == "background" and sess.presses == 0
    res, _ = _run_mode(["auto", "default", "acceptEdits", "plan",
                        "bypassPermissions"], monkeypatch)
    assert res["hosting"] == "tmux"


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
    sess = permission_mode._DaemonSession(target(), opener=opener_for(sock))
    assert permission_mode.classify(sess.read()) == "auto"
    sess.forget()
    # Nothing new arrives, so the stale label must not resurface.
    assert permission_mode.classify(sess.read()) == "unknown"
