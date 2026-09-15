"""The realm verbs against real arena daemons."""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.slow


async def _until(check, timeout=30.0):
    async with asyncio.timeout(timeout):
        while not (value := await check()):
            await asyncio.sleep(0.05)
    return value


async def _error(coro) -> str:
    from awm.rlm_chess.hub_adapter import RealmError
    with pytest.raises(RealmError) as info:
        await coro
    return info.value.code


def _awaiting(realm, sid, ply, external=True):
    async def check():
        state = (await realm._observe({"session_id": sid}))["state"]
        waiting = state["awaiting"]
        return waiting if waiting and waiting["ply"] == ply and waiting["external"] == external \
            else None
    return check


def _emitted(realm, sid, kind):
    async def check():
        return kind in realm.fake.kinds(sid)
    return check


async def test_bots_and_bad_requests(realm):
    assert {"naive", "simple"} <= set(realm._bots({})["bots"])
    assert await _error(realm._acquire({"game": "go"})) == "invalid_params"
    assert await _error(realm._acquire({"game": "chess", "opts": {"colour": "w"}})) \
        == "invalid_params"
    assert await _error(realm._acquire({"game": "chess", "opts": {"load": "nope"}})) \
        == "not_found"
    assert await _error(realm._acquire({"game": "chess", "opts": {
        "white": "nobody", "black": "naive"}})) == "invalid_params"
    assert await _error(realm._observe({"session_id": "../x"})) == "invalid_params"
    assert await _error(realm._observe({"session_id": "zzzzz"})) == "not_running"
    with pytest.raises(realm.RealmError, match="^invalid_params"):
        realm._replay({})


async def test_play_a_bot_save_resume_and_replay(realm):
    acquired = await realm._acquire({"game": "chess", "opts": {"white": "@human", "black": "naive"}})
    sid = acquired["session_id"]
    assert acquired["mode"] == "pvb"

    await _until(_awaiting(realm, sid, 0))
    assert await realm._move({"session_id": sid, "move": "e2e4", "ply": 0}) == {
        "accepted": True, "san": "e4"}
    await _until(_awaiting(realm, sid, 2))
    rejected = await realm._move({"session_id": sid, "move": "e2e4"})
    assert rejected["accepted"] is False and rejected["legal_moves"]
    assert await _error(realm._move({"session_id": sid, "move": "d2d4", "ply": 0})) == "stale"

    await realm._save({"session_id": sid, "name": "t-realm"})
    assert await _error(realm._save({"session_id": sid, "name": "t-realm"})) == "exists"
    listing = realm._saves({})
    assert "t-realm" in [s["name"] for s in listing["saves"]]
    assert [s["sid"] for s in (await realm._status({}))["sessions"]] == [sid]

    assert await realm._resign({"session_id": sid}) == {"result": "0-1", "reason": "resignation"}
    await _until(_emitted(realm, sid, "game_over"))
    assert {"session_started", "turn", "move", "rejected", "saved"} <= set(realm.fake.kinds(sid))
    seqs = [e["seq"] for e in realm.fake.emitted if e["session_id"] == sid and e["seq"] is not None]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)

    await realm._release({"session_id": sid})
    await _until(_emitted(realm, sid, "session_ended"))
    record = realm._replay({"session_id": sid})
    assert record["game_ids"] == [f"{sid}-1"] and record["outcome"]["reason"] == "resignation"

    saved = realm._replay({"name": "t-realm"})
    assert [m["san"] for m in saved["moves"]] == ["e4", saved["moves"][1]["san"]]
    assert saved["moves"][-1]["fen"]

    resumed = await realm._acquire({"game": "chess", "opts": {"load": "t-realm"}})
    assert resumed["mode"] == "pvb"
    await _until(_awaiting(realm, resumed["session_id"], 2))


async def test_watch_bots_pause_step_pace_and_rematch(realm):
    acquired = await realm._acquire({"game": "chess", "opts": {
        "white": "naive", "black": "naive", "min_ply_s": 0.1, "max_plies": 400, "seed": 3}})
    sid = acquired["session_id"]
    assert acquired["mode"] == "bvb"

    snap = await realm._snapshot({"session_id": sid})
    assert {"status", "state", "history"} <= set(snap["snapshot"])

    async def moved():
        return (await realm._observe({"session_id": sid}))["state"]["ply"] >= 2
    await _until(moved)
    assert (await realm._control("stop", timeout=60.0)({"session_id": sid}))["phase"] == "stopped"
    ply = (await realm._observe({"session_id": sid}))["state"]["ply"]
    stepped = await realm.HANDLERS["step"]({"session_id": sid})
    assert stepped["ply"] == ply + 1
    assert await realm.HANDLERS["set_pace"]({"session_id": sid, "min_ply_s": 0}) == {"min_ply_s": 0}
    await _until(_emitted(realm, sid, "pace"))

    rematch = await realm._reset({"session_id": sid})
    assert rematch["previous"] == sid and rematch["session_id"] != sid
    assert sid not in realm.sessions.live_sessions()
    status = (await realm._status({"session_id": rematch["session_id"]}))["sessions"][0]
    assert status["labels"] == {"owner": "rlm-chess", "mode": "bvb"}
    assert status["min_ply_s"] == 0.1


async def test_adopt_follows_only_owned_sessions(realm):
    owned = (await realm._acquire({"game": "chess", "opts": {
        "white": "@human", "black": "naive"}}))["session_id"]
    foreign, _ = await realm.sessions.create(
        "start_game", {"white": "@human", "black": "naive"}, python=realm.arena.PYTHON)
    for task in list(realm._PUMPS.values()):
        task.cancel()
    realm._PUMPS.clear()

    await realm._adopt()
    assert set(realm._PUMPS) == {owned}
    assert [s["sid"] for s in (await realm._status({}))["sessions"]] == [owned]
    assert foreign in realm.sessions.live_sessions()
