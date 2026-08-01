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


@pytest.fixture(autouse=True)
def _agent_present(monkeypatch):
    """Default the T3 agent-subtree sanity check to "yes" for every test.

    `_subtree_has_agent` walks the real `/proc`, which a unit test has no
    business depending on. Tests exercising the refusal path override this
    per-test via `monkeypatch.setattr(tmux_inject, "_subtree_has_agent", ...)`.
    """
    monkeypatch.setattr(tmux_inject, "_subtree_has_agent", lambda pid, kids: True)


class FakeRunner:
    def __init__(self, returncode: int = 0, captures=None,
                pane_pid: str = "4242", session: str = "sess0",
                reresolved_pane: str = "%32", list_panes=None):
        self.calls: list[tuple[list, dict]] = []
        self.returncode = returncode
        # Scripted `capture-pane -p` snapshots served in order (last one repeats).
        self._captures = list(captures) if captures else []
        self._pane_pid = pane_pid
        self._session = session
        # What a `display-message -t <session>` re-resolve query returns —
        # the session's pane id after a hop (T5).
        self._reresolved_pane = reresolved_pane
        # Rows for `list-panes -a`, as (pane, pid, command, session, activity)
        # tuples; each becomes one tab-separated stdout line.
        self._list_panes = list(list_panes) if list_panes else []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if "list-panes" in argv:
            stdout = "\n".join("\t".join(str(f) for f in row)
                               for row in self._list_panes)
        elif "display-message" in argv:
            fmt = argv[-1]
            if fmt == "#{pane_pid}":
                stdout = self._pane_pid
            elif fmt == "#{session_name}":
                stdout = self._session
            elif fmt == "#{pane_id}":
                # `-t <pane>` existence check echoes the pane; `-t <session>`
                # re-resolve returns wherever that session's current pane is.
                target = argv[argv.index("-t") + 1]
                stdout = self._reresolved_pane if target == self._session else target
            else:
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
    # check) → display-message (agent-subtree check) → load → paste →
    # send-keys → display-message (session captured for the deferred
    # watcher's pane-hop fallback). The resume itself is deferred to the
    # watcher, never co-queued behind /compact.
    assert r.verbs() == ["display-message", "display-message",
                         "load-buffer", "paste-buffer", "send-keys",
                         "display-message"]
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


# ---------------------------------------------------------------------------
# T3: refuse to inject into a pane with no agent in its process subtree
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_refuses_pane_with_no_agent(monkeypatch):
    # The pane exists (display-message succeeds) but nothing in its subtree is
    # claude/opencode — a stale id repointed at a shell, or a wrong explicit
    # `pane` argument. Must refuse, not silently paste into whatever is there.
    monkeypatch.setattr(tmux_inject, "_subtree_has_agent", lambda pid, kids: False)
    r = FakeRunner()
    with pytest.raises(tmux_inject.TmuxError, match="not running an agent"):
        tmux_inject.send("/compact", pane="%32", runner=r, spawn=_noop_spawn)
    # Nothing pasted once the agent-subtree check fails.
    assert "load-buffer" not in r.verbs()


@pytest.mark.smoke
def test_agent_present_allows_injection():
    # Sanity: the default fixture's "agent present" stub is exercised by every
    # other test in this file; this asserts it explicitly for the happy path.
    r = FakeRunner()
    res = tmux_inject.send("/compact", pane="%32", runner=r, spawn=_noop_spawn)
    assert res["ok"] is True


# ---------------------------------------------------------------------------
# T5: the deferred watcher survives its pane vanishing mid-wait, via the
# pane's tmux session name
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_await_followup_reresolves_pane_after_session_hop():
    # The original pane (%32) vanishes mid-wait (capture-pane fails once); the
    # watcher falls back to the session's *current* pane (%99, per
    # FakeRunner's reresolved_pane) and keeps watching — and pastes the resume
    # against — that pane instead.
    r = FakeRunner(reresolved_pane="%99")
    n = {"captures": 0}

    def flaky(argv, **kw):
        if "capture-pane" in argv:
            n["captures"] += 1
            if n["captures"] == 1:
                proc = subprocess.CompletedProcess(argv, 1, stdout="", stderr="gone")
            else:
                snapshot = ("Compacting conversation…" if n["captures"] == 2
                           else "Compacted (ctrl+o to see full summary)\n❯ ")
                proc = subprocess.CompletedProcess(argv, 0, stdout=snapshot, stderr="")
            r.calls.append((argv, kw))
            return proc
        return r(argv, **kw)

    tmux_inject._await_and_followup(
        "/compact", "resume", "%32", session="sess0",
        socket=None, runner=flaky, sleep=lambda _s: None, clock=FakeClock())
    loaded = [kw["input"] for argv, kw in r.calls if "load-buffer" in argv]
    assert loaded == [b"resume"]
    # The resume must have been pasted against the re-resolved pane, not the
    # vanished one.
    pastes = [argv for argv, _ in r.calls if "paste-buffer" in argv]
    assert any("%99" in argv for argv in pastes)


@pytest.mark.smoke
def test_await_followup_gives_up_if_session_also_gone():
    # If the session itself is gone (re-resolve also fails), drop the resume
    # cleanly rather than loop or crash.
    r = FakeRunner(returncode=1)
    tmux_inject._await_and_followup(
        "/compact", "resume", "%32", session=None,
        socket=None, runner=r, sleep=lambda _s: None, clock=FakeClock())
    loaded = [kw["input"] for argv, kw in r.calls if "load-buffer" in argv]
    assert loaded == []


# ---------------------------------------------------------------------------
# T6: fire on the compacting→anything transition, not on settled idle alone —
# so an agent that self-resumes immediately after /compact isn't missed.
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_await_followup_fires_when_agent_self_resumes_after_compacting():
    # busy -> compacting -> busy again (the agent started its own next turn
    # immediately, no idle gap, no lingering Compacted marker). Must still
    # fire promptly on the very next sample after compacting, not wait for an
    # idle streak that will never come.
    r = FakeRunner(captures=[
        "assistant working… esc to interrupt",     # driving turn
        "Compacting conversation…",                  # compaction underway
        "assistant working… esc to interrupt",       # self-resumed immediately
        "assistant working… esc to interrupt",       # still going (must not matter)
    ])
    tmux_inject._await_and_followup(
        "/compact", "resume now", "%32",
        socket=None, runner=r, sleep=lambda _s: None, clock=FakeClock())
    loaded = [kw["input"] for argv, kw in r.calls if "load-buffer" in argv]
    assert loaded == [b"resume now"]
    caps = [argv for argv, _ in r.calls if "capture-pane" in argv]
    # Fired on the third sample (first non-compacting after compacting) —
    # never consumed the fourth.
    assert len(caps) == 3


# ---------------------------------------------------------------------------
# T7: autodetect_pane() picks instead of blocking on multiple agent panes
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_autodetect_pane_single_match():
    r = FakeRunner(list_panes=[("%1", "111", "claude", "sess0", 10)])
    assert tmux_inject.autodetect_pane(runner=r) == "%1"


@pytest.mark.smoke
def test_autodetect_pane_zero_matches_raises():
    r = FakeRunner(list_panes=[])
    with pytest.raises(tmux_inject.TmuxError, match="could not auto-detect"):
        tmux_inject.autodetect_pane(runner=r)


@pytest.mark.smoke
def test_autodetect_pane_multiple_matches_picks_most_active():
    # Ambiguity (multiple agent panes on the server) must never block the
    # caller — the most-recently-active candidate is picked, not raised on.
    r = FakeRunner(list_panes=[
        ("%1", "111", "claude", "sess0", 10),
        ("%2", "222", "claude", "sess1", 99),   # most recently active
        ("%3", "333", "claude", "sess2", 50),
    ])
    assert tmux_inject.autodetect_pane(runner=r) == "%2"


# ---------------------------------------------------------------------------
# T8: the follow-up re-checks tmux immediately before sending, so an agent
# that bounced to another pane mid-wait still gets its resume there.
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_await_followup_rechecks_tmux_before_sending():
    # The cached pane (%32) is still alive throughout the wait, but by the
    # time the resume is ready to send, auto-detect resolves to a different
    # pane (%77) — the agent bounced there. The resume must follow it.
    r = FakeRunner(
        captures=[
            "assistant working… esc to interrupt",
            "Compacting conversation…",
            "Compacted (ctrl+o to see full summary)\n❯ ",
        ],
        list_panes=[("%77", "555", "claude", "sess9", 42)],
    )
    tmux_inject._await_and_followup(
        "/compact", "resume", "%32",
        socket=None, runner=r, sleep=lambda _s: None, clock=FakeClock())
    pastes = [argv for argv, _ in r.calls if "paste-buffer" in argv]
    assert any("%77" in argv for argv in pastes)
    assert not any("%32" in argv for argv in pastes)


@pytest.mark.smoke
def test_await_followup_falls_back_when_final_autodetect_finds_nothing():
    # If the last-moment re-check can't find any agent pane at all (e.g. a
    # transient scan gap), fall back to the cached/last-known pane rather
    # than dropping the resume.
    r = FakeRunner(
        captures=[
            "assistant working… esc to interrupt",
            "Compacting conversation…",
            "Compacted (ctrl+o to see full summary)\n❯ ",
        ],
        list_panes=[],
    )
    tmux_inject._await_and_followup(
        "/compact", "resume", "%32",
        socket=None, runner=r, sleep=lambda _s: None, clock=FakeClock())
    pastes = [argv for argv, _ in r.calls if "paste-buffer" in argv]
    assert any("%32" in argv for argv in pastes)
