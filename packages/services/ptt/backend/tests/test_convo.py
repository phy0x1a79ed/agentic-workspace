"""Headless tests for the convo inner loop with a stubbed LLM backend.

Run from the service root (so ``backend`` is importable):

    cd packages/services/ptt
    mamba run -n awm python -m pytest backend/tests/test_convo.py -q
"""

from __future__ import annotations

import asyncio

import pytest

from backend.agent import AgentError
from backend.convo.session import ConvoResult, ConvoSession


class StubAgent:
    """Records prompts and replays a scripted list of complete() outcomes.

    Each script entry is either a dict (returned) or an Exception (raised).
    """

    def __init__(self, script):
        self.script = list(script)
        self.prompts: list[str] = []
        self.i = 0

    async def complete(self, prompt_text, schema, *, directory=None):
        self.prompts.append(prompt_text)
        item = self.script[self.i]
        self.i += 1
        if isinstance(item, Exception):
            raise item
        return item


def run(maker):
    """Run an async test body (a 0-arg coroutine function) in one event loop,
    so a session's asyncio.Lock is never shared across loops."""
    return asyncio.run(maker())


def test_accumulate_until_submit():
    agent = StubAgent([
        {"cleaned_text": "Add a login button.", "should_submit": False},
        {"cleaned_text": "Add a login button to the navbar.", "should_submit": True},
    ])
    s = ConvoSession(agent)

    async def body():
        r1 = await s.on_silence_cut("add a login button")
        assert isinstance(r1, ConvoResult)
        assert r1.cleaned_text == "Add a login button."
        assert r1.should_submit is False
        assert s.composer == "Add a login button."
        assert s.raw_log == ["add a login button"]

        r2 = await s.on_silence_cut("to the navbar")
        assert r2.should_submit is True
        assert r2.cleaned_text == "Add a login button to the navbar."
        # Flushed on submit: working logs cleared, ready for a fresh message.
        assert s.raw_log == []
        assert s.composer == ""

    run(body)


def test_prior_vs_new_framing():
    agent = StubAgent([
        {"cleaned_text": "First.", "should_submit": False},
        {"cleaned_text": "First. Second.", "should_submit": False},
    ])
    s = ConvoSession(agent)

    async def body():
        await s.on_silence_cut("first")
        await s.on_silence_cut("second")

    run(body)

    # On the second cut, the earlier utterance is the prior_raw, the new one is
    # in the NEW CHUNK section.
    p2 = agent.prompts[1]
    prior = p2.split("[ALREADY COMPOSED — raw]")[1].split("[YOUR PRIOR")[0]
    new = p2.split("[NEW CHUNK SINCE LAST PAUSE — raw]")[1]
    assert "first" in prior
    assert "second" in new
    assert "second" not in prior


def test_notes_persist_and_feed_forward():
    agent = StubAgent([
        {"cleaned_text": "Deploy Hyperion.", "should_submit": False,
         "notes_update": "Project name is 'Hyperion' (not 'hyperon')."},
        {"cleaned_text": "Deploy Hyperion now.", "should_submit": True},
    ])
    s = ConvoSession(agent)

    async def body():
        await s.on_silence_cut("deploy hyperon")
        assert "Hyperion" in s.notes_pad
        await s.on_silence_cut("now")

    run(body)
    # Second prompt carried the notes forward.
    assert "Hyperion" in agent.prompts[1]
    # Notes survive a submit-driven flush.
    assert "Hyperion" in s.notes_pad


def test_fallback_on_agent_error():
    agent = StubAgent([AgentError("zen down")])
    s = ConvoSession(agent)

    async def body():
        return await s.on_silence_cut("hello world")

    r = run(body)
    assert r.fallback is True
    assert r.should_submit is False
    assert r.cleaned_text == "hello world"
    assert s.composer == "hello world"


def test_context_capped():
    agent = StubAgent([{"cleaned_text": "x", "should_submit": False}])
    s = ConvoSession(agent)
    s.set_context("y" * 5000)
    assert len(s.context) <= 2000


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
