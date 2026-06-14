"""Deterministic tests for the VAD-driven silence-cut / submit gate.

The real Silero VAD and whisper passes are stubbed so the cut → refine → arm →
confirm state machine can be exercised without audio or a model. Covers the core
rows of the plan's case table:

  1. complete + silent window  → submit
  2. complete + voiced window  → held (no submit), folds forward
  3. incomplete                → no arm, no submit
  4. the background pass transcribes the tail (works with empty committed prefix)
  5. the fast poll fires a cut on voice-then-silence, and holds otherwise
  6. the raw preview is broadcast BEFORE the cleaned composer (responsiveness)
  7. with telemetry opted in, the cut path emits high-res `metric` frames (and
     none at all when it's off)

Run via the per-dist runner, or directly:

    PYTHONPATH=awm/services/stt mamba run -n awm \\
        python -m pytest awm/services/stt/awm/stt/tests/test_silence_gate.py -q
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from awm.stt import registry as reg
from awm.stt import vad
from awm.stt.convo.session import ConvoSession


class StubCleanupAgent:
    """Replays a scripted list of complete_json() outcomes (dicts)."""

    def __init__(self, script):
        self.script = list(script)
        self.i = 0

    async def complete_json(self, prompt_text, *, directory=None):
        item = self.script[self.i]
        self.i += 1
        return item


def _fake_segments(text):
    """A stand-in for ``_transcribe_segments``: one segment carrying ``text``
    (or no segments when ``text`` is empty)."""

    def f(pcm_bytes, vad_filter=False):
        return [(text, 0.0, 1.0)] if text else []

    return f


def _agent_with_capture(convo):
    agent = reg.SttAgent("u", Path("/tmp"))
    agent.continuous = True
    agent.convo = convo
    sent: list[dict] = []

    async def cap(payload):
        sent.append(payload)

    agent.broadcast_json = cap  # type: ignore[assignment]
    return agent, sent


async def _run_cut(monkeypatch, *, cut_text, should_submit, window_voiced,
                   committed_prefix="", telemetry=False, refine=True):
    """Drive one silence-cut end-to-end and return the broadcast payloads.

    ``refine`` selects the convo path: True exercises the LLM cleanup (the
    scripted StubCleanupAgent reply), False the default raw-whisper + auto-submit
    path (no LLM)."""
    monkeypatch.setattr(reg, "_transcribe_segments", _fake_segments(cut_text))
    monkeypatch.setattr(reg, "SUBMIT_CONFIRM_SEC", 0.05)
    monkeypatch.setattr(
        vad, "has_speech", lambda pcm, sample_rate=16000: window_voiced,
    )

    if refine:
        cleaned = (committed_prefix + " " + cut_text).strip().capitalize()
        convo = ConvoSession(StubCleanupAgent(
            [{"cleaned_text": cleaned, "should_submit": should_submit}],
        ), refine=True)
    else:
        # No-refine: the composer is the raw (whisper) text, no LLM call.
        cleaned = (committed_prefix + " " + cut_text).strip()
        convo = ConvoSession(StubCleanupAgent([]), refine=False)
    agent, sent = _agent_with_capture(convo)
    agent.committed_text = committed_prefix
    agent.telemetry = telemetry

    joined = b"\x00" * 64000  # 2s of audio
    await agent._silence_cut(joined, joined, agent.committed_text)
    # Let the dispatched refine task and the confirm timer run to completion.
    await asyncio.sleep(0.2)
    return agent, sent, cleaned


def _types(sent, t):
    return [p for p in sent if p.get("type") == t]


async def test_complete_then_silent_submits(monkeypatch):
    agent, sent, cleaned = await _run_cut(
        monkeypatch, cut_text="add a login button",
        should_submit=True, window_voiced=False,
    )
    assert _types(sent, "composer") and _types(sent, "composer")[0]["text"] == cleaned
    submits = _types(sent, "submit")
    assert submits and submits[0]["text"] == cleaned
    # The window was reset by the cut.
    assert agent._cut_epoch == 1
    assert agent.committed_bytes == 64000
    assert agent.committed_text == ""


async def test_complete_but_resumed_holds(monkeypatch):
    agent, sent, cleaned = await _run_cut(
        monkeypatch, cut_text="add a login button",
        should_submit=True, window_voiced=True,  # user resumed during the window
    )
    assert _types(sent, "composer")  # refine still shown
    assert not _types(sent, "submit")  # but held — folds into the next cut


async def test_incomplete_no_arm(monkeypatch):
    agent, sent, _ = await _run_cut(
        monkeypatch, cut_text="the thing is",
        should_submit=False, window_voiced=False,
    )
    assert _types(sent, "composer")
    assert not _types(sent, "submit")
    assert agent._pending_submit is None


async def test_cut_builds_text_from_tail_with_empty_prefix(monkeypatch):
    # Case 4 essence: a short utterance the cosmetic loop never transcribed — the
    # cut's own whisper pass produces the text from the tail (empty prefix).
    agent, sent, cleaned = await _run_cut(
        monkeypatch, cut_text="hello", should_submit=True,
        window_voiced=False, committed_prefix="",
    )
    assert _types(sent, "submit") and _types(sent, "submit")[0]["text"] == cleaned


async def test_preview_raw_broadcast_before_cleaned(monkeypatch):
    # The core responsiveness change: the cut broadcasts the RAW preview (text the
    # cosmetic stream already produced, here the committed prefix) immediately,
    # BEFORE the accurate re-pass + LLM cleanup land the cleaned composer. So the
    # first composer frame is raw, a later one is cleaned — never gated behind the
    # LLM round-trip.
    agent, sent, cleaned = await _run_cut(
        monkeypatch, cut_text="add a login button",
        should_submit=False, window_voiced=False,
        committed_prefix="please",  # non-empty → a preview is available
    )
    composers = _types(sent, "composer")
    assert len(composers) >= 2, "expected a raw preview then a cleaned composer"
    assert composers[0]["text"] == "please", "first composer must be the raw preview"
    assert composers[-1]["text"] == cleaned, "last composer must be the cleaned text"
    # The raw preview is emitted before any 'refining' status (it's the very first
    # thing the cut does).
    first_composer_idx = next(i for i, p in enumerate(sent) if p.get("type") == "composer")
    first_refining_idx = next(
        i for i, p in enumerate(sent)
        if p.get("type") == "status" and p.get("stage") == "refining"
    )
    assert first_composer_idx < first_refining_idx


async def test_metric_frames_on_cut(monkeypatch):
    # Dev telemetry: with the session opted in, the cut path emits high-res
    # `metric` frames for each stage, with the expected ordering and durations.
    agent, sent, cleaned = await _run_cut(
        monkeypatch, cut_text="add a login button",
        should_submit=True, window_voiced=False,
        committed_prefix="please", telemetry=True,
    )
    metrics = _types(sent, "metric")
    events = [m["event"] for m in metrics]
    for ev in ("silence_cut", "refine_preview", "whisper_repass", "llm_refine"):
        assert ev in events, f"missing {ev} metric; got {events}"
    # The timed stages carry a numeric duration.
    for ev in ("whisper_repass", "llm_refine"):
        m = next(m for m in metrics if m["event"] == ev)
        assert isinstance(m["dur_ms"], (int, float))
    # Ordering: the cut and its instant preview precede the LLM refine.
    assert events.index("silence_cut") < events.index("llm_refine")
    assert events.index("refine_preview") < events.index("llm_refine")
    # llm_refine carries the verdict fields.
    refine = next(m for m in metrics if m["event"] == "llm_refine")
    assert refine["should_submit"] is True
    assert refine["fallback"] is False


async def test_no_refine_submits_raw_without_llm(monkeypatch):
    # Default convo path (refine off): the cut auto-submits the raw whisper text
    # with no LLM call. The cut path emits `convo_cut` (not `llm_refine`) and the
    # interim status is `transcribing`, never `refining`.
    agent, sent, cleaned = await _run_cut(
        monkeypatch, cut_text="add a login button",
        should_submit=True, window_voiced=False,
        committed_prefix="please", telemetry=True, refine=False,
    )
    # composer is the raw committed-prefix + tail (not capitalized/cleaned).
    composers = _types(sent, "composer")
    assert composers and composers[-1]["text"] == cleaned == "please add a login button"
    # complete + silent → submits the raw text.
    submits = _types(sent, "submit")
    assert submits and submits[0]["text"] == cleaned
    # metric framing reflects no-LLM.
    events = [m["event"] for m in _types(sent, "metric")]
    assert "convo_cut" in events
    assert "llm_refine" not in events
    # status never claims to be "refining".
    stages = [p.get("stage") for p in _types(sent, "status")]
    assert "refining" not in stages


async def test_no_metrics_when_off(monkeypatch):
    # Default (telemetry off): a normal session emits no metric frames at all.
    agent, sent, cleaned = await _run_cut(
        monkeypatch, cut_text="add a login button",
        should_submit=True, window_voiced=False,
        committed_prefix="please",
    )
    assert _types(sent, "metric") == []


async def test_silence_loop_fires_on_voice_then_silence(monkeypatch):
    monkeypatch.setattr(reg, "VAD_POLL_SEC", 0.01)
    # tail_dur = 3.0s; voice ends at 0.5s → gap 2.5s ≥ SILENCE_HANG → cut.
    monkeypatch.setattr(vad, "last_speech_end_s", lambda pcm, sample_rate=16000: 0.5)

    convo = ConvoSession(StubCleanupAgent([]))
    agent, _ = _agent_with_capture(convo)
    ws = object()
    agent.recording_client = ws
    agent.pcm_chunks = [b"\x00" * 96000]  # 3s

    calls: list[int] = []

    async def fake_cut(joined, tail, committed_prefix):
        calls.append(len(tail))
        agent.recording_client = None  # stop after one cut

    agent._silence_cut = fake_cut  # type: ignore[assignment]
    task = asyncio.create_task(agent._silence_loop(ws))
    await asyncio.sleep(0.1)
    agent.recording_client = None
    await asyncio.sleep(0.05)
    if not task.done():
        task.cancel()
    assert calls, "expected a silence-cut when gap ≥ hang"


async def test_silence_loop_holds_when_gap_small(monkeypatch):
    monkeypatch.setattr(reg, "VAD_POLL_SEC", 0.01)
    # tail_dur = 3.0s; voice ends at 2.5s → gap 0.5s < SILENCE_HANG → no cut.
    monkeypatch.setattr(vad, "last_speech_end_s", lambda pcm, sample_rate=16000: 2.5)

    convo = ConvoSession(StubCleanupAgent([]))
    agent, _ = _agent_with_capture(convo)
    ws = object()
    agent.recording_client = ws
    agent.pcm_chunks = [b"\x00" * 96000]  # 3s

    calls: list[int] = []

    async def fake_cut(joined, tail, committed_prefix):
        calls.append(len(tail))

    agent._silence_cut = fake_cut  # type: ignore[assignment]
    task = asyncio.create_task(agent._silence_loop(ws))
    await asyncio.sleep(0.08)
    agent.recording_client = None
    await asyncio.sleep(0.05)
    if not task.done():
        task.cancel()
    assert not calls, "must not cut while the user is still within the hang window"
