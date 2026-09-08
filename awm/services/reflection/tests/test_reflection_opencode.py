"""The opencode backend: identity by cwd, observation by DB, injection by serve.

The claude lanes are covered by `test_reflection.py` and
`test_reflection_backends.py`; this file covers the pieces added when reflection
grew an opencode harness. The opencode identity is the caller's cwd (the DB
keys sessions by directory), its observation reads the same SQLite DB, and its
serve lane posts to `opencode serve` over HTTP. All of that is driven through
fakes here — a tmp SQLite DB, a fake fd/net table for serve-url discovery, and
a scripted HTTP opener — so none of this needs a live opencode process to run.
"""
from __future__ import annotations

import json
import os
import sqlite3

import pytest

pytestmark = [pytest.mark.smoke]

from awm.reflection import oc_inject, oc_observe, oc_session


# ---------------------------------------------------------------------------
# A tmp SQLite DB shaped like opencode's
# ---------------------------------------------------------------------------

def _mkdb(tmp_path, sessions, parts=()):
    """Build a fake opencode DB. ``sessions`` is id -> field dict; ``parts`` is
    a list of (session_id, message_ts, data_json) rows on the `part` table."""
    db = tmp_path / "opencode.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE session (id TEXT, slug TEXT, title TEXT, "
                 "directory TEXT, parent_id TEXT, time_updated INTEGER)")
    conn.execute("CREATE TABLE part (session_id TEXT, message_id INTEGER, "
                 "data TEXT)")
    conn.execute("CREATE TABLE message (id INTEGER, time_created INTEGER)")
    for sid, fields in sessions.items():
        conn.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?)",
            (sid, fields.get("slug", "slug"), fields.get("title", "title"),
             fields.get("directory", "/w"), fields.get("parent_id"),
             fields.get("time_updated", 0)))
    for i, (sid, ts, data) in enumerate(parts):
        conn.execute("INSERT INTO message (id, time_created) VALUES (?,?)",
                     (i + 1, ts))
        conn.execute("INSERT INTO part VALUES (?,?,?)", (sid, i + 1, data))
    conn.commit()
    conn.close()
    return db


def _fake_proc(monkeypatch, *, exe=True, cwd="/w", start="111"):
    """Make oc_session read a fake process for any pid."""
    monkeypatch.setattr(oc_session, "_proc_start", lambda pid: start)
    monkeypatch.setattr(oc_session, "_cwd", lambda pid: cwd)
    monkeypatch.setattr(oc_session, "_is_opencode",
                        lambda pid: bool(exe))
    monkeypatch.setattr(oc_session, "_default_pane_for_pid",
                        lambda pid: "%3")


def _use_db(monkeypatch, db_path):
    monkeypatch.setattr(oc_session, "DB_PATH", str(db_path))
    monkeypatch.setattr(oc_observe, "DB_PATH", str(db_path))


# ---------------------------------------------------------------------------
# serve_url_for — LISTEN state, not a port suffix
# ---------------------------------------------------------------------------

def test_serve_url_matches_a_listen_socket_any_port(monkeypatch):
    # The listener is port 4096 = 0x1000, which does not end in "0000". The
    # earlier check matched only hex ports ending in 0000 and missed every real
    # serve port — this asserts the state field (0A = LISTEN) is what decides.
    _patch_os(monkeypatch, fds=["17"], links={"17": "socket:[42]"})
    # Column layout matches /proc/net/tcp: sl local rem st tx rx tr retr uid
    # timeout inode ... — the inode (42) must sit at parts[9].
    tcp = ("sl local rem st tx rx tr retr uid timeout inode refs flags\n"
           " 1: 0100007F:1000 00000000:0000 0A 00000000:00000000 "
           "00:00000000 00000000 0 0 42 1 0 0 0 0")
    _patch_net_tcp(monkeypatch, tcp)
    assert oc_session.serve_url_for(1234) == "http://127.0.0.1:4096"


def test_serve_url_ignores_connected_not_listening(monkeypatch):
    # The fd table of a serve process also holds its outbound sockets (state 01
    # = ESTAB). Only a listening socket is an address to post an injection to.
    _patch_os(monkeypatch, fds=["3", "4"],
              links={"3": "socket:[99]", "4": "socket:[42]"})
    tcp = ("sl local rem st tx rx tr retr uid timeout inode refs flags\n"
           " 1: 0100007F:0000 00000000:0000 01 00000000:00000000 "
           " 00:00000000 00000000 0 0 99 1 0 0 0 0\n"
           " 2: 0100007F:1000 00000000:0000 0A 00000000:00000000 "
           " 00:00000000 00000000 0 0 42 1 0 0 0 0")
    _patch_net_tcp(monkeypatch, tcp)
    assert oc_session.serve_url_for(1234) == "http://127.0.0.1:4096"


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------

def test_resolve_finds_the_live_session_by_directory(tmp_path, monkeypatch):
    _use_db(monkeypatch, _mkdb(
        tmp_path,
        {"old": {"directory": "/w", "time_updated": 1, "parent_id": None},
         "live": {"directory": "/w", "time_updated": 2, "parent_id": None}}))
    _fake_proc(monkeypatch, cwd="/w")
    lane = oc_session.resolve(1234)
    assert lane.session_id == "live"
    assert lane.repl_pid == 1234


def test_resolve_skips_a_subagent_session(tmp_path, monkeypatch):
    # A session with a parent is somebody else's turn — a spawned agent writing
    # inside the caller's conversation. It must never shadow the caller's own.
    _use_db(monkeypatch, _mkdb(
        tmp_path, {"me": {"directory": "/w", "time_updated": 1},
                   "sub": {"directory": "/w", "time_updated": 9,
                           "parent_id": "me"}}))
    _fake_proc(monkeypatch, cwd="/w")
    assert oc_session.resolve(1234).session_id == "me"


def test_resolve_refuses_a_pid_that_is_not_opencode(monkeypatch):
    _fake_proc(monkeypatch, exe=False)
    with pytest.raises(oc_session.ResolveError, match="not an opencode session"):
        oc_session.resolve(1234)


def test_resolve_refuses_a_dead_pid(monkeypatch):
    _fake_proc(monkeypatch, start=None)
    with pytest.raises(oc_session.ResolveError, match="is gone"):
        oc_session.resolve(1234)


def test_resolve_refuses_when_no_session_is_open_there(tmp_path, monkeypatch):
    _use_db(monkeypatch, _mkdb(tmp_path, {}))
    _fake_proc(monkeypatch, cwd="/elsewhere")
    with pytest.raises(oc_session.ResolveError, match="no opencode session"):
        oc_session.resolve(1234)


def test_resolve_expect_session_can_only_refuse(tmp_path, monkeypatch):
    _use_db(monkeypatch, _mkdb(
        tmp_path, {"mine": {"directory": "/w", "time_updated": 1}}))
    _fake_proc(monkeypatch, cwd="/w")
    with pytest.raises(oc_session.ResolveError, match="different conversation"):
        oc_session.resolve(1234, expect_session="theirs")


# ---------------------------------------------------------------------------
# The serve writer
# ---------------------------------------------------------------------------

class FakeResp:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeFile:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _serve_lane(serve_url="http://127.0.0.1:4096", sid="s1"):
    return oc_session.OpencodeLane(session_id=sid, repl_pid=1234,
                                   serve_url=serve_url)


def _patch_net_tcp(monkeypatch, tcp):
    """Replace the real /proc/net/tcp with ``tcp`` text."""
    import builtins
    real_open = builtins.open

    def fake_open(path, *a, **kw):
        if path == "/proc/net/tcp":
            return FakeFile(tcp)
        return real_open(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", fake_open)


def _patch_os(monkeypatch, *, fds, links):
    """Point oc_session's os module at fake fd/readlink/proc helpers."""
    import types
    fake = types.SimpleNamespace(
        listdir=lambda _p: list(fds),
        readlink=lambda p: links.get(p.split("/")[-1], links.get(p, "")),
    )
    monkeypatch.setattr(oc_session, "os", fake)


class RecordingOpener:
    """An opener that records the POST body/URL, then returns a scripted resp."""

    def __init__(self, resp):
        self._resp = resp
        self.url = None
        self.body = None

    def __call__(self, url, timeout=None):
        self.url = url
        self.body = None
        return self._resp


def test_serve_commit_posts_the_text(monkeypatch):
    import urllib.request
    real_request = urllib.request.Request
    seen = {}

    def fake_request(url, data=None, method=None, headers=None):
        seen["url"] = url
        seen["method"] = method
        seen["data"] = data
        return real_request(url, data=data, method=method, headers=headers)

    monkeypatch.setattr(oc_inject.urllib.request, "Request", fake_request)
    with oc_inject.open_lane(_serve_lane(), opener=lambda u, *a, **kw: FakeResp(200)) as w:
        w.write("hello")
        w.commit()
    assert seen["url"].endswith("/session/s1/message")
    assert seen["method"] == "POST"
    posted = json.loads(seen["data"])
    assert posted == {"parts": [{"type": "text", "text": "hello"}]}


def test_serve_commit_surfaces_http_errors():
    with pytest.raises(oc_inject.ServeError, match="HTTP 500"):
        with oc_inject.open_lane(_serve_lane(), opener=lambda u, *a, **kw: FakeResp(500)) as w:
            w.write("hi")
            w.commit()


def test_serve_lane_offers_no_screen_and_answers_the_rejection_check():
    # There was never a screen here; there is no longer one anywhere. What the
    # sender does call between the write and the commit is the rejection check,
    # and on this lane it has nothing to report — the POST *is* the write, so a
    # refusal surfaces as its HTTP status instead.
    with oc_inject.open_lane(_serve_lane(), opener=lambda u, *a, **kw: FakeResp(200)) as w:
        assert not hasattr(w, "read_back")
        assert not hasattr(w, "read_back_is_evidence")
        w.check_not_rejected()


# ---------------------------------------------------------------------------
# DB observation
# ---------------------------------------------------------------------------

def test_read_status_busy_when_a_tool_is_running(tmp_path, monkeypatch):
    _use_db(monkeypatch, _mkdb(
        tmp_path, {"s1": {"directory": "/w", "time_updated": 5}},
        parts=[("s1", 1, json.dumps({"type": "tool",
                                     "state": {"status": "running"}}))]))
    _fake_proc(monkeypatch, cwd="/w")
    status, updated = oc_observe.read_status(1234)
    assert status == "busy" and updated == 5


def test_read_status_idle_when_no_turn_is_in_flight(tmp_path, monkeypatch):
    _use_db(monkeypatch, _mkdb(
        tmp_path, {"s1": {"directory": "/w", "time_updated": 3}}))
    _fake_proc(monkeypatch, cwd="/w")
    status, _ = oc_observe.read_status(1234)
    assert status == "idle"


def test_tail_watches_text_and_compaction(tmp_path, monkeypatch):
    _use_db(monkeypatch, _mkdb(
        tmp_path, {"s1": {"directory": "/w", "time_updated": 1}},
        parts=[("s1", 1, json.dumps({"type": "text", "text": "resume"})),
               ("s1", 2, json.dumps({"type": "compaction"}))]))
    _fake_proc(monkeypatch, cwd="/w")
    tail = oc_observe.OpencodeTail(1234)
    tail.watch("resume")
    assert tail.poll() is True
    assert tail.started("resume") is True
    assert tail.landed("resume") is True
    assert tail.compacted() is True
    assert tail.queued("resume") is None, "opencode has no observable queue"


def test_open_lane_refuses_a_lane_with_no_transport():
    lane = oc_session.OpencodeLane(session_id="s1", repl_pid=1234)
    with pytest.raises(oc_inject.OpencodeError, match="neither a pane nor"):
        with oc_inject.open_lane(lane):
            pass

# ---------------------------------------------------------------------------
# Which observation a pid gets, and whether the resume keeps it
# ---------------------------------------------------------------------------

def test_a_claude_pid_gets_the_claude_observation(tmp_path, monkeypatch):
    from awm.reflection import observation, session_target
    monkeypatch.setattr(session_target, "SESSIONS_DIR", tmp_path)
    (tmp_path / "4242.json").write_text("{}")
    assert isinstance(observation.observation_for(4242),
                      observation.ClaudeObservation)


def test_an_opencode_pid_gets_the_opencode_observation(tmp_path, monkeypatch):
    # The dispatch that had no coverage at all. It matters more now than it did:
    # the reader it picks is the arbiter, and picking the Claude one for an
    # opencode pid means reading a per-pid record file that does not exist.
    from awm.reflection import observation, session_target
    monkeypatch.setattr(session_target, "SESSIONS_DIR", tmp_path / "empty")
    monkeypatch.setattr(oc_session, "_is_opencode", lambda pid: True)
    _use_db(monkeypatch, _mkdb(
        tmp_path, {"s1": {"directory": "/w", "time_updated": 1}}))
    _fake_proc(monkeypatch, cwd="/w")
    assert isinstance(observation.observation_for(1234),
                      oc_observe.OpencodeObservation)


def test_a_pid_that_is_neither_falls_back_to_claude(tmp_path, monkeypatch):
    # Every fake pid in every other test file is this case, and it must keep
    # resolving to the module functions the rest of the suite pins.
    from awm.reflection import observation, session_target
    monkeypatch.setattr(session_target, "SESSIONS_DIR", tmp_path / "empty")
    monkeypatch.setattr(oc_session, "_is_opencode", lambda pid: False)
    assert isinstance(observation.observation_for(9999),
                      observation.ClaudeObservation)


def test_the_deferred_resume_keeps_the_reader_send_chose(monkeypatch):
    # `send` builds a harness-aware reader and hands it to `deliver` as an
    # explicit keyword, so it never rode in `**kw` — and the resume, which only
    # forwarded `kw` and a tail, silently fell back to Claude Code's per-pid
    # record for an opencode session. The screen gate used to mask that; with
    # confirmation the sole arbiter it is the difference between an arbiter and
    # none.
    from awm.reflection import inject

    def reader(_pid):
        return ("busy", 1)

    seen = {}

    def fake_await(item, **kw):
        seen.update(kw)

    monkeypatch.setattr(inject, "_await_and_resume", fake_await)
    inject.resume_watch(object(), spawn=lambda fn: fn(), tail="the-tail",
                        read_status=reader)
    assert seen["read_status"] is reader
    assert seen["tail"] == "the-tail"


def test_a_resume_given_only_one_of_the_pair_rebuilds_the_other(monkeypatch):
    # The rebuild used to be guarded on `tail is None` alone, so a caller that
    # passed a tail and no reader got the Claude reader whatever harness it was
    # watching.
    from awm.reflection import inject, observation, pending, stop_gate, watcher

    class Obs:
        def read_status(self, pid): return ("idle", 1)
        def open_tail(self, pid): raise AssertionError("the tail was supplied")

    class T:
        def watch(self, _t): pass

    monkeypatch.setattr(observation, "observation_for", lambda pid: Obs())
    monkeypatch.setattr(stop_gate, "rearm", lambda sid: None)
    got = {}
    monkeypatch.setattr(inject, "_await_and_resume_inner",
                        lambda item, tail, who, **kw: got.update(kw))
    item = pending.Pending(repl_pid=1, proc_start="1", session_id="s",
                           text="/compact", followup="resume",
                           injected_at_ms=0, name="n", hosting="tmux")
    inject._await_and_resume(item, tail=T())
    assert got["read_status"] is not watcher.read_status
