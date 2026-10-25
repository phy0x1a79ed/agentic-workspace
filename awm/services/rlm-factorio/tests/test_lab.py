"""Worlds of every kind, realm rules, seats, blocking verbs and the 0.6 tool
verbs, against fakes and an in-memory DB. No game runs."""

import hashlib
import json
import re
import socket
import sqlite3
import subprocess

import pytest

from awm.rlm_factorio import appliance, dao, hub_adapter as ha


class RespondingBroker:
    """Answers each RCON command from a responder(code) -> output."""

    def __init__(self, respond, cost=0.1):
        self.respond, self.sent, self.conns, self.cost = respond, [], [], cost
        self.closed = []

    def lua(self, conn, code, *, timeout):
        self.sent.append(code)
        self.conns.append(conn)
        return {"output": self.respond(code), "cost_ms": self.cost}

    def close_conn(self, conn):
        self.closed.append(conn)

    def calls(self, fn):
        return [c for c in self.sent if f"'{fn}'" in c]


def mod_responder(version="0.6.1", tools=(), calls=None):
    """A fake engine: version query, tool_list, and anything else echoes ok."""
    def respond(code):
        if "active_mods" in code:
            return version
        if "'tool_list'" in code:
            return json.dumps({"tools": list(tools)})
        if "'tool_define'" in code and calls is not None:
            calls.append(code)
        return json.dumps({"ok": True})
    return respond


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("RLM_FACTORIO_EXEC_LOG", str(tmp_path / "exec.jsonl"))
    monkeypatch.setenv("RLM_FACTORIO_ADMIN_TOKEN_FILE", str(tmp_path / "no-token"))
    monkeypatch.setattr(appliance, "SERVICES_DIR", tmp_path / "services")
    monkeypatch.setattr(ha, "_THROTTLES", {})
    monkeypatch.setattr(ha, "_CONN_USED", {})
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(dao.SCHEMA_SQL)
    real = dao.FactorioDAO
    monkeypatch.setattr(ha.dao, "FactorioDAO", lambda: real(conn=conn))
    ha._MOD_VERSIONS.clear()
    ha._EVENT_BUF.clear()


def make_world(kind="lab", name="t", rules=None, status="ready", headless=True):
    d = ha.dao.FactorioDAO()
    rules = rules or ("sandbox" if kind == "lab" else "realm")
    row = d.create_session("factorio", kind=kind, rules=rules, name=name,
                           mods=appliance.world_mods(kind), headless=headless,
                           **appliance.world_coords(kind, name, set()))
    return d.set_runtime(row["session_id"], status=status)


@pytest.fixture()
def LAB():
    return make_world()["session_id"]


@pytest.fixture()
def engine(monkeypatch):
    def install(**kw):
        b = RespondingBroker(mod_responder(**kw))
        monkeypatch.setattr(ha.broker, "for_row", lambda row: b)
        monkeypatch.setattr(ha.broker, "existing", lambda container: b)
        return b
    return install


def call(handler, /, **args):
    return ha.HANDLERS[handler](args)


# ---- world records and coordinates --------------------------------------------

def test_migration_backfills_old_rows_as_the_headless_realm_game():
    conn = sqlite3.connect(":memory:")
    conn.executescript(dao.MIGRATIONS[(1, 2)])
    conn.executescript("""CREATE TABLE rlm_sessions (session_id TEXT PRIMARY KEY,
        game TEXT, status TEXT, container_name TEXT, compose_project TEXT,
        control_port INTEGER, game_port INTEGER, rcon_port INTEGER,
        current_world TEXT, created_at TEXT, updated_at TEXT);
        INSERT INTO rlm_sessions (session_id) VALUES ('old');
        INSERT INTO rlm_seats (seat_id, session_id) VALUES ('s', 'old');""")
    conn.executescript(dao.MIGRATIONS[(2, 3)])
    conn.executescript(dao.MIGRATIONS[(3, 4)])
    assert conn.execute("SELECT kind, rules, name, mods, headless FROM rlm_sessions"
                        ).fetchone() == ("game", "realm", "realm", "[]", 1)
    assert conn.execute("SELECT role FROM rlm_seats").fetchone() == ("player",)


def test_migration_turns_v3_kinds_into_a_kind_and_the_toggle():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""CREATE TABLE rlm_sessions (session_id TEXT PRIMARY KEY, kind TEXT);
        INSERT INTO rlm_sessions VALUES ('h', 'headless'), ('r', 'rendered'), ('l', 'lab');""")
    conn.executescript(dao.MIGRATIONS[(3, 4)])
    rows = conn.execute("SELECT session_id, kind, headless FROM rlm_sessions ORDER BY 1")
    assert rows.fetchall() == [("h", "game", 1), ("l", "lab", 1), ("r", "game", 0)]


def test_realm_keeps_its_coordinates_and_others_get_their_own():
    realm = appliance.world_coords("game", "realm", set())
    assert realm == {"compose_project": appliance.PROJECT, "container_name": appliance.CONTAINER,
                     "control_port": appliance.CONTROL_PORT, "game_port": appliance.GAME_PORT}
    lab = appliance.world_coords("lab", "realm", set())
    assert lab["compose_project"] == "rlm-factorio-lab-realm"
    taken = {lab["control_port"], lab["game_port"]}
    other = appliance.world_coords("lab", "b", taken)
    assert other["control_port"] not in taken and other["game_port"] not in taken


def test_a_port_another_host_process_holds_is_skipped(monkeypatch):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("0.0.0.0", 0))
        held.listen()
        port = held.getsockname()[1]
        monkeypatch.setattr(appliance, "PORT_BASE", port)
        assert appliance.world_coords("lab", "b", set())["control_port"] != port


def override(row, boot=None):
    return json.loads(appliance.write_override(row, boot).read_text())["services"]["factorio"]


@pytest.mark.parametrize("kind,name", [("game", "realm"), ("game", "w"), ("lab", "t")])
def test_a_world_publishes_its_own_game_port_only_when_not_headless(kind, name):
    closed = make_world(kind, name)
    opened = ha.dao.FactorioDAO().set_runtime(closed["session_id"], headless=0)
    assert "ports" not in override(closed)
    assert override(opened)["ports"] == [f"{opened['game_port']}:12140/udp"]
    if name == "realm":
        assert opened["game_port"] == appliance.GAME_PORT


def test_a_lab_binds_saves_loads_lab_mods_and_lab_settings():
    row = make_world()
    svc = override(row, {"save": "base", "paused": True})
    vols = svc["volumes"]
    assert f"{appliance.saves_dir(row)}:/factorio/saves" in vols
    assert any(v.endswith("lab-server-settings.json:/factorio/config/server-settings.json:ro")
               for v in vols)
    for mod in appliance.LAB_MODS:
        assert f"{appliance.LAB_MODS_DIR / mod}:/opt/factorio/mods/{mod}:ro" in vols
    assert svc["environment"] == {"BOOT_SAVE": "base", "BOOT_NEW": "", "BOOT_PAUSED": "1"}
    assert appliance.compose_env(row)["FACTORIO_CPUS"] == "2.0"
    assert appliance.compose_files(row)[-1].name == "compose.json"


def test_compose_runs_the_world_project_with_its_override(monkeypatch):
    row = make_world()
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, kw["env"]))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(appliance, "ensure_image", lambda: None)
    monkeypatch.setattr(appliance.subprocess, "run", run)
    appliance.compose_up(row, {"create": True, "paused": True})
    appliance.compose_down(row)
    (up, env), (down, _) = calls
    files = [str(appliance.COMPOSE_FILE), str(appliance.world_dir(row["session_id"]) / "compose.json")]
    assert up == ["docker", "compose", "-p", row["compose_project"], "-f", files[0], "-f", files[1],
                  "up", "-d", "--force-recreate"]
    assert down[-1] == "down" and down[:3] == ["docker", "compose", "-p"]
    assert env["FACTORIO_CONTAINER"] == row["container_name"]


def test_lab_config_files_exist():
    for name in ("lab-server-settings.json", "lab-map-gen-settings.json", "config-camera.ini"):
        assert (appliance.APPLIANCE_DIR / "config" / name).is_file()


def test_stage_save_copies_a_host_zip_into_the_lab(tmp_path):
    row = make_world()
    src = tmp_path / "base+cam.zip"
    src.write_bytes(b"zip")
    assert appliance.stage_save(row, str(src)) == "base+cam"
    assert (appliance.saves_dir(row) / "base+cam.zip").read_bytes() == b"zip"


# ---- acquire --------------------------------------------------------------------

@pytest.fixture()
def compose(monkeypatch):
    seen = {"up": [], "down": [], "running": set()}

    def up(row, boot=None):
        appliance.write_override(row, boot)
        seen["up"].append((row, boot))
        seen["running"].add(row["compose_project"])
    monkeypatch.setattr(appliance, "compose_up", up)
    monkeypatch.setattr(appliance, "compose_down", lambda row: seen["down"].append(row))
    monkeypatch.setattr(appliance, "is_container_running",
                        lambda row: row["compose_project"] in seen["running"])
    monkeypatch.setattr(appliance, "wait_ready", lambda row: {"current_world": None})
    monkeypatch.setattr(appliance, "stage_save", lambda row, save: save)
    return seen


def test_acquire_defaults_to_the_headless_realm(compose):
    out = call("acquire", game="factorio")
    assert (out["kind"], out["headless"], out["rules"], out["name"]) == \
        ("game", True, "realm", "realm")
    assert "game_port" not in out
    row, boot = compose["up"][0]
    assert row["compose_project"] == appliance.PROJECT and boot == {"paused": False, "create": False}
    again = call("acquire", game="factorio")
    assert again == {**out, "adopted": True} and len(compose["up"]) == 1


def test_acquire_a_lab(compose):
    out = call("acquire", game="factorio", kind="lab", name="t-s", save="base")
    assert out["rules"] == "sandbox" and out["saves_dir"].endswith("/saves")
    row, boot = compose["up"][0]
    assert boot == {"paused": True, "create": False, "save": "base"}
    assert json.loads(row["mods"]) == appliance.world_mods("lab")
    assert row["container_name"] == "rlm-factorio-lab-t-s-appliance"


@pytest.mark.parametrize("args,match", [
    ({"kind": "lab", "save": "x"}, "a lab needs a name"),
    ({"kind": "lab", "name": "a"}, "boots on a save"),
    ({"kind": "lab", "name": "a", "save": "x", "create": True}, "not both"),
])
def test_acquire_refuses(compose, args, match):
    with pytest.raises(ValueError, match=match):
        call("acquire", game="factorio", **args)
    assert compose["up"] == []


def test_acquire_an_attached_sandbox_lab(compose):
    out = call("acquire", game="factorio", kind="lab", name="w", save="base",
               headless=False)
    assert out["rules"] == "sandbox" and out["headless"] is False
    row = ha.dao.FactorioDAO().get_session(out["session_id"])
    assert out["game_port"] == row["game_port"] != appliance.GAME_PORT
    compose_json = appliance.world_dir(row["session_id"]) / "compose.json"
    ports = json.loads(compose_json.read_text())["services"]["factorio"]["ports"]
    assert ports == [f"{row['game_port']}:12140/udp"]


@pytest.mark.parametrize("again,adopts", [(None, True), (False, True), (True, False)])
def test_acquire_adopts_unless_the_headless_toggle_mismatches(compose, again, adopts):
    first = call("acquire", game="factorio", kind="lab", name="w", create=True,
                 headless=False)
    args = {} if again is None else {"headless": again}
    if adopts:
        out = call("acquire", game="factorio", kind="lab", name="w", create=True, **args)
        assert out == {**first, "adopted": True}
    else:
        with pytest.raises(ValueError, match="live with headless=False"):
            call("acquire", game="factorio", kind="lab", name="w", create=True, **args)
    assert len(compose["up"]) == 1


def test_acquire_restarts_a_world_whose_container_is_gone(compose):
    first = call("acquire", game="factorio")
    compose["running"].clear()
    second = call("acquire", game="factorio", headless=False)
    assert second["session_id"] != first["session_id"] and second["game_port"]
    assert ha.dao.FactorioDAO().get_session(first["session_id"])["status"] == "stopped"


def test_lab_cap_refuses_at_once(compose, monkeypatch):
    monkeypatch.setattr(ha, "MAX_LABS", 2)
    a = call("acquire", game="factorio", kind="lab", name="a", create=True)
    b = call("acquire", game="factorio", kind="lab", name="b", create=True)
    call("acquire", game="factorio")
    with pytest.raises(appliance.ApplianceError, match="LAB_CAP"):
        call("acquire", game="factorio", kind="lab", name="c", create=True)
    assert len(compose["up"]) == 3
    rows = {r["session_id"]: r for r in ha.dao.FactorioDAO().live_sessions()}
    assert rows[a["session_id"]]["control_port"] != rows[b["session_id"]]["control_port"]


def test_failed_bring_up_marks_error_and_tears_down(compose, monkeypatch):
    def fail(row):
        raise appliance.ApplianceError("no engine")
    monkeypatch.setattr(appliance, "wait_ready", fail)
    with pytest.raises(appliance.ApplianceError, match="no engine"):
        call("acquire", game="factorio", kind="lab", name="a", create=True)
    assert len(compose["down"]) == 1
    assert ha.dao.FactorioDAO().live_sessions() == []


def test_release_a_lab(compose, engine, monkeypatch):
    engine()
    dropped = []
    monkeypatch.setattr(ha.broker, "drop", dropped.append)
    sid = call("acquire", game="factorio", kind="lab", name="a", create=True)["session_id"]
    assert call("release", session_id=sid)["released"]
    assert dropped == ["rlm-factorio-lab-a-appliance"]
    assert ha.dao.FactorioDAO().get_session(sid)["status"] == "stopped"


# ---- supervisor verbs on a lab ------------------------------------------------------

@pytest.fixture()
def supervisor(monkeypatch):
    posts = []

    class Resp:
        status_code = 200

        def json(self):
            return {"ok": True, "result": {"paused": True, "saved": "x", "world": "x"}}
    monkeypatch.setattr(ha.appliance.httpx, "post",
                        lambda url, json, timeout: posts.append((url, json)) or Resp())
    return posts


@pytest.mark.parametrize("verb,args,path", [
    ("pause", {"paused": True}, "/pause"), ("world_save", {"name": "x"}, "/save"),
    ("world_load", {"name": "x"}, "/load"),
])
def test_a_lab_reaches_its_own_supervisor(LAB, engine, supervisor, verb, args, path):
    engine()
    call(verb, session_id=LAB, **args)
    port = ha.dao.FactorioDAO().get_session(LAB)["control_port"]
    assert [url for url, _ in supervisor] == [f"http://127.0.0.1:{port}{path}"]


def test_observe_events_on_a_lab(LAB, engine):
    engine()
    ha._fire(LAB, "x", {"a": 1})
    assert call("observe_events", session_id=LAB)["events"][0] == {"kind": "x", "data": {"a": 1}}


def test_exec_lua_runs_on_a_lab(LAB, engine):
    b = engine()
    b.respond = lambda code: "123"
    out = call("exec_lua", session_id=LAB, code="rcon.print(game.tick)")
    assert out["output"] == "123" and out["seat"] is None


def test_lab_mod_version_is_not_cached_for_long(LAB, engine, monkeypatch):
    b = engine()
    clock = [100.0]
    monkeypatch.setattr(ha.time, "monotonic", lambda: clock[0])
    row = ha.dao.FactorioDAO().get_session(LAB)
    ha._mod_version(row)
    clock[0] += 3
    ha._mod_version(row)
    assert len([c for c in b.sent if "active_mods" in c]) == 1
    clock[0] += 3
    ha._mod_version(row)
    assert len([c for c in b.sent if "active_mods" in c]) == 2


# ---- seats on every kind -------------------------------------------------------------

@pytest.fixture()
def seats(monkeypatch):
    runs = []
    monkeypatch.setattr(appliance, "seat_run",
                        lambda container, network, name, **kw: runs.append(
                            {"container": container, "network": network, "name": name, **kw}))
    monkeypatch.setattr(appliance, "seat_container_running", lambda c: True)
    monkeypatch.setattr(appliance, "seat_stop", lambda c: None)
    return runs


def seat_engine(engine):
    b = engine()
    b.respond = lambda code: "3" if "game.players[" in code and "connected" in code \
        else json.dumps({"ok": True})
    return b


def test_a_player_seat_on_a_lab_loads_the_lab_mods_and_spawns(LAB, engine, seats):
    b = seat_engine(engine)
    out = call("join", session_id=LAB)
    assert out["role"] == "player" and out["player_index"] == 3
    assert seats[0]["mods"] == appliance.world_mods("lab")
    assert seats[0]["network"] == "rlm-factorio-lab-t_default"
    assert b.calls("spawn") and not b.calls("view_booth")
    b.respond = lambda code: json.dumps({"ok": True})
    call("move", seat_id=out["seat_id"], x=1, y=2)
    assert f'"seat": "{out["player_name"]}"' in b.calls("set_target")[0]


def test_a_camera_seat_parks_in_the_booth(LAB, engine, seats):
    b = seat_engine(engine)
    out = call("join", session_id=LAB, role="camera")
    assert out["player_name"] == ha.CAMERA_NAME and seats[0]["role"] == "camera"
    assert "remote.call('sbx','view_booth'" in b.calls("view_booth")[0]
    assert not b.calls("spawn")
    with pytest.raises(ValueError, match="already has a camera"):
        call("join", session_id=LAB, role="camera")


def test_a_camera_retries_until_the_player_exists(LAB, engine, seats, monkeypatch):
    b = seat_engine(engine)
    replies = [{"ok": False, "error": "not_found"}, {"ok": True}]
    base = b.respond
    b.respond = lambda code: json.dumps(replies.pop(0)) if "view_booth" in code else base(code)
    monkeypatch.setattr(ha.time, "sleep", lambda s: None)
    call("join", session_id=LAB, role="camera")
    assert len(b.calls("view_booth")) == 2


def test_no_camera_under_realm_rules(engine, seats):
    seat_engine(engine)
    realm = make_world("game", "realm")["session_id"]
    with pytest.raises(appliance.ApplianceError, match="refused under realm rules: a camera seat"):
        call("join", session_id=realm, role="camera")
    assert seats == []


def test_reaper_leaves_an_idle_camera(LAB, engine, seats, monkeypatch):
    seat_engine(engine)
    cam = call("join", session_id=LAB, role="camera")
    player = call("join", session_id=LAB)
    monkeypatch.setattr(ha, "_age_s", lambda stamp: 10 ** 6)
    d = ha.dao.FactorioDAO()
    assert ha._reap_reason(d.get_seat(cam["seat_id"])) is None
    assert ha._reap_reason(d.get_seat(player["seat_id"])).startswith("idle")


# ---- keys and throttles per world and per caller ---------------------------------------

def test_seatless_calls_key_by_world_and_caller(LAB, engine):
    b = engine()
    ha.HANDLERS["exec_lua"]({"session_id": LAB, "code": "rcon.print(1)"}, "agent-1")
    ha.HANDLERS["exec_lua"]({"session_id": LAB, "code": "rcon.print(1)"}, "agent-2")
    call("sbx", session_id=LAB, verb="sandbox_list")
    container = "rlm-factorio-lab-t-appliance"
    assert b.conns == [f"{container}/agent-1", f"{container}/agent-2", f"{container}/anon"]
    keys = ha._throttle({"container_name": container}).snapshot()["keys"]
    assert set(keys) == {f"{container}/agent-1", f"{container}/agent-2", f"{container}/anon"}


def test_each_world_has_its_own_throttle(LAB, engine):
    engine()
    realm = make_world("game", "realm")["session_id"]
    call("exec_lua", session_id=LAB, code="rcon.print(1)")
    call("exec_lua", session_id=realm, code="rcon.print(1)")
    assert set(ha._THROTTLES) == {"rlm-factorio-lab-t-appliance", appliance.CONTAINER}
    assert set(call("load")["worlds"]) == {LAB, realm}


def test_ups_is_sampled_per_world_and_a_paused_world_is_no_overload(LAB, engine):
    b = engine()
    realm = make_world("game", "realm")
    b.respond = lambda code: "100 true"
    ha._sample_ups()
    ha._sample_ups()
    for row in (ha.dao.FactorioDAO().get_session(LAB), realm):
        assert ha._throttle(row).ups() is None
        assert ha._throttle(row)._threshold()[0] == 0.0


def test_idle_seatless_sockets_close(LAB, engine, monkeypatch):
    b = engine()
    clock = [1000.0]
    monkeypatch.setattr(ha.time, "monotonic", lambda: clock[0])
    call("exec_lua", session_id=LAB, code="rcon.print(1)")
    assert ha._close_idle_conns() == []
    clock[0] += ha.CONN_IDLE_S + 1
    assert ha._close_idle_conns() == ["rlm-factorio-lab-t-appliance/anon"]
    assert b.closed == ["rlm-factorio-lab-t-appliance/anon"] and ha._CONN_USED == {}


def test_sandbox_rules_never_refuse_on_the_command_budget(LAB, engine):
    b = engine()
    b.cost = 100.0
    out = call("exec_lua", session_id=LAB, code="rcon.print(1)")
    assert "error" not in out and out["cost_ms"] == 100.0
    realm = make_world("game", "realm")["session_id"]
    assert call("exec_lua", session_id=realm, code="rcon.print(1)")["error"].startswith("OVER_BUDGET")


# ---- advance and wait_until ----------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += max(s, 0.25)


@pytest.fixture()
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(ha.time, "monotonic", c.monotonic)
    monkeypatch.setattr(ha.time, "sleep", c.sleep)
    return c


def ticking_engine(engine, step=300):
    b = engine()
    state = {"tick": 0, "paused": True}

    def respond(code):
        if "tick_paused = false" in code:
            state["paused"] = False
        if "tick_paused = true" in code:
            state["paused"] = True
        if "rcon.print(game.tick)" in code:
            if not state["paused"]:
                state["tick"] += step
            return str(state["tick"])
        return ""
    b.respond = respond
    return b, state


def test_advance_runs_then_pauses(LAB, engine, clock):
    b, state = ticking_engine(engine)
    out = call("advance", session_id=LAB, ticks=1200)
    assert out["ticks"] >= 1200 and state["paused"] and "error" not in out
    assert any("game.speed = 1000" in c for c in b.sent)


def test_advance_refused_under_realm_rules(engine, clock):
    b = engine()
    realm = make_world("game", "realm")["session_id"]
    with pytest.raises(appliance.ApplianceError, match="refused under realm rules: advance"):
        call("advance", session_id=realm, ticks=60)
    assert b.sent == []


def test_advance_times_out_paused(LAB, engine, clock):
    b, state = ticking_engine(engine, step=1)
    out = call("advance", session_id=LAB, ticks=10 ** 6, timeout_s=5)
    assert out["error"] == "TIMEOUT" and state["paused"]


def ghost_engine(engine, remaining):
    b = engine()
    b.respond = lambda code: json.dumps({"remaining": remaining.pop(0) if len(remaining) > 1
                                         else remaining[0]})
    return b


def test_wait_until_ghosts_built(LAB, engine, clock):
    ghost_engine(engine, [3, 1, 0])
    out = call("wait_until", session_id=LAB, timeout_s=60,
               condition={"ghosts_built": {"area": [[0, 0], [10, 10]]}})
    assert out["met"] and out["state"] == {"remaining": 0} and out["waited_s"] == 4.0


def test_wait_until_times_out_with_the_last_state_and_the_clamp(LAB, engine, clock, monkeypatch):
    monkeypatch.setattr(ha, "WAIT_MAX_S", 30.0)
    ghost_engine(engine, [2])
    out = call("wait_until", session_id=LAB, timeout_s=5000,
               condition={"ghosts_built": {"ids": [1, 2]}})
    assert out["error"] == "TIMEOUT" and out["timeout_s"] == 30.0
    assert out["state"] == {"remaining": 2} and out["waited_s"] <= 30.0


def test_wait_until_needs_a_timeout(LAB):
    with pytest.raises(ValueError, match="needs timeout_s"):
        call("wait_until", session_id=LAB, condition={"ghosts_built": {"ids": [1]}})


@pytest.mark.parametrize("condition", [
    {}, {"ghosts_built": {}}, {"entities_working": {"area": 1, "unit_numbers": [1]}},
    {"tool": "x", "ghosts_built": {"ids": [1]}},
])
def test_wait_until_condition_shape(LAB, condition):
    with pytest.raises(ValueError, match="argument condition"):
        call("wait_until", session_id=LAB, timeout_s=5, condition=condition)


def test_wait_until_entities_working_for_a_while(LAB, engine, clock):
    b = engine()
    b.respond = lambda code: json.dumps({"total": 2, "working": 2, "idle": {}, "missing": {}})
    out = call("wait_until", session_id=LAB, timeout_s=60,
               condition={"entities_working": {"unit_numbers": [4, 5]}, "for_s": 6})
    assert out["met"] and out["waited_s"] >= 6 and out["state"]["idle"] == []


def test_wait_until_a_read_tool_predicate(LAB, engine, clock):
    b = engine()
    counts = [1, 2, 5]

    def respond(code):
        if "active_mods" in code:
            return "0.6.1"
        if "'tool_list'" in code:
            return json.dumps({"tools": [{"name": "count", "effect": "read"},
                                         {"name": "spawn", "effect": "cheat"}]})
        return json.dumps({"ok": True, "r": {"n": counts.pop(0) if len(counts) > 1 else counts[0]}})
    b.respond = respond
    out = call("wait_until", session_id=LAB, timeout_s=60,
               condition={"tool": "count", "path": "r.n", "op": "ge", "value": 5})
    assert out["met"] and out["state"]["r"]["n"] == 5
    with pytest.raises(ValueError, match="read tools only"):
        call("wait_until", session_id=LAB, timeout_s=5, condition={"tool": "spawn"})


def test_wait_until_ends_when_the_world_is_released(LAB, engine, clock):
    b = engine()

    def respond(code):
        ha.dao.FactorioDAO().set_status(LAB, "stopped")
        return json.dumps({"remaining": 1})
    b.respond = respond
    with pytest.raises(appliance.ApplianceError, match="released mid-wait"):
        call("wait_until", session_id=LAB, timeout_s=60,
             condition={"ghosts_built": {"ids": [1]}})


# ---- seatless blueprint stamps -----------------------------------------------------------

def stamp_bodies(b):
    out = []
    for c in b.sent:
        m = re.search(r"remote\.call\('game_bot','(bp_put|blueprint_stamp)',h\.json_to_table\('(.*)'\)", c)
        if m:
            out.append((m[1], json.loads(m[2])))
    return out


def test_lab_stamp_uses_a_short_hashed_upload_id_and_a_surface(LAB, engine):
    b = engine()
    call("blueprint_stamp", session_id=LAB, x=1, y=2, blueprint="0abc")
    sent = stamp_bodies(b)
    assert [v for v, _ in sent] == ["bp_put", "blueprint_stamp"]
    ids = {body["id"] for _, body in sent}
    assert len(ids) == 1 and len(ids.pop()) <= 64
    stamp = sent[1][1]
    assert stamp["surface"] == "nauvis" and "seat" not in stamp


def test_lab_stamp_passes_surface_force_and_built_mode(LAB, engine):
    b = engine()
    call("blueprint_stamp", session_id=LAB, x=1, y=2, blueprint="0abc",
         surface="fx", force="enemy", build_mode="built")
    stamp = stamp_bodies(b)[-1][1]
    assert stamp["surface"] == "fx" and stamp["force"] == "enemy" and stamp["build"] is True
    with pytest.raises(appliance.ApplianceError, match="refused"):
        call("blueprint_stamp", session_id=LAB, x=1, y=2, blueprint="0abc",
             build=True, rules="realm")


def test_realm_stamp_needs_a_seat(engine):
    engine()
    realm = make_world("game", "realm")["session_id"]
    with pytest.raises(ValueError, match="needs a seat"):
        call("blueprint_stamp", session_id=realm, x=1, y=2, blueprint="0abc")


# ---- realm rules ------------------------------------------------------------

class FakeDao:
    def touch_seat(self, seat_id):
        pass


@pytest.fixture()
def realm(monkeypatch, engine):
    """A realm session with one seat, no DB."""
    b = engine()
    row = {"session_id": "s1", "container_name": "c1"}
    seat = {"seat_id": "x1", "player_name": "p1"}
    monkeypatch.setattr(ha, "_resolve", lambda args, need_seat=True: (row, seat))
    monkeypatch.setattr(ha.dao, "FactorioDAO", FakeDao)
    return b


@pytest.mark.parametrize("verb,args", [
    ("teleport", {"x": 1, "y": 2}),
    ("research", {"name": "automation"}),
    ("blueprint_stamp", {"x": 1, "y": 2, "blueprint": "0abc", "build": True}),
    ("blueprint_stamp", {"x": 1, "y": 2, "blueprint": "0abc", "build_mode": "built"}),
])
def test_realm_rules_refuse(realm, verb, args):
    with pytest.raises(appliance.ApplianceError, match="refused under realm rules"):
        call(verb, session_id="s1", **args)
    assert realm.sent == []


def test_realm_ghost_stamp_allowed(realm):
    realm.respond = lambda code: json.dumps({"ok": True, "placed": 1})
    out = call("blueprint_stamp", session_id="s1", x=1, y=2, blueprint="0abc")
    assert out["placed"] == 1


def test_admin_token_lifts_refusal(realm, tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("s3cret\n")
    monkeypatch.setenv("RLM_FACTORIO_ADMIN_TOKEN_FILE", str(token))
    realm.respond = lambda code: json.dumps({"ok": True})
    assert call("teleport", session_id="s1", x=1, y=2, admin_token="s3cret")["ok"]
    assert len(realm.calls("teleport")) == 1
    with pytest.raises(appliance.ApplianceError, match="refused under realm rules: teleport"):
        call("teleport", session_id="s1", x=1, y=2, admin_token="wrong")
    assert len(realm.calls("teleport")) == 1
    assert "admin_token" not in " ".join(realm.sent)


def test_sandbox_rules_on_a_realm_row_still_refuse_teleport(realm):
    with pytest.raises(appliance.ApplianceError, match="refused under realm rules: teleport"):
        call("teleport", session_id="s1", x=1, y=2, rules="sandbox")
    assert realm.sent == []


def test_no_token_file_means_no_override(realm):
    with pytest.raises(appliance.ApplianceError, match="refused"):
        call("research", session_id="s1", name="automation", admin_token="")


def test_empty_token_file_means_no_override(realm, tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("\n")
    monkeypatch.setenv("RLM_FACTORIO_ADMIN_TOKEN_FILE", str(token))
    with pytest.raises(appliance.ApplianceError, match="refused"):
        call("research", session_id="s1", name="automation", admin_token="")


def test_lab_runs_sandbox_rules_unless_asked(LAB, engine):
    b = engine()
    assert call("research", session_id=LAB, name="automation")["ok"]
    with pytest.raises(appliance.ApplianceError, match="refused under realm rules: research"):
        call("research", session_id=LAB, name="automation", rules="realm")
    assert len(b.calls("research")) == 1


def test_bad_rules_value_refused_by_validation(LAB):
    with pytest.raises(ValueError, match="argument rules"):
        call("research", session_id=LAB, name="automation", rules="god")


# ---- sbx --------------------------------------------------------------------

def test_sbx_calls_the_lab_mod(LAB, engine):
    b = engine()
    b.respond = lambda code: json.dumps({"ok": True, "sandboxes": []})
    assert call("sbx", session_id=LAB, verb="sandbox_list")["ok"]
    assert "remote.call('sbx','sandbox_list'" in b.sent[0]


REFUSED_SBX = ["research", "items", "evo", "nest", "wave", "body", "body_tp",
               "slice_build", "slice_inplace", "slice_reset", "slice_reset_at",
               "slice_drop", "sandbox_new", "sandbox_drop", "no_such_verb"]


@pytest.mark.parametrize("verb,args", [(v, {}) for v in REFUSED_SBX]
                         + [("sbx_mode", {"mode": "god"}), ("sbx_mode", {})])
def test_sbx_outside_the_allowlist_refused_under_realm_rules(LAB, engine, verb, args):
    b = engine()
    with pytest.raises(appliance.ApplianceError, match="refused under realm rules: sbx"):
        call("sbx", session_id=LAB, verb=verb, args=args, rules="realm")
    assert b.sent == []


@pytest.mark.parametrize("verb", REFUSED_SBX)
def test_sbx_anything_allowed_in_sandbox_rules(LAB, engine, verb):
    b = engine()
    assert call("sbx", session_id=LAB, verb=verb, args={"name": "a"})["ok"]
    assert len(b.calls(verb)) == 1


def test_sbx_god_mode_allowed_in_sandbox_rules(LAB, engine):
    b = engine()
    assert call("sbx", session_id=LAB, verb="sbx_mode", args={"mode": "god"})["ok"]
    assert len(b.calls("sbx_mode")) == 1


@pytest.mark.parametrize("verb", sorted(ha.SBX_REALM_VERBS))
def test_sbx_allowlist_passes_under_realm_rules(LAB, engine, verb):
    b = engine()
    call("sbx", session_id=LAB, verb=verb, rules="realm")
    call("sbx", session_id=LAB, verb="sbx_mode", args={"mode": "standard"}, rules="realm")
    assert len(b.calls(verb)) == 1 and len(b.calls("sbx_mode")) == 1


def test_sbx_admin_token_lifts_the_allowlist(LAB, engine, tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("s3cret\n")
    monkeypatch.setenv("RLM_FACTORIO_ADMIN_TOKEN_FILE", str(token))
    b = engine()
    call("sbx", session_id=LAB, verb="slice_build", rules="realm", admin_token="s3cret")
    assert len(b.calls("slice_build")) == 1
    with pytest.raises(appliance.ApplianceError, match="refused"):
        call("sbx", session_id=LAB, verb="slice_build", rules="realm", admin_token="x")
    assert len(b.calls("slice_build")) == 1


# ---- the 0.6 gate and the tool verbs -----------------------------------------

@pytest.mark.parametrize("verb,args", [
    ("tool", {"name": "look"}), ("tool_list", {}), ("tool_sync", {}),
])
def test_tool_verbs_need_mod_06(LAB, engine, verb, args):
    b = engine(version="0.5.0")
    with pytest.raises(appliance.ApplianceError, match="needs game-bot-control 0.6"):
        call(verb, session_id=LAB, **args)
    assert len(b.sent) == 1 and "active_mods" in b.sent[0]


def test_tool_passes_name_and_args(LAB, engine):
    b = engine()
    b.respond = lambda code: ("0.6.1" if "active_mods" in code
                              else json.dumps({"ok": True, "r": {"n": 1}}))
    out = call("tool", session_id=LAB, name="look", args={"x0": 1})
    assert out == {"ok": True, "r": {"n": 1}}
    assert "'tool_call'" in b.sent[-1] and '\\"name\\"' not in b.sent[-1]
    assert '"name": "look"' in b.sent[-1] and '"x0": 1' in b.sent[-1]


def tool_calls(b):
    return [json.loads(re.search(r"h\.json_to_table\('(.*)'\)", c).group(1)
                       .replace("\\\\", "\\").replace("\\'", "'"))
            for c in b.calls("tool_call")]


def test_tool_under_realm_rules_passes_deny_and_never_lists(LAB, engine):
    b = engine()
    call("tool", session_id=LAB, name="look", rules="realm")
    call("tool", session_id=LAB, name="look")
    sent = tool_calls(b)
    assert sent[0]["deny"] == ["cheat"] and "deny" not in sent[1]
    assert not b.calls("tool_list") and not hasattr(ha, "_TOOL_EFFECT_CACHE")


def test_denied_tool_maps_to_a_realm_rules_refusal(LAB, engine):
    def respond(code):
        if "active_mods" in code:
            return "0.6.1"
        return json.dumps({"ok": False, "error": "denied",
                           "detail": "cheat tool geom refused"})
    b = engine()
    b.respond = respond
    with pytest.raises(appliance.ApplianceError,
                       match="refused under realm rules: cheat tool geom$"):
        call("tool", session_id=LAB, name="look", rules="realm")


def test_deny_is_enforced_by_the_mod_so_sandbox_rules_run_the_tool(LAB, engine):
    """The fake mod refuses a cheat-using tool only when `deny` names cheat."""
    def respond(code):
        if "active_mods" in code:
            return "0.6.1"
        denied = "cheat" in code and '"deny"' in code
        return json.dumps({"ok": False, "error": "denied", "detail": "cheat tool g refused"}
                          if denied else {"ok": True, "r": 1})
    b = engine()
    b.respond = respond
    with pytest.raises(appliance.ApplianceError, match="cheat tool g"):
        call("tool", session_id=LAB, name="reader", rules="realm")
    assert call("tool", session_id=LAB, name="reader")["r"] == 1


def test_realm_rules_refuse_a_mod_that_cannot_deny(LAB, engine):
    b = engine(version="0.6.0")
    with pytest.raises(appliance.ApplianceError, match="0.6.1"):
        call("tool", session_id=LAB, name="look", rules="realm")
    assert not b.calls("tool_call")
    call("tool", session_id=LAB, name="look")
    assert len(b.calls("tool_call")) == 1


def test_tool_list_keeps_an_empty_registry_a_list(LAB, engine):
    b = engine()
    b.respond = lambda code: "0.6.1" if "active_mods" in code else '{"tools":{}}'
    assert call("tool_list", session_id=LAB)["tools"] == []


def write_tool(root, group, name, header):
    path = root / group / f"{name}.lua"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"--- {k}: {v}\n" for k, v in header.items()) + "return {}\n")


def header(name, kind="call", effect="read"):
    return {"tool": name, "kind": kind, "effect": effect, "summary": "s",
            "args": "none", "returns": "{}"}


def test_tool_sync_defines_libs_first_and_reports_errors(LAB, engine, tmp_path, monkeypatch):
    root = tmp_path / "tools"
    write_tool(root, "read", "a_call", {**header("a_call"), "uses": "geom"})
    write_tool(root, "lib", "geom", header("geom", kind="lib"))
    write_tool(root, "read", "no_args", {k: v for k, v in header("no_args").items()
                                         if k != "args"})
    write_tool(root, "read", "wrong", header("other"))
    (root / "read" / "notes.txt").write_text("ignored")
    monkeypatch.setenv("RLM_FACTORIO_TOOLS", str(root))
    defines = []
    b = engine(calls=defines)
    out = call("tool_sync", session_id=LAB)
    assert out["defined"] == ["geom", "a_call"]
    errors = {e["name"]: e["error"] for e in out["errors"]}
    assert set(errors) == {"no_args", "wrong"}
    assert "missing header key args" in errors["no_args"]
    assert "does not match the file name" in errors["wrong"]
    order = [re.search(r'"name": "(\w+)"', c).group(1) for c in defines]
    assert order == ["geom", "a_call"]
    assert '"kind": "lib"' in defines[0] and '"effect": "read"' in defines[1]


def test_tool_sync_names_and_define_failure(LAB, engine, tmp_path, monkeypatch):
    root = tmp_path / "tools"
    write_tool(root, "g", "good", header("good"))
    write_tool(root, "g", "bad", header("bad"))
    monkeypatch.setenv("RLM_FACTORIO_TOOLS", str(root))

    def respond(code):
        if "active_mods" in code:
            return "0.6.1"
        return "Cannot execute command. Error: compile error" if '"bad"' in code \
            else json.dumps({"name": "good", "size": 1})
    b = engine()
    b.respond = respond
    out = call("tool_sync", session_id=LAB)
    assert out["defined"] == ["good"]
    assert [e["name"] for e in out["errors"]] == ["bad"]
    assert "compile error" in out["errors"][0]["error"]
    out = call("tool_sync", session_id=LAB, names=["good", "ghost"])
    assert out["defined"] == ["good"]
    assert out["errors"][0]["name"] == "ghost"


def test_tool_sync_missing_directory_is_empty(LAB, engine, tmp_path, monkeypatch):
    monkeypatch.setenv("RLM_FACTORIO_TOOLS", str(tmp_path / "absent"))
    engine()
    assert call("tool_sync", session_id=LAB) == {"defined": [], "errors": []}


def define_order(calls):
    return [re.search(r'"name": "(\w+)"', c).group(1) for c in calls]


def test_tool_sync_orders_libs_by_uses(LAB, engine, tmp_path, monkeypatch):
    root = tmp_path / "tools"
    write_tool(root, "a", "b", {**header("b", kind="lib"), "uses": "a"})
    write_tool(root, "z", "a", header("a", kind="lib"))
    write_tool(root, "a", "caller", {**header("caller"), "uses": "b"})
    monkeypatch.setenv("RLM_FACTORIO_TOOLS", str(root))
    defines = []
    engine(calls=defines)
    out = call("tool_sync", session_id=LAB)
    assert define_order(defines) == ["a", "b", "caller"]
    assert out["defined"] == ["a", "b", "caller"] and out["errors"] == []


def test_tool_sync_reports_a_lib_cycle(LAB, engine, tmp_path, monkeypatch):
    root = tmp_path / "tools"
    write_tool(root, "l", "p", {**header("p", kind="lib"), "uses": "q"})
    write_tool(root, "l", "q", {**header("q", kind="lib"), "uses": "p"})
    write_tool(root, "l", "dependent", {**header("dependent", kind="lib"), "uses": "p"})
    write_tool(root, "l", "fine", header("fine", kind="lib"))
    monkeypatch.setenv("RLM_FACTORIO_TOOLS", str(root))
    defines = []
    engine(calls=defines)
    out = call("tool_sync", session_id=LAB)
    assert out["defined"] == ["fine"]
    assert {e["name"] for e in out["errors"]} == {"p", "q", "dependent"}
    assert all("cycle" in e["error"] for e in out["errors"])


def test_tool_sync_rejects_a_chain_that_reaches_a_cheat_lib(LAB, engine, tmp_path, monkeypatch):
    root = tmp_path / "tools"
    write_tool(root, "l", "spawner", header("spawner", kind="lib", effect="cheat"))
    write_tool(root, "l", "middle", {**header("middle", kind="lib"), "uses": "spawner"})
    write_tool(root, "t", "reader", {**header("reader"), "uses": "middle"})
    write_tool(root, "t", "conjurer", {**header("conjurer", effect="cheat"), "uses": "middle"})
    write_tool(root, "t", "plain", header("plain"))
    monkeypatch.setenv("RLM_FACTORIO_TOOLS", str(root))
    defines = []
    engine(calls=defines)
    out = call("tool_sync", session_id=LAB)
    assert sorted(out["defined"]) == ["conjurer", "plain", "spawner"]
    errors = {e["name"]: e["error"] for e in out["errors"]}
    assert set(errors) == {"middle", "reader"}
    assert "cheat lib spawner" in errors["reader"]


def test_tool_sync_survives_an_unreadable_file(LAB, engine, tmp_path, monkeypatch):
    root = tmp_path / "tools"
    write_tool(root, "g", "good", header("good"))
    (root / "g").mkdir(exist_ok=True)
    (root / "g" / "binary.lua").write_bytes(b"\xff\xfe\x00 not utf-8")
    monkeypatch.setenv("RLM_FACTORIO_TOOLS", str(root))
    engine()
    out = call("tool_sync", session_id=LAB)
    assert out["defined"] == ["good"]
    assert [e["name"] for e in out["errors"]] == ["binary"]
    assert "cannot read" in out["errors"][0]["error"]


# ---- the exec log ------------------------------------------------------------

def test_exec_lua_log_line(LAB, engine, tmp_path):
    engine()
    code = "rcon.print('x')" + "-" * 300
    call("exec_lua", session_id=LAB, code=code)
    lines = (tmp_path / "exec.jsonl").read_text().splitlines()
    rec = json.loads(lines[0])
    assert rec["session_id"] == LAB and rec["seat_id"] is None
    assert rec["size"] == len(code) and rec["head"] == code[:200]
    assert rec["sha1"] == hashlib.sha1(code.encode()).hexdigest()
    assert rec["time"]
    call("exec_lua", session_id=LAB, code="rcon.print(1)")
    assert len((tmp_path / "exec.jsonl").read_text().splitlines()) == 2


def test_exec_log_failure_never_fails_the_verb(LAB, engine, tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setenv("RLM_FACTORIO_EXEC_LOG", str(blocker / "sub" / "log.jsonl"))
    engine()
    with caplog.at_level("WARNING"):
        assert "output" in call("exec_lua", session_id=LAB, code="rcon.print(1)")
    assert any(r.levelname == "WARNING" and "exec log" in r.message for r in caplog.records)


def test_exec_log_creates_parent_dir(LAB, engine, tmp_path, monkeypatch):
    target = tmp_path / "deep" / "er" / "log.jsonl"
    monkeypatch.setenv("RLM_FACTORIO_EXEC_LOG", str(target))
    engine()
    call("exec_lua", session_id=LAB, code="rcon.print(1)")
    assert target.exists()
