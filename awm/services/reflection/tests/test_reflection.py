"""The tmux lane, and the guards both lanes share.

Every tmux call goes through an injected runner, so none of this needs a real
tmux server. The runner is not a stub that records argv, though — it models a
pane with a prompt box you can paste into, clear, and submit, because the
transaction under test writes, reads the pane back, and only then presses Enter.
A recorder alone cannot express "the text never showed up".
"""
from __future__ import annotations

import subprocess

import pytest

from awm.reflection import guards, inject, session_target, tmux_inject


LANE = session_target.TmuxLane(pane="%7", session_id="sid-1", repl_pid=4242,
                               name="test")


@pytest.fixture(autouse=True)
def _agent_present(monkeypatch):
    """Default the agent-subtree sanity check to "yes" for every test.

    `_subtree_has_agent` walks the real `/proc`, which a unit test has no
    business depending on. The refusal path overrides this per-test.
    """
    monkeypatch.setattr(tmux_inject, "_subtree_has_agent", lambda pid, kids: True)
    monkeypatch.setattr(tmux_inject, "_ppid_children", dict)


class FakePane:
    """A tmux server holding one pane with a working prompt box."""

    def __init__(self, *, pane_pid="4242", session="sess0", list_panes=None,
                 returncode=0, swallow_paste=False, scrollback=""):
        self.calls: list[list[str]] = []
        self.buffers: dict[str, str] = {}
        self.prompt = ""
        self.submitted: list[str] = []
        self.scrollback = scrollback
        self._pane_pid = pane_pid
        self._session = session
        self._list_panes = list(list_panes or [])
        self._rc = returncode
        # Models a modal (or a session not reading its pty): the paste is
        # accepted by tmux and never reaches the prompt.
        self._swallow = swallow_paste

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        rest = argv[1:]
        if rest[:1] == ["-S"]:
            rest = rest[2:]
        verb = rest[0] if rest else ""
        stdout = ""
        if verb == "load-buffer":
            self.buffers[argv[argv.index("-b") + 1]] = \
                kw.get("input", b"").decode()
        elif verb == "paste-buffer":
            if not self._swallow:
                self.prompt += self.buffers.get(argv[argv.index("-b") + 1], "")
        elif verb == "send-keys":
            key = argv[-1]
            if key == "Enter":
                self.submitted.append(self.prompt)
                self.scrollback += f"❯ {self.prompt}\n"
                self.prompt = ""
            elif key == tmux_inject._CLEAR_KEY:
                self.prompt = ""
        elif verb == "capture-pane":
            stdout = f"{self.scrollback}❯ {self.prompt}"
        elif verb == "display-message":
            fmt = argv[-1]
            stdout = {"#{pane_pid}": self._pane_pid,
                      "#{session_name}": self._session}.get(
                          fmt, argv[argv.index("-t") + 1])
        elif verb == "list-panes":
            stdout = "\n".join("\t".join(str(f) for f in row)
                               for row in self._list_panes)
        return subprocess.CompletedProcess(argv, self._rc, stdout=stdout,
                                           stderr="boom")

    def verbs(self) -> list[str]:
        out = []
        for argv in self.calls:
            rest = argv[1:]
            if rest[:1] == ["-S"]:
                rest = rest[2:]
            out.append(rest[0] if rest else "")
        return out

    def flat(self) -> list[str]:
        return [tok for argv in self.calls for tok in argv]


def send(text, pane: FakePane, *, status=("idle", 1000), **kw):
    """Deliver through the real tmux writer, against a fake pane.

    ``status`` stands in for the session's own record, which is what the sender
    now confirms a submit against. The pane fake cannot write one, and the tmux
    lane has no more claim on that signal than the daemon lane does — the point
    of reading the record is that neither transport owns it.
    """
    moved = {"at": status[1]}

    def read_status(_pid):
        if status is None:
            return None
        out = (status[0], moved["at"])
        if pane.submitted:
            moved["at"] = status[1] + 1
        return out

    return inject.deliver(4242, text, detect=lambda _p: LANE, runner=pane,
                          sleep=lambda _s: None, read_status=read_status, **kw)


# ---------------------------------------------------------------------------
# The paste sequence
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_a_command_lands_in_the_prompt_and_is_submitted():
    pane = FakePane()
    result = send("/compact", pane)
    assert result.submitted is True
    assert result.lane is LANE
    assert result.confirmed == inject.CONFIRMED_RECORD
    assert pane.submitted == ["/compact"]


@pytest.mark.smoke
def test_the_paste_is_bracketed_and_targets_the_pane():
    # `-p` is load-bearing, not decoration: without bracketed paste a leading
    # `/` opens the TUI's slash menu instead of arriving as literal text.
    pane = FakePane()
    send("/compact", pane)
    paste = next(a for a in pane.calls if "paste-buffer" in a)
    assert "-p" in paste and "-d" in paste
    assert paste[paste.index("-t") + 1] == "%7"


@pytest.mark.smoke
def test_no_escape_is_ever_sent():
    # Escape would cancel the agent's in-flight turn. The whole design queues
    # behind that turn instead of interrupting it.
    pane = FakePane()
    send("/compact", pane)
    assert "Escape" not in pane.flat()


def test_the_text_goes_in_on_stdin_not_as_an_argument():
    # Command lines are visible to every process on the box, and `send` carries
    # the caller's own prompt content.
    pane = FakePane()
    send("/model opus", pane)
    load = next(a for a in pane.calls if "load-buffer" in a)
    assert load[-1] == "-"
    assert "/model opus" not in " ".join(load)


def test_enter_false_leaves_the_text_in_the_prompt_unsubmitted():
    pane = FakePane()
    result = send("half a thought", pane, enter=False)
    assert result.submitted is False
    assert pane.submitted == []
    assert pane.prompt == "half a thought"


# ---------------------------------------------------------------------------
# Verification, on this lane specifically
# ---------------------------------------------------------------------------

def test_a_swallowed_paste_is_not_reported_as_sent():
    # tmux accepted every call and the prompt stayed empty — a modal ate it.
    pane = FakePane(swallow_paste=True)
    with pytest.raises(inject.DeliveryError):
        send("/compact", pane)
    assert pane.submitted == []


def test_an_earlier_compaction_in_the_scrollback_does_not_fake_a_success():
    # `capture-pane` hands back the visible screen, which still holds the last
    # time this session compacted. Verification counts occurrences for exactly
    # this reason.
    pane = FakePane(swallow_paste=True,
                    scrollback="❯ /compact\n⎿ Compacted (ctrl+o …)\n")
    with pytest.raises(inject.DeliveryError):
        send("/compact", pane)


def test_a_retry_wipes_the_prompt_with_ctrl_u():
    pane = FakePane(swallow_paste=True)
    with pytest.raises(inject.DeliveryError):
        send("/compact", pane)
    assert pane.flat().count(tmux_inject._CLEAR_KEY) == 3, \
        "attempts 2 and 3 wipe on the way in; the give-up wipes on the way out"


# ---------------------------------------------------------------------------
# Refusing a pane that is not what it was
# ---------------------------------------------------------------------------

def test_a_pane_running_no_agent_is_refused(monkeypatch):
    # The pane id is real and exists, but nothing running there is an agent — a
    # stale id now repointed at a shell or an editor.
    monkeypatch.setattr(tmux_inject, "_subtree_has_agent", lambda pid, kids: False)
    pane = FakePane()
    with pytest.raises(inject.DeliveryError):
        send("/compact", pane)
    assert pane.submitted == []


def test_a_dead_pane_is_refused():
    pane = FakePane(returncode=1)
    with pytest.raises(inject.DeliveryError):
        send("/compact", pane)


def test_the_pane_is_re_checked_on_every_attempt():
    # The checks belong to the attempt, not to some earlier resolution: a pane
    # can be destroyed between detecting it and writing to it.
    pane = FakePane(swallow_paste=True)
    with pytest.raises(inject.DeliveryError):
        send("/compact", pane)
    assert pane.verbs().count("display-message") == 8, \
        "two assertions per attempt, three attempts, plus the give-up wipe"


# ---------------------------------------------------------------------------
# Pane discovery
# ---------------------------------------------------------------------------

def test_a_pane_is_found_by_containing_the_caller(monkeypatch):
    monkeypatch.setattr(tmux_inject, "_ppid_children",
                        lambda: {100: [200], 200: [4242]})
    pane = FakePane(list_panes=[("%1", "999", "bash", "s"),
                                ("%7", "100", "claude", "s")])
    assert tmux_inject.pane_for_pid(4242, runner=pane) == "%7"


def test_a_caller_in_no_pane_resolves_to_nothing(monkeypatch):
    monkeypatch.setattr(tmux_inject, "_ppid_children", lambda: {100: [200]})
    pane = FakePane(list_panes=[("%1", "100", "claude", "s")])
    assert tmux_inject.pane_for_pid(4242, runner=pane) is None


def test_panes_are_never_ranked_by_recency():
    # An earlier version asked tmux for `#{pane_activity}`, which does not exist
    # — it expanded to empty for every pane, so they all ranked identically and
    # every deferred resume went to the lowest-numbered agent pane. The caller's
    # identity says which pane is theirs; nothing else gets a vote.
    pane = FakePane(list_panes=[("%1", "100", "claude", "s")])
    tmux_inject.list_panes(runner=pane)
    fmt = next(a for a in pane.calls if "list-panes" in a)[-1]
    assert "activity" not in fmt


# ---------------------------------------------------------------------------
# Guards — enforced above both lanes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", ["/mcp", "/status", "/config", "/resume"])
def test_a_modal_command_is_refused(cmd):
    # These trap pasted input and Enter; a follow-up does not escape them and a
    # navigable list drills deeper on Enter. Only a hand-typed Esc recovers.
    res = inject.send(cmd, caller_pid=4242)
    assert res["refused"] is True and res["kind"] == "interactive"


def test_a_modal_command_is_not_overridable_by_confirm():
    assert inject.send("/mcp", caller_pid=4242, confirm=True)["refused"] is True


def test_bare_model_is_refused_but_model_with_an_argument_is_not():
    # `/model` alone opens a chooser; `/model opus` acts immediately.
    assert inject.send("/model", caller_pid=4242)["refused"] is True
    pane = FakePane()
    assert send("/model opus", pane)[0] is True


@pytest.mark.parametrize("cmd", ["/clear", "/quit", "/exit"])
def test_a_destructive_command_needs_confirmation(cmd):
    assert inject.send(cmd, caller_pid=4242)["refused"] is True


def test_a_destructive_command_goes_through_with_confirmation():
    pane = FakePane()
    assert send("/clear", pane, confirm=True).submitted is True


def test_empty_text_is_a_caller_bug_not_a_refusal():
    with pytest.raises(ValueError):
        inject.send("   ", caller_pid=4242)


def test_a_caller_that_cannot_be_identified_is_refused():
    # Reflection acts on the caller and on nobody else, so an unresolvable
    # caller is refused rather than served with someone else's session.
    with pytest.raises(session_target.ResolveError):
        inject.send("/compact", caller_pid=None)


def test_the_guard_tables_are_shared_by_both_lanes():
    # They are properties of the agent TUI, not of how a keystroke reaches it.
    # It would be a nasty surprise if `/clear` were guarded on one lane only.
    assert guards.refusal("/clear", confirm=False) is not None
    assert guards.refusal("/clear", confirm=True) is None
