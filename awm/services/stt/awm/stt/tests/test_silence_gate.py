"""Deterministic tests for the VAD-driven silence-cut / auto-submit gate.

The real Silero VAD and whisper passes are stubbed so the cut → finalize and the
loop-owned auto-submit can be exercised without audio or a model. The LLM refiner
is gone: a cut just accumulates the raw whisper text into the composer, and the
silence poll owns submit timing — it auto-submits once the mic has been silent for
``SUBMIT_SILENCE_SEC``, measured blip-tolerantly from a fixed trailing window so a
momentary noise does not reset the clock.

Covers:
  1. a cut finalizes the composer (raw text) and never submits on its own
  2. the instant raw preview is broadcast before the accurate re-passed composer
  3. ``_maybe_submit`` fires once trailing silence >= the threshold
  4. it holds while speech is recent (trailing silence too small)
  5. a short blip in the trailing window does NOT stop the submit (blip floor)
  6. with telemetry on, the cut emits convo_cut (not llm_refine); none when off
  7. the fast poll fires a cut on voice-then-silence, and holds otherwise

Run via the per-dist runner, or directly:

    PYTHONPATH=awm/services/stt mamba run -n awm \\
        python -m pytest awm/services/stt/awm/stt/tests/test_silence_gate.py -q
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from awm.stt import registry as reg
from awm.stt import vad
from awm.stt.convo.session import ConvoSession

SR = 16000
SB = 2


def _fake_segments(text):
    """A stand-in for ``_transcribe_segments``: one segment carrying ``text``
    (or no segments when ``text`` is empty)."""

    def f(pcm_bytes, vad_filter=False):
        return [(text, 0.0, 1.0)] if text else []

    return f


def _agent_with_capture():
    agent = reg.SttAgent("u", Path("/tmp"))
    agent.continuous = True
    agent.convo = ConvoSession()
    sent: list[dict] = []

    async def cap(payload):
        sent.append(payload)

    agent.broadcast_json = cap  # type: ignore[assignment]
    return agent, sent


def _types(sent, t):
    return [p for p in sent if p.get("type") == t]


async def _run_cut(monkeypatch, *, cut_text, committed_prefix="", telemetry=False):
    """Drive one silence-cut end-to-end and return the broadcasts. The cut
    finalizes the composer; it does NOT submit (the silence poll does)."""
    monkeypatch.setattr(reg, "_transcribe_segments", _fake_segments(cut_text))
    agent, sent = _agent_with_capture()
    agent.committed_text = committed_prefix
    agent.telemetry = telemetry

    joined = b"\x00" * 64000  # 2s of audio
    await agent._silence_cut(joined, joined, committed_prefix)
    await asyncio.sleep(0.2)  # let the dispatched re-pass task finish
    composed = (committed_prefix + " " + cut_text).strip()
    return agent, sent, composed


# ---- the finalize-cut path ----

async def test_cut_finalizes_composer_no_submit(monkeypatch):
    agent, sent, composed = await _run_cut(
        monkeypatch, cut_text="add a login button",
        committed_prefix="please", telemetry=True,
    )
    composers = _types(sent, "composer")
    # The instant raw preview lands first, the accurate composer last.
    assert composers[0]["text"] == "please", "first composer must be the raw preview"
    assert composers[-1]["text"] == composed == "please add a login button"
    # A cut never submits on its own — that's the silence poll's job.
    assert not _types(sent, "submit")
    assert agent.convo.composer == composed
    # The window was reset by the cut.
    assert agent._cut_epoch == 1
    assert agent.committed_bytes == 64000
    assert agent.committed_text == ""


async def test_preview_raw_broadcast_before_composer(monkeypatch):
    agent, sent, composed = await _run_cut(
        monkeypatch, cut_text="add a login button", committed_prefix="please",
    )
    composers = _types(sent, "composer")
    assert len(composers) >= 2, "expected a raw preview then the re-passed composer"
    assert composers[0]["text"] == "please"
    assert composers[-1]["text"] == composed


async def test_cut_builds_text_from_tail_with_empty_prefix(monkeypatch):
    # A short utterance the cosmetic loop never transcribed — the cut's own
    # whisper pass produces the text from the tail (empty committed prefix).
    agent, sent, composed = await _run_cut(
        monkeypatch, cut_text="hello", committed_prefix="",
    )
    assert _types(sent, "composer")[-1]["text"] == "hello"
    assert agent.convo.composer == "hello"


async def test_metric_frames_on_cut(monkeypatch):
    # Dev telemetry: the cut path emits per-stage frames, and crucially NO
    # llm_refine (the LLM is gone) — convo_cut is the finalize marker.
    agent, sent, _ = await _run_cut(
        monkeypatch, cut_text="add a login button",
        committed_prefix="please", telemetry=True,
    )
    events = [m["event"] for m in _types(sent, "metric")]
    for ev in ("silence_cut", "cut_preview", "whisper_repass", "convo_cut"):
        assert ev in events, f"missing {ev} metric; got {events}"
    assert "llm_refine" not in events
    assert "refine_preview" not in events
    # Ordering: the cut and its instant preview precede the accumulate marker.
    assert events.index("silence_cut") < events.index("convo_cut")
    assert events.index("cut_preview") < events.index("convo_cut")


async def test_no_metrics_when_off(monkeypatch):
    agent, sent, _ = await _run_cut(
        monkeypatch, cut_text="add a login button", committed_prefix="please",
    )
    assert _types(sent, "metric") == []


# ---- the loop-owned auto-submit (the guarantee) ----

def _fill_submit_window(agent):
    """Give the agent a trailing window long enough to demonstrate the full
    SUBMIT_SILENCE_SEC span, and return it joined."""
    win_sec = reg.SUBMIT_SILENCE_SEC + reg.SUBMIT_VAD_PAD_SEC
    agent.pcm_chunks = [b"\x00" * int((win_sec + 0.5) * SR * SB)]
    return b"".join(agent.pcm_chunks)


async def test_maybe_submit_fires_after_silence(monkeypatch):
    agent, sent = _agent_with_capture()
    agent.telemetry = True
    await agent.convo.on_silence_cut("hello world")  # composer populated by a cut
    joined = _fill_submit_window(agent)
    # Last real speech ended early in the window → trailing silence is large.
    monkeypatch.setattr(
        vad, "last_speech_end_s",
        lambda pcm, sample_rate=16000, **kw: 0.2,
    )
    await agent._maybe_submit(joined, asyncio.get_running_loop())
    submits = _types(sent, "submit")
    assert submits and submits[0]["text"] == "hello world"
    assert agent._submitted is True
    assert agent.convo.composer == ""  # flushed by take_submission
    # A submit metric carries the measured trailing silence.
    m = next(m for m in _types(sent, "metric") if m["event"] == "submit")
    assert m["trailing_silence"] >= reg.SUBMIT_SILENCE_SEC


async def test_maybe_submit_holds_when_speech_recent(monkeypatch):
    agent, sent = _agent_with_capture()
    await agent.convo.on_silence_cut("hello")
    joined = _fill_submit_window(agent)
    win_sec = reg.SUBMIT_SILENCE_SEC + reg.SUBMIT_VAD_PAD_SEC
    # Speech runs to the very end of the window → trailing silence ~0 → hold.
    monkeypatch.setattr(
        vad, "last_speech_end_s",
        lambda pcm, sample_rate=16000, **kw: win_sec,
    )
    await agent._maybe_submit(joined, asyncio.get_running_loop())
    assert not _types(sent, "submit")
    assert agent._submitted is False
    assert agent.convo.composer == "hello"  # still building the message


async def test_maybe_submit_skips_when_nothing_composed(monkeypatch):
    # No composed message yet → never submits, even on a fully silent window.
    agent, sent = _agent_with_capture()
    joined = _fill_submit_window(agent)
    monkeypatch.setattr(
        vad, "last_speech_end_s",
        lambda pcm, sample_rate=16000, **kw: None,
    )
    await agent._maybe_submit(joined, asyncio.get_running_loop())
    assert not _types(sent, "submit")


async def test_blip_does_not_reset_submit(monkeypatch):
    # The user's requirement: a brief blip in the trailing-silence window must NOT
    # restart the submit clock. The loop passes min_region_ms=VAD_BLIP_MS, so a
    # sub-floor region is ignored and the trailing silence is measured against the
    # last REAL speech — the message still submits.
    agent, sent = _agent_with_capture()
    await agent.convo.on_silence_cut("done talking")
    joined = _fill_submit_window(agent)

    def fake(pcm, sample_rate=16000, *, min_region_ms=0, threshold=None):
        # A 400ms real word early, then a 100ms blip late in the window.
        regions = [(0.10, 0.50), (2.40, 2.50)]
        floor = min_region_ms / 1000.0
        kept = [(s, e) for (s, e) in regions if (e - s) >= floor]
        return kept[-1][1] if kept else None

    # Document the contrast the loop relies on:
    assert fake(b"", min_region_ms=0) == pytest.approx(2.50)        # blip counted
    assert fake(b"", min_region_ms=reg.VAD_BLIP_MS) == pytest.approx(0.50)  # blip ignored

    monkeypatch.setattr(vad, "last_speech_end_s", fake)
    await agent._maybe_submit(joined, asyncio.get_running_loop())
    assert _types(sent, "submit"), "a blip in the window must not stop the submit"
    assert agent._submitted is True


# ---- the fast silence poll's cut timing ----

async def test_silence_loop_fires_on_voice_then_silence(monkeypatch):
    monkeypatch.setattr(reg, "VAD_POLL_SEC", 0.01)
    # tail_dur = 3.0s; voice ends at 0.5s → gap 2.5s ≥ SILENCE_HANG → cut.
    monkeypatch.setattr(
        vad, "last_speech_end_s",
        lambda pcm, sample_rate=16000, **kw: 0.5,
    )

    agent, _ = _agent_with_capture()
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
    monkeypatch.setattr(
        vad, "last_speech_end_s",
        lambda pcm, sample_rate=16000, **kw: 2.5,
    )

    agent, _ = _agent_with_capture()
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
