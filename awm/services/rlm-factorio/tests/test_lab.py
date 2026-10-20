"""Lab targets, realm rules, the exec log and the 0.6 tool verbs, against fakes."""

import hashlib
import json
import re

import pytest

from awm.rlm_factorio import appliance, hub_adapter as ha, throttle

LAB = "lab:fxlab-t-s"
LAB_INFO = {"State": {"Running": True}, "Config": {"Labels": {"fxlab": "1"}}}


class RespondingBroker:
    """Answers each RCON command from a responder(code) -> output."""

    def __init__(self, respond):
        self.respond, self.sent = respond, []

    def lua(self, conn, code, *, timeout):
        self.sent.append(code)
        return {"output": self.respond(code), "cost_ms": 0.1}

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
    monkeypatch.setattr(ha, "THROTTLE", throttle.Throttle())
    monkeypatch.setattr(ha, "_docker_inspect", lambda c: LAB_INFO)
    ha._MOD_VERSIONS.clear()


@pytest.fixture()
def engine(monkeypatch):
    def install(**kw):
        b = RespondingBroker(mod_responder(**kw))
        monkeypatch.setattr(ha.broker, "for_row", lambda row: b)
        return b
    return install


def call(handler, /, **args):
    return ha.HANDLERS[handler](args)


# ---- the lab: target -------------------------------------------------------

def test_lab_row_is_synthetic():
    row, seat = ha._resolve({"session_id": LAB}, need_seat=False)
    assert seat is None
    assert row == {"session_id": LAB, "container_name": "fxlab-t-s",
                   "kind": "lab", "rules": "sandbox"}


def test_lab_without_label_refused(monkeypatch):
    monkeypatch.setattr(ha, "_docker_inspect",
                        lambda c: {"State": {"Running": True}, "Config": {"Labels": {}}})
    with pytest.raises(ValueError, match="labelled fxlab=1"):
        ha._resolve({"session_id": LAB}, need_seat=False)


def test_lab_must_be_running(monkeypatch):
    monkeypatch.setattr(ha, "_docker_inspect",
                        lambda c: {"State": {"Running": False}, "Config": LAB_INFO["Config"]})
    with pytest.raises(ValueError, match="not running"):
        ha._resolve({"session_id": LAB}, need_seat=False)


def test_lab_container_must_exist(monkeypatch):
    monkeypatch.setattr(ha, "_docker_inspect", lambda c: None)
    with pytest.raises(ValueError, match="no such lab container"):
        ha._resolve({"session_id": LAB}, need_seat=False)


@pytest.mark.parametrize("sid", ["lab:rlm-factorio", "lab:-rf", "lab:a b", "lab:"])
def test_realm_and_odd_names_never_inspected(sid, monkeypatch):
    monkeypatch.setattr(ha, "_docker_inspect",
                        lambda c: pytest.fail("inspected " + c))
    with pytest.raises(ValueError, match="not a lab container name"):
        ha._resolve({"session_id": sid}, need_seat=False)


@pytest.mark.parametrize("verb,args", [
    ("move", {"x": 1, "y": 2}), ("mine", {"x": 1, "y": 2, "name": "coal"}),
    ("craft", {"recipe": "pipe"}), ("observe", {}), ("stop", {}),
    ("teleport", {"x": 1, "y": 2}),
])
def test_seat_verbs_refused_in_a_lab(verb, args):
    with pytest.raises(ValueError, match="no seat in a lab"):
        call(verb, session_id=LAB, **args)


def test_seat_id_with_a_lab_refused():
    with pytest.raises(ValueError, match="no seat in a lab"):
        ha._resolve({"session_id": LAB, "seat_id": "x"}, need_seat=False)


def test_exec_lua_runs_on_a_lab(engine):
    b = engine()
    b.respond = lambda code: "123"
    out = call("exec_lua", session_id=LAB, code="rcon.print(game.tick)")
    assert out["output"] == "123" and out["seat"] is None


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


def test_lab_runs_sandbox_rules_unless_asked(engine):
    b = engine()
    assert call("research", session_id=LAB, name="automation")["ok"]
    with pytest.raises(appliance.ApplianceError, match="refused under realm rules: research"):
        call("research", session_id=LAB, name="automation", rules="realm")
    assert len(b.calls("research")) == 1


def test_bad_rules_value_refused_by_validation():
    with pytest.raises(ValueError, match="argument rules"):
        call("research", session_id=LAB, name="automation", rules="god")


# ---- sbx --------------------------------------------------------------------

def test_sbx_is_lab_only(engine):
    engine()
    with pytest.raises(ValueError, match="sbx is lab-only"):
        call("sbx", session_id="s1", verb="sandbox_list")


def test_sbx_calls_the_lab_mod(engine):
    b = engine()
    b.respond = lambda code: json.dumps({"ok": True, "sandboxes": []})
    assert call("sbx", session_id=LAB, verb="sandbox_list")["ok"]
    assert "remote.call('sbx','sandbox_list'" in b.sent[0]


REFUSED_SBX = ["research", "items", "evo", "nest", "wave", "body", "body_tp",
               "slice_build", "slice_inplace", "slice_reset", "slice_reset_at",
               "slice_drop", "sandbox_new", "sandbox_drop", "no_such_verb"]


@pytest.mark.parametrize("verb,args", [(v, {}) for v in REFUSED_SBX]
                         + [("sbx_mode", {"mode": "god"}), ("sbx_mode", {})])
def test_sbx_outside_the_allowlist_refused_under_realm_rules(engine, verb, args):
    b = engine()
    with pytest.raises(appliance.ApplianceError, match="refused under realm rules: sbx"):
        call("sbx", session_id=LAB, verb=verb, args=args, rules="realm")
    assert b.sent == []


@pytest.mark.parametrize("verb", REFUSED_SBX)
def test_sbx_anything_allowed_in_sandbox_rules(engine, verb):
    b = engine()
    assert call("sbx", session_id=LAB, verb=verb, args={"name": "a"})["ok"]
    assert len(b.calls(verb)) == 1


def test_sbx_god_mode_allowed_in_sandbox_rules(engine):
    b = engine()
    assert call("sbx", session_id=LAB, verb="sbx_mode", args={"mode": "god"})["ok"]
    assert len(b.calls("sbx_mode")) == 1


@pytest.mark.parametrize("verb", sorted(ha.SBX_REALM_VERBS))
def test_sbx_allowlist_passes_under_realm_rules(engine, verb):
    b = engine()
    call("sbx", session_id=LAB, verb=verb, rules="realm")
    call("sbx", session_id=LAB, verb="sbx_mode", args={"mode": "standard"}, rules="realm")
    assert len(b.calls(verb)) == 1 and len(b.calls("sbx_mode")) == 1


def test_sbx_admin_token_lifts_the_allowlist(engine, tmp_path, monkeypatch):
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
def test_tool_verbs_need_mod_06(engine, verb, args):
    b = engine(version="0.5.0")
    with pytest.raises(appliance.ApplianceError, match="needs game-bot-control 0.6"):
        call(verb, session_id=LAB, **args)
    assert len(b.sent) == 1 and "active_mods" in b.sent[0]


def test_tool_passes_name_and_args(engine):
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


def test_tool_under_realm_rules_passes_deny_and_never_lists(engine):
    b = engine()
    call("tool", session_id=LAB, name="look", rules="realm")
    call("tool", session_id=LAB, name="look")
    sent = tool_calls(b)
    assert sent[0]["deny"] == ["cheat"] and "deny" not in sent[1]
    assert not b.calls("tool_list") and not hasattr(ha, "_TOOL_EFFECT_CACHE")


def test_denied_tool_maps_to_a_realm_rules_refusal(engine):
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


def test_deny_is_enforced_by_the_mod_so_sandbox_rules_run_the_tool(engine):
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


def test_realm_rules_refuse_a_mod_that_cannot_deny(engine):
    b = engine(version="0.6.0")
    with pytest.raises(appliance.ApplianceError, match="0.6.1"):
        call("tool", session_id=LAB, name="look", rules="realm")
    assert not b.calls("tool_call")
    call("tool", session_id=LAB, name="look")
    assert len(b.calls("tool_call")) == 1


def test_tool_list_keeps_an_empty_registry_a_list(engine):
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


def test_tool_sync_defines_libs_first_and_reports_errors(engine, tmp_path, monkeypatch):
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


def test_tool_sync_names_and_define_failure(engine, tmp_path, monkeypatch):
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


def test_tool_sync_missing_directory_is_empty(engine, tmp_path, monkeypatch):
    monkeypatch.setenv("RLM_FACTORIO_TOOLS", str(tmp_path / "absent"))
    engine()
    assert call("tool_sync", session_id=LAB) == {"defined": [], "errors": []}


def define_order(calls):
    return [re.search(r'"name": "(\w+)"', c).group(1) for c in calls]


def test_tool_sync_orders_libs_by_uses(engine, tmp_path, monkeypatch):
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


def test_tool_sync_reports_a_lib_cycle(engine, tmp_path, monkeypatch):
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


def test_tool_sync_rejects_a_chain_that_reaches_a_cheat_lib(engine, tmp_path, monkeypatch):
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


def test_tool_sync_survives_an_unreadable_file(engine, tmp_path, monkeypatch):
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


# ---- a lab never reaches the realm supervisor ---------------------------------

@pytest.fixture()
def no_supervisor(monkeypatch):
    def boom(*a, **k):
        pytest.fail("reached the realm supervisor")
    monkeypatch.setattr(ha.appliance.httpx, "post", boom)
    monkeypatch.setattr(ha.appliance.httpx, "get", boom)


def test_pause_refused_on_a_lab(engine, no_supervisor):
    b = engine()
    with pytest.raises(appliance.ApplianceError, match="not available on a lab target"):
        call("pause", session_id=LAB)
    with pytest.raises(appliance.ApplianceError, match="not available on a lab target"):
        call("pause", session_id=LAB, paused=True)
    assert b.sent == []


def test_control_url_refuses_a_lab_row():
    with pytest.raises(appliance.ApplianceError, match="not available on a lab target"):
        appliance.control_url({"kind": "lab", "container_name": "fxlab-t-s"})
    assert appliance.control_url({"control_port": 1234}).endswith(":1234")


@pytest.mark.parametrize("verb,args", [
    ("world_load", {"name": "x"}), ("world_new", {}), ("world_save", {"name": "x"}),
    ("reset", {}), ("release", {}),
])
def test_world_verbs_refused_on_a_lab(engine, no_supervisor, verb, args):
    engine()
    with pytest.raises(ValueError, match="not available on a lab target"):
        call(verb, session_id=LAB, **args)


# ---- a lab keeps off the realm's throttle and events -------------------------

def test_lab_calls_use_their_own_throttle_key(engine):
    b = engine()
    seen = []
    ha.THROTTLE.admit = lambda key, deadline: seen.append(key)
    call("exec_lua", session_id=LAB, code="rcon.print(1)")
    call("tool", session_id=LAB, name="look")
    call("sbx", session_id=LAB, verb="sandbox_list")
    assert seen == ["fxlab-t-s/anon"] * 3
    assert ha._key(None) == ha.ANON and ha._key({"player_name": "p"}, {"kind": "lab"}) == "p"


def test_lab_calls_leave_the_realm_anon_budget_alone(engine):
    engine()
    call("exec_lua", session_id=LAB, code="rcon.print(1)")
    assert ha.ANON not in ha.THROTTLE.snapshot()["keys"]


def test_observe_events_on_a_lab_drains_and_fires_nothing(engine):
    b = engine()
    ha._EVENT_BUF.clear()
    assert call("observe_events", session_id=LAB) == {"events": []}
    assert b.sent == []
    ha._fire(LAB, "x", {})
    assert LAB not in ha._EVENT_BUF


def test_lab_mod_version_is_not_cached_for_long(engine, monkeypatch):
    b = engine()
    clock = [100.0]
    monkeypatch.setattr(ha.time, "monotonic", lambda: clock[0])
    row = {"session_id": LAB, "container_name": "fxlab-t-s", "kind": "lab"}
    ha._mod_version(row)
    clock[0] += 3
    ha._mod_version(row)
    assert len([c for c in b.sent if "active_mods" in c]) == 1
    clock[0] += 3
    ha._mod_version(row)
    assert len([c for c in b.sent if "active_mods" in c]) == 2


# ---- seatless blueprint stamps on a lab -----------------------------------------

def stamp_bodies(b):
    out = []
    for c in b.sent:
        m = re.search(r"remote\.call\('game_bot','(bp_put|blueprint_stamp)',h\.json_to_table\('(.*)'\)", c)
        if m:
            out.append((m[1], json.loads(m[2])))
    return out


def test_lab_stamp_uses_a_short_hashed_upload_id_and_a_surface(engine):
    b = engine()
    long_lab = "lab:" + "n" * 100
    call("blueprint_stamp", session_id=long_lab, x=1, y=2, blueprint="0abc")
    sent = stamp_bodies(b)
    assert [v for v, _ in sent] == ["bp_put", "blueprint_stamp"]
    ids = {body["id"] for _, body in sent}
    assert len(ids) == 1 and len(ids.pop()) <= 64
    stamp = sent[1][1]
    assert stamp["surface"] == "nauvis" and "seat" not in stamp


def test_lab_stamp_passes_surface_force_and_built_mode(engine):
    b = engine()
    call("blueprint_stamp", session_id=LAB, x=1, y=2, blueprint="0abc",
         surface="fx", force="enemy", build_mode="built")
    stamp = stamp_bodies(b)[-1][1]
    assert stamp["surface"] == "fx" and stamp["force"] == "enemy" and stamp["build"] is True
    with pytest.raises(appliance.ApplianceError, match="refused"):
        call("blueprint_stamp", session_id=LAB, x=1, y=2, blueprint="0abc",
             build=True, rules="realm")


# ---- the exec log ------------------------------------------------------------

def test_exec_lua_log_line(engine, tmp_path):
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


def test_exec_log_failure_never_fails_the_verb(engine, tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setenv("RLM_FACTORIO_EXEC_LOG", str(blocker / "sub" / "log.jsonl"))
    engine()
    with caplog.at_level("WARNING"):
        assert "output" in call("exec_lua", session_id=LAB, code="rcon.print(1)")
    assert any(r.levelname == "WARNING" and "exec log" in r.message for r in caplog.records)


def test_exec_log_creates_parent_dir(engine, tmp_path, monkeypatch):
    target = tmp_path / "deep" / "er" / "log.jsonl"
    monkeypatch.setenv("RLM_FACTORIO_EXEC_LOG", str(target))
    engine()
    call("exec_lua", session_id=LAB, code="rcon.print(1)")
    assert target.exists()
