"""Unit tests for the two-engine TTS surface and piper's config dropdowns.

These cover the engine-surface + schema-enrichment logic without loading a
voice model or reaching the RVC sidecar (the heavy synth + sidecar paths are
exercised by the isolated bring-up harness, not here).
"""

import asyncio

import pytest

from awm.tts.app import EXPOSED_TTS_ENGINES, _enrich_piper_enums
from awm.tts.voice import engines as registry
from awm.tts.voice.engines.tts import piper as piper_engine


def test_exactly_two_exposed_engines():
    assert tuple(EXPOSED_TTS_ENGINES) == ("piper", "sbv2")


def test_piper_config_defaults_and_schema():
    schema = piper_engine.PiperConfig.model_json_schema()
    props = schema["properties"]
    # rvc_voice is an off-defaulted dropdown.
    assert props["rvc_voice"]["default"] == "off"
    assert props["rvc_voice"]["enum"] == ["off"]
    # pitch / speed are bounded (rendered as sliders).
    assert props["pitch"]["minimum"] == -24 and props["pitch"]["maximum"] == 24
    assert props["speed"]["minimum"] == 0.5 and props["speed"]["maximum"] == 2.0


def test_list_piper_voices_scans_models_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AWM_DATA_DIR", str(tmp_path))
    voices_dir = tmp_path / "tts-models" / "piper"
    voices_dir.mkdir(parents=True)
    (voices_dir / "en_US-test-medium.onnx").write_bytes(b"\x00")
    (voices_dir / "en_US-test-medium.onnx.json").write_text("{}")
    assert piper_engine.list_piper_voices() == ["en_US-test-medium.onnx"]


def test_list_piper_voices_empty_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("AWM_DATA_DIR", str(tmp_path))
    assert piper_engine.list_piper_voices() == []


def test_enrich_piper_enums_stamps_dropdowns(tmp_path, monkeypatch):
    monkeypatch.setenv("AWM_DATA_DIR", str(tmp_path))
    voices_dir = tmp_path / "tts-models" / "piper"
    voices_dir.mkdir(parents=True)
    (voices_dir / "en_US-test-medium.onnx").write_bytes(b"\x00")
    # Point the RVC sidecar at a dead port so the fetch fails gracefully ->
    # rvc_voice enum should still contain exactly "off".
    monkeypatch.setenv("TTS_RVC_URL", "http://127.0.0.1:1")

    entry = registry.list_engines()["tts"]["piper"]
    asyncio.run(_enrich_piper_enums(entry))
    props = entry["schema"]["properties"]
    assert props["voice"]["enum"] == ["en_US-test-medium.onnx"]
    assert props["rvc_voice"]["enum"] == ["off"]


def test_piper_synth_skips_rvc_when_off(monkeypatch):
    """rvc_voice='off' must never touch the sidecar."""
    cfg = piper_engine.PiperConfig(rvc_voice="off")

    eng = piper_engine.PiperEngine.__new__(piper_engine.PiperEngine)
    eng.cfg = cfg
    eng._sample_rate = 22050
    eng._synth_raw = lambda text: b"\x01\x02\x03\x04"

    def _boom(_pcm):
        raise AssertionError("RVC stage must not be called when rvc_voice='off'")

    eng._apply_rvc = _boom
    assert eng.synth("hello") == b"\x01\x02\x03\x04"
    assert eng.synth("   ") == b""
