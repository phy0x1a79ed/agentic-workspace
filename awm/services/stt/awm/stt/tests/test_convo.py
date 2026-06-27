"""Headless tests for the convo accumulator (the LLM-free inner loop).

``ConvoSession`` now just accumulates the raw whisper transcript across silence
cuts into a single composer message and flushes on submit — there is no LLM seam
and no network. Submit timing lives in the registry's silence poll, exercised in
``test_silence_gate.py``.

Run via the per-dist runner (PYTHONPATH = the stt dist root + components):

    awm/gateway/scripts/run-tests.sh stt

or directly, pointing PYTHONPATH at this dist's source root:

    PYTHONPATH=awm/services/stt mamba run -n awm \\
        python -m pytest awm/services/stt/awm/stt/tests/test_convo.py -q
"""

from __future__ import annotations

import asyncio

import pytest

from awm.stt.convo.session import ConvoSession


def run(maker):
    """Run an async test body (a 0-arg coroutine function) in one event loop,
    so a session's asyncio.Lock is never shared across loops."""
    return asyncio.run(maker())


def test_session_takes_no_agent():
    # The package no longer has an LLM/agent seam — a session is constructed with
    # no arguments and pulls in nothing network-bound.
    s = ConvoSession()
    assert s.raw_log == []
    assert s.composer == ""


def test_accumulate_across_cuts():
    s = ConvoSession()

    async def body():
        c1 = await s.on_silence_cut("add a login button")
        assert c1 == "add a login button"
        assert s.composer == "add a login button"
        assert s.raw_log == ["add a login button"]

        # A second cut (a brief pause that didn't submit) keeps building the same
        # message — the composer is the join of all raw chunks so far.
        c2 = await s.on_silence_cut("to the navbar")
        assert c2 == "add a login button to the navbar"
        assert s.raw_log == ["add a login button", "to the navbar"]
        assert s.composer == "add a login button to the navbar"

    run(body)


def test_take_submission_flushes():
    s = ConvoSession()

    async def body():
        await s.on_silence_cut("first part")
        await s.on_silence_cut("second part")
        flushed = await s.take_submission()
        assert flushed == "first part second part"
        # Working logs reset so the next utterance starts fresh.
        assert s.raw_log == []
        assert s.composer == ""
        # A fresh cut after a submit starts a brand-new message.
        c = await s.on_silence_cut("a new message")
        assert c == "a new message"
        assert s.composer == "a new message"

    run(body)


def test_empty_cut_is_ignored():
    s = ConvoSession()

    async def body():
        await s.on_silence_cut("hello")
        # An empty/whitespace cut doesn't append a blank chunk or change state.
        c = await s.on_silence_cut("   ")
        assert c == "hello"
        assert s.raw_log == ["hello"]

    run(body)


def test_reset_drops_state():
    s = ConvoSession()

    async def body():
        await s.on_silence_cut("something")

    run(body)
    s.reset()
    assert s.raw_log == []
    assert s.composer == ""


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
