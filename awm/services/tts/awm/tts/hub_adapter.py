"""Hub adapter for the tts service (TTS-only).

Boots the tts service as a gateway-registered process on the shared
``awm.gatewayclient.ServiceAdapter`` loop (register → ready → serve →
reconnect). Replaces the former hand-rolled, ``AWM_HUB_TOKEN``-era control loop;
the gateway injects only ``AWM_HUB_URL`` / ``AWM_SERVICE_NAME`` /
``AWM_SERVICE_ID`` — there is no token.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.tts.hub_adapter

Functions (the TTS surface):
  - listEngines    — exposed TTS engines (schema + defaults) for the config form
  - listPresets    — named engine-config presets + the last-used pointer
  - savePreset / deletePreset
  - getAllState / getState / setState / delState — keyed UI-state bucket

Sessions:
  - kind="call", transport="direct" — the playback session. The browser opens a
    direct WS that byte-relays through the hub bridge; ``speak`` frames in, raw
    PCM frames + a ``done`` marker back. This is the only session the service
    exposes (the duplicate STT conversation loop was pruned; ``stt`` owns STT).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from awm.gatewayclient import ServiceAdapter, SessionContext
from awm.persistence.databases import init_service_db

from awm.tts import presets, state
from awm.tts.app import (
    EXPOSED_TTS_ENGINES,
    _enrich_piper_enums,
)
from awm.tts.text_clean import clean_for_tts
from awm.tts.voice import engines as engines_registry

log = logging.getLogger("awm.tts.hub_adapter")


API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "listEngines",
            "description": "Exposed TTS engines: JSON-Schema + defaults per engine.",
        },
        {
            "name": "listPresets",
            "description": "Named engine-config presets plus the last-used pointer.",
        },
        {
            "name": "savePreset",
            "description": "Create or update a named engine-config preset.",
            "params": [
                {"name": "name", "type": "string", "required": True},
                {"name": "engine", "type": "string", "required": True},
                {"name": "params", "type": "object", "required": False},
            ],
        },
        {
            "name": "deletePreset",
            "description": "Delete a named preset (built-ins are refused).",
            "params": [{"name": "name", "type": "string", "required": True}],
        },
        {
            "name": "getAllState",
            "description": "Return every keyed UI-state value.",
        },
        {
            "name": "getState",
            "description": "Return one keyed UI-state value.",
            "params": [{"name": "key", "type": "string", "required": True}],
        },
        {
            "name": "setState",
            "description": "Set one keyed UI-state value.",
            "params": [
                {"name": "key", "type": "string", "required": True},
                {"name": "value", "required": False},
            ],
        },
        {
            "name": "delState",
            "description": "Delete one keyed UI-state value.",
            "params": [{"name": "key", "type": "string", "required": True}],
        },
    ],
    "emitters": [],
    "sessions": [
        {"kind": "call", "transport": "direct"},
    ],
}


# -- function handlers ------------------------------------------------------


async def _h_list_engines(args: dict) -> dict:
    all_tts = engines_registry.list_engines()["tts"]
    exposed = {eid: all_tts[eid] for eid in EXPOSED_TTS_ENGINES if eid in all_tts}
    if "piper" in exposed:
        await _enrich_piper_enums(exposed["piper"])
    return exposed


def _h_list_presets(args: dict) -> dict:
    return {"presets": presets.list_presets(), "last_used": presets.get_last_used()}


def _h_save_preset(args: dict) -> None:
    presets.save_preset(args["name"], args.get("engine"), args.get("params") or {})


def _h_delete_preset(args: dict) -> None:
    presets.delete_preset(args["name"])


def _h_get_all_state(args: dict) -> dict:
    return state.list_state()


def _h_get_state(args: dict) -> dict:
    return {"value": state.get_state(args["key"])}


def _h_set_state(args: dict) -> None:
    state.set_state(args["key"], args.get("value"))


def _h_del_state(args: dict) -> None:
    state.delete_state(args["key"])


HANDLERS = {
    "listEngines": _h_list_engines,
    "listPresets": _h_list_presets,
    "savePreset": _h_save_preset,
    "deletePreset": _h_delete_preset,
    "getAllState": _h_get_all_state,
    "getState": _h_get_state,
    "setState": _h_set_state,
    "delState": _h_del_state,
}


# -- playback session -------------------------------------------------------


async def _run_call_session(ctx: SessionContext) -> None:
    """Direct ``call`` playback session.

    Loads the requested engine, then byte-relays through the hub bridge:
      client → ``{"type":"speak","text":...}`` / ``{"type":"reconfigure",...}``
      server → raw PCM bytes + ``{"type":"done","sample_rate":hz}``
    """
    init = ctx.init or {}
    engine_id = init.get("engine") or "piper"
    params = init.get("params") or {}
    try:
        info = engines_registry.load("tts", engine_id, params)
    except Exception as exc:
        log.warning("session engine load failed: %s", exc)
        return
    instance = engines_registry.current_instance("tts")
    if instance is None:
        log.warning("session: no current instance after load")
        return
    cur_engine = info["id"]

    bridge = await ctx.open_bridge()
    loop = asyncio.get_running_loop()
    try:
        while True:
            raw = await bridge.recv()
            if isinstance(raw, (bytes, bytearray)):
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = payload.get("type")
            if kind == "speak":
                text = clean_for_tts(payload.get("text", ""))
                try:
                    pcm = await loop.run_in_executor(None, instance.synth, text)
                except Exception as exc:
                    log.warning("synth failed (engine=%s): %s", cur_engine, exc)
                    await bridge.send(json.dumps({"type": "error", "message": f"synth: {exc}"}))
                    continue
                await bridge.send(pcm)
                await bridge.send(json.dumps({
                    "type": "done",
                    "sample_rate": getattr(instance, "sample_rate", 24000),
                }))
            elif kind == "reconfigure":
                eng = payload.get("engine") or cur_engine
                rparams = payload.get("params") or {}
                try:
                    info2 = engines_registry.load("tts", eng, rparams)
                    inst2 = engines_registry.current_instance("tts")
                    cur_engine = info2["id"]
                    if inst2 is not None:
                        instance = inst2
                    await bridge.send(json.dumps({
                        "type": "reconfigured",
                        "engine": info2["id"],
                        "instance_id": info2["instance_id"],
                    }))
                except Exception as exc:
                    await bridge.send(json.dumps({"type": "error", "message": str(exc)}))
    except Exception:
        # Bridge closed / network error — end the session quietly.
        pass
    finally:
        try:
            await bridge.close()
        except Exception:
            pass


SESSION_HANDLERS = {"call": _run_call_session}


# -- boot -------------------------------------------------------------------

# The tts service owns a one-table DB: a ``config`` KV table that ``presets``
# and ``state`` query via ``get_connection("tts")``. Init it on start so the
# very first listPresets/getAllState call doesn't hit a missing table.
_CONFIG_TABLE_DDL = """\
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT
);
"""


def _on_start() -> None:
    init_service_db("tts", _CONFIG_TABLE_DDL, schema_version=1)
    try:
        presets.seed_builtin_presets()
    except Exception as exc:
        log.warning("preset seed skipped: %s", exc)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter(
        "tts",
        API_MANIFEST,
        HANDLERS,
        session_handlers=SESSION_HANDLERS,
        on_start=_on_start,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
