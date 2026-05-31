"""Voice engine config (global STT + TTS selection) persistence tests."""

from __future__ import annotations

import json

import pytest

from awm.services import voice_engine_config


def test_get_global_empty(awm_workspace):
    """Fresh DB → both kinds are None."""
    assert voice_engine_config.get_global() == {"stt": None, "tts": None}


def test_set_and_get_global_roundtrip(awm_workspace):
    voice_engine_config.set_global("stt", "whisper", {"model": "small.en", "compute_type": "int8"})
    voice_engine_config.set_global("tts", "piper", {"voice": "en_US"})

    got = voice_engine_config.get_global()
    assert got["stt"] == {"engine_id": "whisper", "params": {"model": "small.en", "compute_type": "int8"}}
    assert got["tts"] == {"engine_id": "piper", "params": {"voice": "en_US"}}


def test_set_global_rejects_unknown_kind(awm_workspace):
    with pytest.raises(ValueError, match="unknown voice engine kind"):
        voice_engine_config.set_global("llm", "openrouter", {})


def test_overwrite_keeps_only_latest(awm_workspace):
    voice_engine_config.set_global("stt", "whisper", {"model": "small.en"})
    voice_engine_config.set_global("stt", "sherpa", {"lang": "en"})
    assert voice_engine_config.get_global()["stt"] == {
        "engine_id": "sherpa",
        "params": {"lang": "en"},
    }


def test_params_survive_reopen(awm_workspace, db_conn):
    """Closing + reopening the DB connection keeps both rows."""
    voice_engine_config.set_global("tts", "kokoro_rvc", {"speaker": "alice", "rate": 1.0})
    db_conn.close()
    got = voice_engine_config.get_global()
    assert got["tts"] == {"engine_id": "kokoro_rvc", "params": {"speaker": "alice", "rate": 1.0}}


def test_malformed_params_row_falls_back_to_empty_dict(awm_workspace, db_conn):
    """If someone hand-edits the config row to non-JSON, we don't crash."""
    from awm.services.config_service import set_config
    set_config("voice_stt_engine", "whisper")
    db_conn.execute(
        "INSERT INTO config (key, value, updated_at) VALUES (?, ?, datetime('now'))",
        ("voice_stt_params", "not json{"),
    )
    db_conn.commit()
    got = voice_engine_config.get_global()
    assert got["stt"] == {"engine_id": "whisper", "params": {}}


def test_params_stored_as_json_string(awm_workspace, db_conn):
    """Sanity: the params row is JSON, so external SQL inspection is readable."""
    voice_engine_config.set_global("stt", "whisper", {"compute_type": "int8"})
    row = db_conn.execute(
        "SELECT value FROM config WHERE key = 'voice_stt_params'"
    ).fetchone()
    assert row is not None
    assert json.loads(row["value"]) == {"compute_type": "int8"}
