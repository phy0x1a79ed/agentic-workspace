"""The hub adapter's one seam: no reflection call fails without saying so.

Every way a reflection call can fail used to end as a plain result dict and
nothing else — no log line, no on-disk trace. That made "reflection didn't
compact my session" answerable only by reconstructing the moment from `/proc`,
the daemon roster and Claude Code's per-session records, long after the state
that would have explained it had moved on. These tests pin the two levels and
the one thing that must never be logged.
"""
from __future__ import annotations

import logging

import pytest

pytestmark = [pytest.mark.smoke]

from awm.reflection import hub_adapter, session_target


def _run(verb: str, fn, args: dict) -> dict:
    return hub_adapter._guarded(verb, fn)(args)


def test_a_transport_failure_is_logged_at_warning(caplog):
    def boom(_args):
        raise session_target.ResolveError("calling process 4242 is gone")

    with caplog.at_level(logging.WARNING, logger="awm.reflection.hub_adapter"):
        res = _run("compact", boom, {"_caller_pid": 4242})

    assert res == {"ok": False, "error": "calling process 4242 is gone"}
    assert any("compact" in r.message and "4242" in r.message
               and "is gone" in r.message for r in caplog.records)


def test_a_guard_refusal_is_logged_too_but_only_at_info(caplog):
    # A refusal is the service working as designed, not a defect — but from the
    # caller's side it looks identical to a failure, so it still has to appear.
    refusal = {"ok": False, "refused": True, "reason": "'/clear' irreversibly …"}

    with caplog.at_level(logging.INFO, logger="awm.reflection.hub_adapter"):
        res = _run("send", lambda _a: refusal, {"_caller_pid": 7})

    assert res is refusal, "the result the caller sees must not change"
    levels = {r.levelno for r in caplog.records}
    assert levels == {logging.INFO}


def test_the_callers_own_prompt_text_is_never_logged(caplog):
    # `send`'s `text` is whatever the calling session is about to type into
    # itself. It has no business in a service log.
    secret = "some private prompt content"
    with caplog.at_level(logging.INFO, logger="awm.reflection.hub_adapter"):
        _run("send", lambda _a: {"ok": False, "reason": "nope"},
             {"_caller_pid": 7, "text": secret})

    assert not any(secret in r.getMessage() for r in caplog.records)


def test_a_successful_call_is_left_alone(caplog):
    ok = {"ok": True, "session": "test", "submitted": True}
    with caplog.at_level(logging.INFO, logger="awm.reflection.hub_adapter"):
        assert _run("compact", lambda _a: ok, {"_caller_pid": 7}) is ok
    assert caplog.records == []


def test_every_verb_goes_through_the_seam():
    # A handler wired in later without the wrapper would be silently exempt from
    # all of the above, which is exactly how the gap opened the first time.
    assert set(hub_adapter.HANDLERS) == {"send", "compact", "mode", "pending",
                                         "whoami"}
    for verb, handler in hub_adapter.HANDLERS.items():
        assert handler.__qualname__.startswith("_guarded."), verb


def test_a_missing_caller_pid_does_not_break_the_log(caplog):
    # The gateway strips the key when it has no header to stamp, so the failure
    # path must survive its absence — logging "None" is fine, raising is not.
    def boom(_args):
        raise session_target.ResolveError("could not tell which session is calling")

    with caplog.at_level(logging.WARNING, logger="awm.reflection.hub_adapter"):
        res = _run("whoami", boom, {})
    assert res["ok"] is False
    assert len(caplog.records) == 1


# ---------------------------------------------------------------------------
# The log must not overstate what happened
# ---------------------------------------------------------------------------

def test_a_refused_command_is_never_logged_as_injected(caplog, monkeypatch):
    # Observed live: `inject.send` announced "injecting '/clear'" and only then
    # did the backend's guard refuse it, so the service log claimed a
    # destructive command had gone in when nothing had. On the one path where
    # someone is trying to find out why a command did not run, a log that
    # overstates what happened is worse than no log at all.
    from awm.reflection import inject, session_target as stgt

    target = stgt.DaemonTarget(sock="/tmp/x.sock", auth="t", session_id="sid",
                               repl_pid=1, name="test")
    monkeypatch.setattr(inject.session_target, "resolve", lambda _pid: target)

    def must_not_run(*a, **k):
        raise AssertionError("the backend must not be reached for a refusal")

    monkeypatch.setattr(inject.daemon_inject, "send", must_not_run)

    with caplog.at_level(logging.INFO, logger="awm.reflection.inject"):
        res = inject.send("/clear", caller_pid=1)

    assert res["refused"] is True
    assert not any("injecting" in r.getMessage() for r in caplog.records)


def test_an_overlay_does_not_re_arm_the_bases_promises(monkeypatch):
    # A shadow does not replace the base, it only takes the prefix — so the base
    # is still holding a watcher for every promise in that directory. Replaying
    # here arms a second one and the session gets its resume twice. Observed
    # live while shadowing this service to test it.
    called = []
    monkeypatch.setattr(hub_adapter.inject, "replay_pending",
                        lambda: called.append(1) or {"pending": 0, "resumed": 0})

    monkeypatch.setenv("AWM_SERVICE_OVERLAY", "1")
    hub_adapter._on_start()
    assert called == [], "an overlay must leave the base's promises alone"

    monkeypatch.delenv("AWM_SERVICE_OVERLAY")
    hub_adapter._on_start()
    assert called == [1], "a base must still replay its own"
