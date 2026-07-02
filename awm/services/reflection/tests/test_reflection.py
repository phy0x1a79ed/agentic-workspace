"""Unit tests for the reflection self-injection primitive.

All tmux calls go through an injected fake runner, so these exercise the argv
assembly + guard logic without a real tmux server. The follow-up prompt is
*deferred* (a detached watcher injects it only after the slash command has
visibly finished), so `send()` schedules the watcher via an injectable ``spawn``
seam and the completion logic is tested directly through ``_await_and_followup``.
"""
import logging
import subprocess

import pytest

from awm.reflection import tmux_inject


def _noop_spawn(fn):
    """Drop the scheduled watcher — most tests only inspect synchronous output."""
    return None


class FakeRunner:
    def __init__(self, returncode: int = 0, captures=None):
        self.calls: list[tuple[list, dict]] = []
        self.returncode = returncode
        # Scripted `capture-pane -p` snapshots served in order (last one repeats).
        self._captures = list(captures) if captures else []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if "display-message" in argv:
            stdout = "%32"
        elif "capture-pane" in argv:
            stdout = self._next_capture()
        else:
            stdout = ""
        return subprocess.CompletedProcess(argv, self.returncode,
                                           stdout=stdout, stderr="boom")

    def _next_capture(self) -> str:
        if not self._captures:
            return ""
        if len(self._captures) == 1:
            return self._captures[0]
        return self._captures.pop(0)

    def verbs(self) -> list[str]:
        # The tmux subcommand is the token after the binary (and optional -S sock).
        out = []
        for argv, _ in self.calls:
            rest = argv[1:]
            if rest[:1] == ["-S"]:
                rest = rest[2:]
            out.append(rest[0] if rest else "")
        return out

    def flat(self) -> list[str]:
        return [tok for argv, _ in self.calls for tok in argv]


class FakeClock:
    """Monotonic-ish clock that advances ``step`` seconds per call."""

    def __init__(self, step: float = 100.0):
        self.t = 0.0
        self.step = step

    def __call__(self) -> float:
        v = self.t
        self.t += self.step
        return v


# ---------------------------------------------------------------------------
# send(): synchronous injection + deferred-follow-up scheduling
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_send_pastes_loads_and_submits():
    r = FakeRunner()
    scheduled = []
    res = tmux_inject.send("/compact", pane="%32", runner=r, spawn=scheduled.append)
    assert res == {"ok": True, "pane": "%32", "text": "/compact",
                   "submitted": True, "followup": tmux_inject.DEFAULT_FOLLOWUP,
                   "followup_deferred": True}
    # ONLY the command is injected synchronously: display-message (existence
    # check) → load → paste → send-keys. The resume is deferred to the watcher,
    # never co-queued behind /compact.
    assert r.verbs() == ["display-message",
                         "load-buffer", "paste-buffer", "send-keys"]
    assert len(scheduled) == 1


@pytest.mark.smoke
def test_bracketed_paste_targets_pane():
    r = FakeRunner()
    tmux_inject.send("/compact", pane="%7", runner=r, spawn=_noop_spawn)
    paste = next(argv for argv, _ in r.calls if "paste-buffer" in argv)
    assert "-p" in paste           # bracketed paste, so a leading / is literal
    assert "-t" in paste and "%7" in paste


@pytest.mark.smoke
def test_never_sends_escape():
    # Escape would interrupt the in-flight turn; the whole point is to queue.
    r = FakeRunner()
    tmux_inject.send("/compact", pane="%32", runner=r, spawn=_noop_spawn)
    assert "Escape" not in r.flat()


@pytest.mark.smoke
def test_load_buffer_gets_text_on_stdin():
    r = FakeRunner()
    tmux_inject.send("/model opus", pane="%32", runner=r, spawn=_noop_spawn)
    lb = next(kw for argv, kw in r.calls if "load-buffer" in argv)
    assert lb.get("input") == b"/model opus"


@pytest.mark.smoke
def test_slash_command_defers_followup():
    # A bare slash command leaves the session idle; a resume must follow — but it
    # is DEFERRED (scheduled), not pasted synchronously behind the command.
    r = FakeRunner()
    scheduled = []
    res = tmux_inject.send("/compact", pane="%32", runner=r, spawn=scheduled.append)
    assert res["followup"] == tmux_inject.DEFAULT_FOLLOWUP
    assert res["followup_deferred"] is True
    # Synchronously only /compact is loaded; the resume is not co-queued.
    loaded = [kw["input"] for argv, kw in r.calls if "load-buffer" in argv]
    assert loaded == [b"/compact"]
    assert r.verbs().count("send-keys") == 1
    assert len(scheduled) == 1


@pytest.mark.smoke
def test_custom_followup_used():
    r = FakeRunner()
    scheduled = []
    res = tmux_inject.send("/compact", pane="%32", followup="resume task 3",
                           runner=r, spawn=scheduled.append)
    assert res["followup"] == "resume task 3"
    assert res["followup_deferred"] is True
    # The custom text is carried on the result and handed to the watcher, not
    # pasted synchronously.
    loaded = [kw["input"] for argv, kw in r.calls if "load-buffer" in argv]
    assert loaded == [b"/compact"]
    assert len(scheduled) == 1


@pytest.mark.smoke
def test_plain_prompt_gets_no_followup():
    # A normal prompt is its own turn — no keep-alive needed, nothing scheduled.
    r = FakeRunner()
    scheduled = []
    res = tmux_inject.send("hello there", pane="%32", runner=r, spawn=scheduled.append)
    assert res["followup"] is None
    assert res["followup_deferred"] is False
    assert r.verbs().count("send-keys") == 1
    loaded = [kw["input"] for argv, kw in r.calls if "load-buffer" in argv]
    assert loaded == [b"hello there"]
    assert scheduled == []


# ---------------------------------------------------------------------------
# _pane_phase(): classify the TUI pane tail
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_pane_phase_variants():
    assert tmux_inject._pane_phase("Compacting conversation…") == "compacting"
    assert tmux_inject._pane_phase(
        "Compacted (ctrl+o to see full summary)\n❯ ") == "compacted"
    assert tmux_inject._pane_phase("❯ ") == "idle"
    assert tmux_inject._pane_phase("working… esc to interrupt") == "busy"


@pytest.mark.smoke
def test_pane_phase_busy_beats_stale_compacted():
    # An active turn whose scrollback still holds a prior compaction's marker must
    # classify as busy, not compacted — else the watcher fires on the old marker.
    snap = "Compacted (ctrl+o to see full summary)\n…\nthinking… esc to interrupt"
    assert tmux_inject._pane_phase(snap) == "busy"


# ---------------------------------------------------------------------------
# _await_and_followup(): inject the resume only after the command finishes
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_await_followup_fires_after_compacting_then_compacted():
    r = FakeRunner(captures=[
        "assistant working… esc to interrupt",          # turn still running
        "Compacting conversation…",                       # compaction underway
        "Compacted (ctrl+o to see full summary)\n❯ ",     # done + idle
    ])
    tmux_inject._await_and_followup(
        "/compact", "resume now", "%32",
        socket=None, runner=r, sleep=lambda _s: None, clock=FakeClock())
    # Resume injected exactly once, only after the busy→compacted transition.
    loaded = [kw["input"] for argv, kw in r.calls if "load-buffer" in argv]
    assert loaded == [b"resume now"]
    assert r.verbs().count("send-keys") == 1


@pytest.mark.smoke
def test_await_followup_ignores_idle_before_busy():
    # A brief idle sample *before* the command starts must not misfire the resume.
    r = FakeRunner(captures=[
        "❯ ",                                             # idle gap (pre-command)
        "assistant working… esc to interrupt",            # busy
        "Compacted (ctrl+o to see full summary)\n❯ ",     # done
    ])
    tmux_inject._await_and_followup(
        "/compact", "resume", "%32",
        socket=None, runner=r, sleep=lambda _s: None, clock=FakeClock())
    caps = [argv for argv, _ in r.calls if "capture-pane" in argv]
    assert len(caps) == 3               # waited through all three, didn't early-fire
    loaded = [kw["input"] for argv, kw in r.calls if "load-buffer" in argv]
    assert loaded == [b"resume"]


@pytest.mark.smoke
def test_await_followup_non_compact_busy_to_idle():
    # A non-/compact slash command (e.g. /model opus) resumes on a settled idle.
    r = FakeRunner(captures=[
        "switching model… esc to interrupt",
        "❯ ", "❯ ", "❯ ",       # a settled idle streak
    ])
    tmux_inject._await_and_followup(
        "/model opus", "resume", "%32",
        socket=None, runner=r, sleep=lambda _s: None, clock=FakeClock())
    loaded = [kw["input"] for argv, kw in r.calls if "load-buffer" in argv]
    assert loaded == [b"resume"]


@pytest.mark.smoke
def test_await_followup_noop_compact_settled_idle():
    # A no-op /compact ("Not enough messages to compact") produces no marker; the
    # resume must still fire, via a settled idle streak after the busy turn.
    r = FakeRunner(captures=[
        "assistant working… esc to interrupt",   # driving turn
        "❯ ", "❯ ", "❯ ",                         # settled idle (compact was a no-op)
    ])
    tmux_inject._await_and_followup(
        "/compact", "resume", "%32",
        socket=None, runner=r, sleep=lambda _s: None, clock=FakeClock())
    loaded = [kw["input"] for argv, kw in r.calls if "load-buffer" in argv]
    assert loaded == [b"resume"]


@pytest.mark.smoke
def test_await_followup_transient_idle_gap_no_early_fire():
    # A single idle sample between the turn ending and `Compacting conversation`
    # appearing must NOT fire the resume early — it fires only after `Compacted`.
    r = FakeRunner(captures=[
        "assistant working… esc to interrupt",    # driving turn (busy)
        "❯ ",                                      # transient gap (1 idle sample)
        "Compacting conversation…",                # compaction actually starts
        "Compacting conversation…",
        "Compacted (ctrl+o to see full summary)\n❯ ",
    ])
    tmux_inject._await_and_followup(
        "/compact", "resume", "%32",
        socket=None, runner=r, sleep=lambda _s: None, clock=FakeClock())
    # All five samples consumed — the single idle gap did not trip the streak.
    caps = [argv for argv, _ in r.calls if "capture-pane" in argv]
    assert len(caps) == 5
    loaded = [kw["input"] for argv, kw in r.calls if "load-buffer" in argv]
    assert loaded == [b"resume"]


@pytest.mark.smoke
def test_await_followup_timeout_injects_anyway(caplog):
    # Never-idle pane → the hard cap injects the resume anyway (resume beats hang).
    r = FakeRunner(captures=["stuck… esc to interrupt"])
    with caplog.at_level(logging.WARNING):
        tmux_inject._await_and_followup(
            "/compact", "resume", "%32",
            socket=None, runner=r, sleep=lambda _s: None, clock=FakeClock(step=100.0))
    loaded = [kw["input"] for argv, kw in r.calls if "load-buffer" in argv]
    assert loaded == [b"resume"]
    assert any("not observed" in rec.message for rec in caplog.records)


@pytest.mark.smoke
def test_await_followup_pane_vanishes_no_resume():
    # capture-pane failing (pane gone) aborts cleanly with no resume injected.
    r = FakeRunner(returncode=1)
    tmux_inject._await_and_followup(
        "/compact", "resume", "%32",
        socket=None, runner=r, sleep=lambda _s: None, clock=FakeClock())
    loaded = [kw["input"] for argv, kw in r.calls if "load-buffer" in argv]
    assert loaded == []


# ---------------------------------------------------------------------------
# Guards (unchanged behavior)
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_modal_command_refused():
    # /mcp opens a navigable modal that swallows input — refused, nothing pasted.
    for cmd in ("/mcp", "/status", "/config"):
        r = FakeRunner()
        res = tmux_inject.send(cmd, pane="%32", runner=r, spawn=_noop_spawn)
        assert res["ok"] is False and res["refused"] is True
        assert res["kind"] == "interactive"
        assert r.calls == []


@pytest.mark.smoke
def test_modal_not_overridable_by_confirm():
    # Unlike destructive commands, modal ones cannot be forced — they'd freeze.
    r = FakeRunner()
    res = tmux_inject.send("/mcp", pane="%32", confirm=True, runner=r, spawn=_noop_spawn)
    assert res["ok"] is False and res["kind"] == "interactive"
    assert r.calls == []


@pytest.mark.smoke
def test_bare_model_refused_but_model_arg_allowed():
    # `/model` alone opens the picker (modal); `/model opus` acts directly.
    r1 = FakeRunner()
    assert tmux_inject.send("/model", pane="%32", runner=r1,
                            spawn=_noop_spawn)["ok"] is False
    assert r1.calls == []

    r2 = FakeRunner()
    res = tmux_inject.send("/model opus", pane="%32", runner=r2, spawn=_noop_spawn)
    assert res["ok"] is True
    assert res["followup"] == tmux_inject.DEFAULT_FOLLOWUP   # still a slash cmd
    assert res["followup_deferred"] is True
    loaded = [kw["input"] for argv, kw in r2.calls if "load-buffer" in argv]
    assert loaded[0] == b"/model opus"


@pytest.mark.smoke
def test_destructive_refused_without_confirm():
    r = FakeRunner()
    res = tmux_inject.send("/clear", pane="%32", runner=r, spawn=_noop_spawn)
    assert res["ok"] is False and res["refused"] is True
    assert "/clear" in res["reason"]
    assert r.calls == []           # nothing was pasted


@pytest.mark.smoke
def test_destructive_allowed_with_confirm():
    r = FakeRunner()
    res = tmux_inject.send("/clear", pane="%32", confirm=True, runner=r, spawn=_noop_spawn)
    assert res["ok"] is True
    assert "send-keys" in r.verbs()


@pytest.mark.smoke
def test_enter_false_skips_submit():
    r = FakeRunner()
    scheduled = []
    res = tmux_inject.send("draft text", pane="%32", enter=False, runner=r,
                           spawn=scheduled.append)
    assert res["submitted"] is False
    assert "send-keys" not in r.verbs()
    assert scheduled == []          # nothing submitted → no follow-up scheduled


@pytest.mark.smoke
def test_socket_is_threaded():
    r = FakeRunner()
    tmux_inject.send("/compact", pane="%32", socket="/tmp/s", runner=r, spawn=_noop_spawn)
    assert all(argv[1:3] == ["-S", "/tmp/s"] for argv, _ in r.calls)


@pytest.mark.smoke
def test_empty_text_raises():
    with pytest.raises(ValueError):
        tmux_inject.send("   ", pane="%32", runner=FakeRunner())


@pytest.mark.smoke
def test_tmux_failure_raises_tmuxerror():
    with pytest.raises(tmux_inject.TmuxError):
        tmux_inject.send("/compact", pane="%32", runner=FakeRunner(returncode=1),
                         spawn=_noop_spawn)
