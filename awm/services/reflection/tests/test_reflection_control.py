"""Session control: resolving `job`, the keystrokes each verb sends, and when it refuses.

The screen, the typing and the clock are fakes; the roster, job records and
session records are real files in a temporary home.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from awm.reflection import control, guards, inject, session_target

SELF_JOB, OTHER_JOB, DEAD_JOB = "aaaaaaaa", "bbbbbbbb", "cccccccc"
ME, PARENT = os.getpid(), os.getppid()
DEAD_PID = 4_190_001


def proc_start(pid):
    with open(f"/proc/{pid}/stat") as fh:
        return fh.read().rpartition(") ")[2].split()[19]


@pytest.fixture
def home(tmp_path, monkeypatch):
    (tmp_path / "jobs").mkdir()
    (tmp_path / "sessions").mkdir()
    roster = tmp_path / "roster.json"
    monkeypatch.setattr(session_target, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(session_target, "ROSTER_PATH", roster)
    monkeypatch.setattr(session_target, "JOBS_DIR", tmp_path / "jobs")
    h = SimpleNamespace(root=tmp_path, roster=roster, workers={})

    def add_job(short, name, pid, *, alive=True, **state):
        h.workers[short] = {"replPid": pid, "sessionId": f"sid-{short}",
                            "replProcStart": proc_start(pid) if alive else "1"}
        roster.write_text(json.dumps({"workers": h.workers}))
        (tmp_path / "jobs" / short).mkdir()
        (tmp_path / "jobs" / short / "state.json").write_text(
            json.dumps({"name": name, **state}))
        if alive:
            record(pid, "bg", name, jobId=short)

    def record(pid, kind, name, **extra):
        (tmp_path / "sessions" / f"{pid}.json").write_text(json.dumps({
            "pid": pid, "kind": kind, "name": name, "sessionId": f"sid-{pid}",
            "procStart": proc_start(pid), **extra}))

    def set_bridge(short, bridge):
        path = tmp_path / "jobs" / short / "state.json"
        rec = json.loads(path.read_text())
        rec["bridgeSessionId"] = bridge
        path.write_text(json.dumps(rec))

    h.add_job, h.record, h.set_bridge = add_job, record, set_bridge
    return h


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += s


class Screen:
    """A scripted screen. `frames` is read in turn, the last one repeating."""

    def __init__(self, *frames, on_press=None):
        self.frames = list(frames)
        self.presses = []
        self.closed = False
        self.on_press = on_press

    def read(self):
        frame = self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]
        return frame() if callable(frame) else frame

    def forget(self):
        pass

    def press(self, key):
        self.presses.append(key)
        if self.on_press:
            self.on_press(key)

    def close(self):
        self.closed = True


PLAIN = "some output\n? for shortcuts"
PROMPT = "Do you want to proceed?\n❯ 1. Yes\n  2. No\nEsc to cancel"
OPTIONS = ["Show QR code", "Disconnect this session", "Continue"]


class Modal:
    """The Remote Control modal: a list with a cursor that Down and Up move."""

    def __init__(self, options=OPTIONS, cursor=0):
        self.options, self.cursor = options, cursor
        self.screen = Screen(self.render, on_press=self.move)

    def render(self):
        return "Remote Control\n" + "\n".join(
            ("❯ " if i == self.cursor else "  ") + o
            for i, o in enumerate(self.options)) + "\nEnter to select"

    def move(self, key):
        step = {"Down": 1, "Up": -1}.get(key, 0)
        self.cursor = (self.cursor + step) % len(self.options)


@pytest.fixture
def world(home, monkeypatch):
    """Fakes for the screen, the typing and the clock, with a log of what was typed."""
    clock = Clock()
    monkeypatch.setattr(control, "_sleep", clock.sleep)
    monkeypatch.setattr(control, "_clock", clock)
    w = SimpleNamespace(home=home, clock=clock, typed=[], sent=[], screen=Screen(PLAIN),
                        status="idle", settled=True, opened=[])

    def open_screen(t):
        w.opened.append(t)
        return w.screen

    def deliver(t, text, *, what, attempts=inject.ATTEMPTS):
        w.typed.append((t.job or t.pid, text, attempts))
        return inject.Delivery(True, SimpleNamespace(hosting="background"), "record")

    def send(text, **kw):
        w.sent.append((text, kw))
        return {"ok": True, "submitted": True, "text": text}

    monkeypatch.setattr(control, "_open_screen", open_screen)
    monkeypatch.setattr(control, "_deliver", deliver)
    monkeypatch.setattr(control, "_status", lambda t: w.status)
    monkeypatch.setattr(control, "_between_turns", lambda t: w.settled)
    monkeypatch.setattr(inject, "send", send)
    home.add_job(SELF_JOB, "me", ME)
    home.add_job(OTHER_JOB, "other one", PARENT)
    return w


# --- resolving `job` ---------------------------------------------------------


def test_without_job_the_target_is_the_caller(world):
    t = control.resolve(None, ME)
    assert t.is_self and t.job == SELF_JOB and t.kind == "bg"


def test_without_job_or_caller_there_is_nothing_to_act_on(world):
    with pytest.raises(session_target.ResolveError, match="which session"):
        control.resolve(None, None)


def test_job_resolves_by_id_and_by_name(world):
    assert control.resolve(OTHER_JOB, ME).pid == PARENT
    t = control.resolve("other one", ME)
    assert t.job == OTHER_JOB and not t.is_self and t.alive
    assert control.resolve("OTHER ONE", ME).job == OTHER_JOB


def test_a_job_naming_the_callers_own_session_is_marked_self(world):
    assert control.resolve(SELF_JOB, ME).is_self
    assert control.resolve("me", ME).is_self
    assert not control.resolve("me", None).is_self


def test_an_ambiguous_name_is_refused_with_the_candidates(world):
    world.home.add_job(DEAD_JOB, "other one", DEAD_PID, alive=False)
    with pytest.raises(session_target.ResolveError) as exc:
        control.resolve("other one", ME)
    text = str(exc.value)
    assert OTHER_JOB in text and DEAD_JOB in text and "job id" in text


def test_an_unknown_name_is_refused(world):
    with pytest.raises(session_target.ResolveError, match="no session"):
        control.resolve("nobody", ME)


def test_an_interactive_session_resolves_by_name_and_has_no_job(world):
    other = [p for p in (1,) if os.path.exists(f"/proc/{p}")][0]
    world.home.record(other, "interactive", "terminal one")
    t = control.resolve("terminal one", ME)
    assert t.kind == "interactive" and t.job is None and t.pid == other


def test_a_dead_session_resolves_but_cannot_be_typed_into(world):
    world.home.add_job(DEAD_JOB, "gone", DEAD_PID, alive=False)
    t = control.resolve("gone", ME)
    assert not t.alive
    with pytest.raises(session_target.ResolveError, match="not running"):
        control.model("opus", job="gone", caller_pid=ME)
    assert world.typed == []


# --- link --------------------------------------------------------------------


def test_link_is_the_app_url_of_the_bridge_id(world):
    world.home.set_bridge(OTHER_JOB, "cse_01Abc")
    out = control.link(job="other one", caller_pid=ME)
    assert out["ok"] and out["remote_control"] is True
    assert out["link"] == "https://claude.ai/code/session_01Abc"
    assert out["job"] == OTHER_JOB and world.typed == [] and world.opened == []


def test_link_is_null_with_a_reason_when_remote_control_is_off(world):
    out = control.link(job=None, caller_pid=ME)
    assert out["remote_control"] is False and out["link"] is None
    assert "off" in out["link_reason"] and out["self"] is True


def test_link_reads_an_interactive_sessions_own_record(world):
    world.home.record(1, "interactive", "term", bridgeSessionId="cse_01T")
    out = control.link(job="term", caller_pid=ME)
    assert out["link"] == "https://claude.ai/code/session_01T"


# --- model, effort, rename ---------------------------------------------------


@pytest.mark.parametrize("verb,arg,text", [
    (control.model, "opus", "/model opus"),
    (control.effort, "xhigh", "/effort xhigh"),
    (control.rename, "  new name ", "/rename new name"),
])
def test_a_slash_verb_types_its_command_into_the_named_session(world, verb, arg, text):
    out = verb(arg, job=OTHER_JOB, caller_pid=ME)
    assert out["ok"] and out["submitted"] and out["text"] == text
    assert world.typed == [(OTHER_JOB, text, inject.ATTEMPTS)]
    assert world.sent == []


def test_a_slash_verb_without_job_goes_through_the_self_sender(world):
    out = control.model("sonnet[1m]", job=None, caller_pid=ME)
    assert out["ok"] and out["self"] is True
    assert world.sent[0][0] == "/model sonnet[1m]"
    assert world.sent[0][1]["caller_pid"] == ME and world.typed == []


@pytest.mark.parametrize("call", [
    lambda: control.model("--evil", job=OTHER_JOB, caller_pid=ME),
    lambda: control.model("a b", job=OTHER_JOB, caller_pid=ME),
    lambda: control.effort("ludicrous", job=OTHER_JOB, caller_pid=ME),
    lambda: control.rename("", job=OTHER_JOB, caller_pid=ME),
    lambda: control.rename("two\nlines", job=OTHER_JOB, caller_pid=ME),
    lambda: control.rename("-x", job=OTHER_JOB, caller_pid=ME),
    lambda: control.rename("<warm x>", job=OTHER_JOB, caller_pid=ME),
])
def test_a_bad_argument_types_nothing(world, call):
    assert call()["ok"] is False
    assert world.typed == [] and world.sent == [] and world.opened == []


@pytest.mark.parametrize("status,screen", [("waiting", PLAIN), ("idle", PROMPT)])
def test_a_modal_or_prompt_stops_every_typing_verb(world, status, screen):
    world.status, world.screen = status, Screen(screen)
    for out in (control.model("opus", job=OTHER_JOB, caller_pid=ME),
                control.effort("low", job=OTHER_JOB, caller_pid=ME),
                control.rename("x", job=OTHER_JOB, caller_pid=ME),
                control.message(OTHER_JOB, "hello", caller_pid=ME),
                control.interrupt(job=OTHER_JOB, caller_pid=ME),
                control.remote_control(True, job=OTHER_JOB, caller_pid=ME)):
        assert out["ok"] is False and out["refused"] and out["kind"] == "modal"
    assert world.typed == [] and world.screen.presses == [] and world.sent == []
    assert world.screen.closed


# --- interrupt ---------------------------------------------------------------


def test_interrupt_presses_escape_once(world):
    world.status = "busy"
    out = control.interrupt(job="other one", caller_pid=ME)
    assert out["ok"] and out["status_before"] == "busy"
    assert world.screen.presses == ["Escape"] and world.typed == []
    assert world.screen.closed


# --- message -----------------------------------------------------------------


def test_message_types_the_text_into_the_other_session(world):
    out = control.message("other one", "please look at X", caller_pid=ME)
    assert out["ok"] and world.typed == [(OTHER_JOB, "please look at X", inject.ATTEMPTS)]


def test_message_has_the_guards_message_self_has(world):
    destructive = control.message(OTHER_JOB, "/clear", caller_pid=ME)
    assert destructive["refused"] and "confirm" in destructive["reason"]
    modal = control.message(OTHER_JOB, "/mcp", caller_pid=ME)
    assert modal["refused"] and modal["kind"] == "interactive"
    assert world.typed == []
    assert control.message(OTHER_JOB, "/clear", caller_pid=ME, confirm=True)["ok"]


def test_message_needs_a_target_and_text(world):
    assert control.message(None, "x", caller_pid=ME)["ok"] is False
    with pytest.raises(ValueError):
        control.message(OTHER_JOB, "  ", caller_pid=ME)


def test_message_to_ones_own_session_is_a_self_send(world):
    out = control.message("me", "note to self", caller_pid=ME)
    assert out["ok"] and world.sent[0][0] == "note to self" and world.typed == []


# --- stop --------------------------------------------------------------------


class Remover:
    def __init__(self, removed=True):
        self.calls, self.removed = [], removed

    def __call__(self, job):
        self.calls.append(job)
        return {"stopped": True, "removed": self.removed,
                "detail": "" if self.removed else "no such job"}


def test_stop_removes_another_session(world):
    rm = Remover()
    out = control.stop("other one", caller_pid=ME, remove=rm)
    assert out["ok"] and out["job"] == OTHER_JOB and rm.calls == [OTHER_JOB]


def test_stop_reaches_a_session_start_other_did_not_create(world):
    rm = Remover()
    assert control.stop(OTHER_JOB, caller_pid=ME, remove=rm)["ok"]


def test_stop_refuses_the_callers_own_session_without_confirm(world):
    rm, spawned = Remover(), []
    out = control.stop("me", caller_pid=ME, remove=rm, spawn=spawned.append)
    assert out["ok"] is False and out["refused"] and out["kind"] == "self"
    assert rm.calls == [] and spawned == []


def test_stop_of_own_session_with_confirm_runs_after_the_call_returns(world):
    rm, spawned = Remover(), []
    out = control.stop(SELF_JOB, caller_pid=ME, confirm=True, remove=rm,
                       spawn=spawned.append)
    assert out["ok"] and out["stopping"] and rm.calls == []
    spawned[0]()
    assert rm.calls == [SELF_JOB]


def test_stop_reports_a_failed_removal(world):
    out = control.stop(OTHER_JOB, caller_pid=ME, remove=Remover(removed=False))
    assert out["ok"] is False and out["reason"] == "no such job"


def test_stop_removes_a_job_that_is_already_dead(world):
    world.home.add_job(DEAD_JOB, "gone", DEAD_PID, alive=False)
    rm = Remover()
    assert control.stop("gone", caller_pid=ME, remove=rm)["ok"]
    assert rm.calls == [DEAD_JOB]


def test_stop_refuses_a_terminal_session_and_a_missing_job(world):
    world.home.record(1, "interactive", "term")
    rm = Remover()
    assert control.stop("term", caller_pid=ME, remove=rm)["refused"]
    assert control.stop(None, caller_pid=ME, remove=rm)["ok"] is False
    assert rm.calls == []


# --- Remote Control: on ------------------------------------------------------


def test_on_types_rc_with_the_name_and_returns_the_link_when_it_appears(world):
    polls = []
    real = control._bridge_id

    def bridge(t):
        polls.append(1)
        if len(polls) == 4:
            world.home.set_bridge(OTHER_JOB, "cse_01New")
        return real(t)

    control._bridge_id = bridge
    try:
        out = control.remote_control(True, name="phone-1", job="other one",
                                     caller_pid=ME)
    finally:
        control._bridge_id = real
    assert out["ok"] and out["link"] == "https://claude.ai/code/session_01New"
    assert world.typed == [(OTHER_JOB, "/rc phone-1", 1)], "one attempt, no retry"
    assert "link_reason" not in out


def test_on_without_a_name_types_a_bare_rc(world):
    control.remote_control(True, job=OTHER_JOB, caller_pid=ME)
    assert world.typed[0][1] == "/rc"


def test_on_returns_a_null_link_with_a_reason_when_the_bridge_never_appears(world):
    out = control.remote_control(True, job=OTHER_JOB, caller_pid=ME)
    assert out["ok"] and out["link"] is None and out["remote_control"] is False
    assert "no bridge id" in out["link_reason"]
    assert world.clock.now >= control.LINK_WAIT_S


def test_on_when_already_on_types_nothing_and_returns_the_link(world):
    world.home.set_bridge(OTHER_JOB, "cse_01On")
    out = control.remote_control(True, job=OTHER_JOB, caller_pid=ME)
    assert out["ok"] and out["changed"] is False
    assert out["link"] == "https://claude.ai/code/session_01On"
    assert world.typed == []


def test_on_rejects_a_name_that_could_be_a_flag(world):
    assert control.remote_control(True, name="--x", job=OTHER_JOB, caller_pid=ME)["ok"] is False
    assert world.typed == []


def test_on_for_the_callers_own_session_queues_and_resumes_with_a_pointer_to_link(world):
    out = control.remote_control(True, name="me-rc", job=None, caller_pid=ME)
    text, kw = world.sent[0]
    assert text == "/rc me-rc" and kw["allow_modal"] is True
    assert kw["followup"] == control.SELF_ON_FOLLOWUP and "link" in kw["followup"]
    assert out["link"] is None and "turn ends" in out["link_reason"]


# --- Remote Control: off -----------------------------------------------------


@pytest.fixture
def connected(world):
    world.home.set_bridge(OTHER_JOB, "cse_01On")
    return world


def test_off_when_already_off_types_nothing(world):
    out = control.remote_control(False, job=OTHER_JOB, caller_pid=ME)
    assert out["ok"] and out["changed"] is False and world.typed == []


def drive(world, modal, *, then_clear=True):
    """Run `off` with the pre-check clean and the modal appearing after `/rc`."""
    precheck = Screen(PLAIN)
    world.screen = precheck
    original = control._deliver

    def deliver(t, text, **kw):
        world.screen = modal.screen
        return original(t, text, **kw)

    def on_press(key):
        modal.move(key)
        if key == "Enter" and then_clear:
            world.home.set_bridge(OTHER_JOB, "")

    modal.screen.on_press = on_press
    control._deliver = deliver
    try:
        return control.remote_control(False, job=OTHER_JOB, caller_pid=ME)
    finally:
        control._deliver = original


def test_off_types_rc_and_selects_disconnect_in_the_modal(connected):
    modal = Modal(cursor=0)
    out = drive(connected, modal)
    assert out["ok"] and out["bridge_cleared"] and out["remote_control"] is False
    assert connected.typed == [(OTHER_JOB, "/rc", 1)]
    assert modal.screen.presses == ["Down", "Enter"], "Show QR -> Disconnect"


def test_off_walks_up_when_the_cursor_is_below_the_option(connected):
    modal = Modal(options=["Disconnect this session", "Show QR code", "Continue"],
                  cursor=2)
    out = drive(connected, modal)
    assert out["ok"]
    assert modal.screen.presses[-1] == "Enter"
    assert set(modal.screen.presses[:-1]) <= {"Up"}


def test_off_presses_enter_at_once_when_disconnect_is_already_selected(connected):
    modal = Modal(cursor=1)
    out = drive(connected, modal)
    assert out["ok"] and modal.screen.presses == ["Enter"]


def test_off_reports_when_the_bridge_id_does_not_clear(connected):
    modal = Modal(cursor=1)
    out = drive(connected, modal, then_clear=False)
    assert out["ok"] is False and out["bridge_cleared"] is False
    assert out["remote_control"] is True
    assert connected.clock.now >= control.CLEAR_WAIT_S


def test_off_refuses_a_modal_that_is_not_the_expected_one(connected):
    modal = Modal(options=["Yes", "No"], cursor=0)
    out = drive(connected, modal)
    assert out["ok"] is False and out["kind"] == "unexpected_modal"
    assert modal.screen.presses == ["Escape"], "closed, and Enter never pressed"


def test_off_refuses_a_modal_with_no_cursor_to_steer_by(connected):
    modal = Modal(cursor=0)
    modal.screen.frames = [
        "Remote Control\n  Show QR code\n  Disconnect this session\nEnter to select"]
    out = drive(connected, modal)
    assert out["ok"] is False and modal.screen.presses == ["Escape"]


def test_off_gives_up_without_pressing_when_no_modal_appears(connected):
    out = drive(connected, SimpleNamespace(
        screen=Screen(PLAIN), move=lambda k: None))
    assert out["ok"] is False and "did not appear" in out["reason"]
    assert connected.screen.presses == []
    assert connected.clock.now >= control.MODAL_WAIT_S


def test_off_refuses_a_session_that_is_mid_turn(connected):
    connected.settled = False
    out = control.remote_control(False, job=OTHER_JOB, caller_pid=ME)
    assert out["ok"] is False and out["refused"] and "mid-turn" in out["reason"]
    assert connected.typed == []


def test_off_for_the_callers_own_session_queues_rc_and_finishes_later(connected):
    connected.home.set_bridge(SELF_JOB, "cse_01Me")
    spawned = []
    out = control.remote_control(False, job=None, caller_pid=ME, spawn=spawned.append)
    assert out["ok"] and out["remote_control"] is True
    assert connected.typed == [(SELF_JOB, "/rc", 1)] and len(spawned) == 1
    modal = Modal(cursor=1)
    connected.screen = modal.screen

    def on_press(key):
        if key == "Enter":
            connected.home.set_bridge(SELF_JOB, "")

    modal.screen.on_press = on_press
    spawned[0]()
    assert modal.screen.presses == ["Enter"]
    assert connected.typed[-1] == (SELF_JOB, control.SELF_OFF_FOLLOWUP, inject.ATTEMPTS)


# --- the modal guard ---------------------------------------------------------


@pytest.mark.parametrize("text", ["/rc", "/rc name", "/remote-control"])
def test_rc_is_a_modal_command_for_free_text(text):
    assert guards.opens_modal(text)
    assert guards.refusal(text, confirm=False)["kind"] == "interactive"
    assert guards.refusal(text, confirm=False, allow_modal=True) is None


def test_allow_modal_does_not_lift_the_destructive_guard():
    assert guards.refusal("/clear", confirm=False, allow_modal=True)["refused"]


def test_modal_copy_is_recognised_through_escape_sequences():
    assert guards.modal_on_screen("x\x1b[1mEsc to cancel\x1b[0m")
    assert guards.modal_on_screen(PLAIN) is None


# --- the keys on the wire ----------------------------------------------------


def test_a_tmux_press_is_a_send_keys_of_the_named_key():
    from awm.reflection import permission_mode

    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    sess = permission_mode._TmuxSession(
        session_target.TmuxLane(pane="%3", session_id="s", repl_pid=1), runner=runner)
    sess.press("Escape")
    sess.press("Down")
    assert [c[-4:] for c in calls] == [["send-keys", "-t", "%3", "Escape"],
                                       ["send-keys", "-t", "%3", "Down"]]


def test_a_daemon_press_sends_the_raw_key_bytes():
    from awm.reflection import permission_mode

    sent = []
    sess = permission_mode._DaemonSession.__new__(permission_mode._DaemonSession)
    sess._conn = SimpleNamespace(send_keys=sent.append)
    for key in ("Escape", "Down", "Up", "Enter"):
        sess.press(key)
    assert sent == [b"\x1b", b"\x1b[B", b"\x1b[A", b"\r"]
