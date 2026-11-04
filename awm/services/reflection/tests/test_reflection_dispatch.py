"""reflection against fake harness plugins: the contract seen from reflection's side.

Each fake answers the harness-contract verbs from a table of sessions, and
records every call, so a test reads exactly what reflection asked of whom.
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest

from awm.reflection import alarm, elsewhere, harness, hub_adapter, ledger, policy, surface

CC = harness.Provider("harness-cc", "cc")
DS = harness.Provider("harness-ds", "ds")
ME = 4242
MY_ID = "cc" + "0e38" * 8


class Fake:
    def __init__(self, provider, sessions, *, down=False):
        self.p = provider
        self.sessions = sessions  # ref -> row
        self.calls: list[tuple[str, dict]] = []
        self.down = down
        self.answers: dict[str, dict] = {}

    def __call__(self, verb, args, as_):
        self.calls.append((verb, args))
        if self.down:
            raise OSError("connection refused")
        if verb in self.answers:
            return self.answers[verb]
        if verb == "resolve":
            for row in self.sessions.values():
                if row.get("pid") == args["pid"]:
                    return {"ok": True, "mine": True, **row}
            return {"ok": True, "mine": False}
        if verb == "find":
            row = self.sessions.get(args["ref"])
            return {"ok": True, "matches": [row] if row else []}
        if verb == "list":
            return {"ok": True, "sessions": list(self.sessions.values())}
        if verb == "pending":
            return {"ok": True, "pending": []}
        if verb in ("send", "compact"):
            out = {"ok": True, "text": args.get("text", "/compact"),
                   "submitted": True, "followup": args.get("followup")}
            if args.get("followup"):
                out.update(followup_deferred=True, ticket="t-1")
            return out
        if verb == "start":
            return {"ok": True, "job": "abcd1234", "session_id": "cc" + "1" * 32}
        if verb == "status":
            return {"ok": True, "session_id": MY_ID, "kind": "bg", "job": "j1",
                    "pid": ME, "cwd": "/w/projects/awm/old"}
        return {"ok": True, "verb": verb}

    def verbs(self):
        return [v for v, _ in self.calls]


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    monkeypatch.setattr(hub_adapter, "_brief_line", lambda project, scope: "")
    cc = Fake(CC, {"j1": {"session_id": MY_ID, "name": "me", "pid": ME, "job": "j1",
                          "cwd": "/w/projects/awm/redux-harness"}})
    ds = Fake(DS, {})
    by_tool = {CC.tool: cc, DS.tool: ds}
    monkeypatch.setattr(harness, "_call", lambda p, verb, args, as_:
                        by_tool[p.tool](verb, args, as_))
    monkeypatch.setattr(harness, "providers", lambda: [CC, DS])
    monkeypatch.setattr(alarm, "STATE_DIR", tmp_path / "state")
    posts = []
    monkeypatch.setattr(alarm, "_call", lambda s, f, a: posts.append((s, f, a)))
    monkeypatch.setattr(hub_adapter, "_task_of", lambda sid: "task-9")
    monkeypatch.setattr(hub_adapter, "_scopes_call", lambda fn, a: None)
    return type("Fakes", (), {"cc": cc, "ds": ds, "posts": posts})


def run(verb, args, as_=None):
    fn = hub_adapter.HANDLERS[verb]
    out = fn(args, as_) if len(__import__("inspect").signature(fn).parameters) == 2 else fn(args)
    return asyncio.run(out) if asyncio.iscoroutine(out) else out


def test_every_published_verb_has_a_handler_and_a_reflection_name():
    names = {f["name"] for f in surface.FUNCTIONS}
    assert names == set(hub_adapter.HANDLERS)
    for f in surface.FUNCTIONS:
        assert f["tool"] == f"reflection_{f['name']}", f


def test_the_internal_verbs_stay_off_mcp():
    hidden = {f["name"] for f in surface.FUNCTIONS if f.get("surfaces") == ["cli", "http"]}
    assert {"harness_event", "session_of", "retired", "retired_among"} <= hidden


def test_no_harness_specific_verb_is_reflections():
    """The daemon and warm-pool verbs are cch's own (install.md R135 item 4)."""
    names = {f["name"] for f in surface.FUNCTIONS}
    assert not names & {"claim_other", "seed_other", "remove_other", "revive_pinned",
                        "daemon_status", "daemon_ensure", "daemon_hold", "daemon_release"}


def test_message_self_goes_to_the_harness_that_owns_the_caller(fakes):
    out = run("message_self", {"_caller_pid": ME, "text": "/compact", "followup": "go on"})
    assert out["ok"] and out["ticket"] == "t-1"
    verb, args = fakes.cc.calls[-1]
    assert verb == "send" and args["pid"] == ME and args["submit"] is True
    assert fakes.ds.verbs() == ["resolve"]
    [ticket] = ledger.open_tickets()
    assert ticket["session_id"] == MY_ID and ticket["tool"] == "harness-cc"


def test_a_caller_no_harness_owns_is_refused(fakes):
    out = run("compact", {"_caller_pid": 99})
    assert out["ok"] is False and "pid 99" in out["error"]
    assert "compact" not in fakes.cc.verbs()


def test_a_call_with_no_caller_pid_is_refused(fakes):
    out = run("compact", {})
    assert out["ok"] is False and "could not tell which session" in out["error"]


def test_a_harness_that_owns_the_pid_but_not_the_session_is_refused(fakes):
    fakes.ds.answers["resolve"] = {"ok": True, "mine": True, "session_id": None,
                                   "error": "dsh cannot tell which session called"}
    fakes.cc.sessions.clear()
    out = run("compact", {"_caller_pid": ME})
    assert out == {"ok": False, "error": "dsh cannot tell which session called"}


def test_an_r9_id_picks_the_harness_by_its_platform_code(fakes):
    run("interrupt", {"_caller_pid": ME, "job": "ds" + "a" * 32})
    assert fakes.cc.verbs() == ["resolve"] or "find" not in fakes.cc.verbs()
    assert fakes.ds.calls[-1] == ("interrupt", {"session_id": "ds" + "a" * 32, "pid": ME})


def test_a_job_is_found_by_asking_each_harness(fakes):
    run("model", {"_caller_pid": ME, "job": "j1", "model": "opus"})
    assert fakes.cc.calls[-1] == ("set", {"ref": "j1", "pid": ME, "model": "opus"})


def test_a_ref_two_harnesses_claim_is_refused(fakes):
    fakes.ds.sessions["j1"] = {"session_id": "ds" + "b" * 32}
    out = run("interrupt", {"_caller_pid": ME, "job": "j1"})
    assert out["ok"] is False and "harness-cc, harness-ds" in out["error"]


def test_an_unknown_job_names_the_peer_that_runs_it(fakes, monkeypatch):
    asked = []
    monkeypatch.setattr(elsewhere, "hint", lambda ref: asked.append(ref) or
                        ". It runs on altair: call again with peer='altair'")
    out = run("message", {"_caller_pid": ME, "job": "67212311", "text": "hi"})
    assert out["ok"] is False and "peer='altair'" in out["error"]
    assert asked == ["67212311"]


def test_a_call_from_a_peer_does_not_fan_out_again(fakes, monkeypatch):
    monkeypatch.setattr(elsewhere, "hint", lambda ref: pytest.fail("fanned out"))
    out = run("message", {"job": "67212311", "text": "hi"})
    assert "no session on this node" in out["error"]


def test_a_harness_that_does_not_answer_is_skipped(fakes):
    fakes.ds.down = True
    out = run("list_other", {})
    assert [r["harness"] for r in out["sessions"]] == ["harness-cc"]


def test_list_other_merges_every_harness(fakes):
    fakes.ds.sessions["x"] = {"session_id": "ds" + "c" * 32, "name": "web"}
    out = run("list_other", {"project": "awm"})
    assert {r["harness"] for r in out["sessions"]} == {"harness-cc", "harness-ds"}
    assert fakes.cc.calls[-1] == ("list", {"project": "awm"})


def test_list_other_shows_each_recorded_level(fakes):
    """A session with no record shows None, which a gate reads as trusted (policy.DEFAULT_LEVEL)."""
    fakes.ds.sessions["x"] = {"session_id": "ds" + "c" * 32, "name": "web"}
    policy.record("ds" + "c" * 32, "cooperative", "test")
    rows = {r["harness"]: r for r in run("list_other", {})["sessions"]}
    assert rows["harness-ds"]["trust"] == "cooperative"
    assert rows["harness-cc"]["trust"] is None


def test_stop_retires_the_session_and_removes_its_job(fakes):
    run("stop", {"_caller_pid": ME, "job": "j1", "confirm": True})
    assert fakes.cc.calls[-1][1]["keep"] is False


def test_start_other_passes_the_spec_through_and_records_trust(fakes, monkeypatch):
    monkeypatch.setattr(policy, "_lib", lambda: type("L", (), {
        "grant_key": staticmethod(lambda: b""),
        "level_for_start": staticmethod(lambda s, r, **kw: r or s),
        "may_steer": staticmethod(lambda a, v, target_level=None: (True, ""))}))
    out = run("start_other", {"_caller_pid": ME, "project": "awm", "scope": "x",
                              "task_id": "t9", "role": "worker", "trust": "cooperative",
                              "name": "seat:ana"})
    verb, args = fakes.cc.calls[-1]
    assert verb == "start" and args["task_id"] == "t9" and args["pid"] == ME
    assert out["trust"] == "cooperative"
    assert policy.level_of("cc" + "1" * 32) == "cooperative"
    assert json.loads(policy._path().read_text())["cc" + "1" * 32]["origin"] == "seat:ana"


def test_start_other_records_the_caller_as_the_parent(fakes, monkeypatch):
    links = []
    monkeypatch.setattr(hub_adapter, "_record_parent", lambda *a: links.append(a) or True)
    run("start_other", {"_caller_pid": ME, "project": "awm", "scope": "x"})
    assert links == [("cc" + "1" * 32, MY_ID, "start")]


def test_a_start_with_no_calling_session_records_no_parent(fakes, monkeypatch):
    links = []
    monkeypatch.setattr(hub_adapter, "_record_parent", lambda *a: links.append(a) or True)
    run("start_other", {"project": "awm", "scope": "x"})
    assert links == []


def test_a_clear_event_records_the_old_session_as_the_parent(fakes, monkeypatch):
    links = []
    monkeypatch.setattr(hub_adapter, "_record_parent", lambda *a: links.append(a) or True)
    old = "0e380e38-0e38-0e38-0e38-0e380e380e38"
    out = run("harness_event", {"session_id": "cc" + "2" * 32, "kind": "clear", "parent": old})
    assert out == {"ok": True, "recorded": True}
    assert links == [("cc" + "2" * 32, MY_ID, "clear")]
    assert run("harness_event", {"session_id": "cc" + "2" * 32, "kind": "clear"})["ok"] is False


def test_an_isolated_start_is_untrusted_unless_trust_is_named(fakes, monkeypatch):
    monkeypatch.setattr(policy, "_lib", lambda: type("L", (), {
        "grant_key": staticmethod(lambda: b""),
        "level_for_start": staticmethod(lambda s, r, **kw: r or s),
        "may_steer": staticmethod(lambda a, v, target_level=None: (True, ""))}))
    out = run("start_other", {"_caller_pid": ME, "project": "awm", "scope": "x",
                              "isolated": True})
    verb, args = fakes.cc.calls[-1]
    assert verb == "start" and args["isolated"] is True
    assert out["trust"] == "untrusted"


def test_a_start_above_the_callers_trust_is_refused(fakes, monkeypatch):
    def deny(s, r, **kw):
        raise PermissionError(f"a raise from {s} to {r} takes a grant Tony approves")

    monkeypatch.setattr(policy, "_lib", lambda: type("L", (), {
        "grant_key": staticmethod(lambda: b""), "level_for_start": staticmethod(deny),
        "may_steer": staticmethod(lambda a, v, target_level=None: (True, ""))}))
    policy.record(MY_ID, "cooperative", "test")
    out = run("start_other", {"_caller_pid": ME, "project": "awm", "scope": "x",
                              "trust": "trusted"})
    assert out["ok"] is False and "grant" in out["error"]
    assert "start" not in fakes.cc.verbs()


def test_a_steer_the_callers_trust_forbids_is_refused(fakes, monkeypatch):
    monkeypatch.setattr(policy, "_lib", lambda: type("L", (), {
        "may_steer": staticmethod(lambda a, v, target_level=None: (False, "no"))}))
    out = run("interrupt", {"_caller_pid": ME, "job": "j1"})
    assert out == {"ok": False, "error": "no"}
    assert "interrupt" not in fakes.cc.verbs()


def test_the_first_local_message_carries_the_hint_once(fakes, monkeypatch):
    monkeypatch.setattr(policy, "_lib", lambda: type("L", (), {
        "FIRST_LOCAL_MESSAGE_HINT": "use SendMessage"}))
    first = run("message", {"_caller_pid": ME, "job": "j1", "text": "hi"})
    second = run("message", {"_caller_pid": ME, "job": "j1", "text": "hi"})
    assert first["hint"] == "use SendMessage" and "hint" not in second


def test_whoami_adds_trust_task_and_harness(fakes):
    out = run("whoami", {"_caller_pid": ME})
    assert (out["session_id"], out["harness"], out["task_id"], out["trust"]) == (
        MY_ID, "harness-cc", "task-9", "trusted")


def test_session_of_names_the_session_behind_a_pid_a_service_holds(fakes):
    out = run("session_of", {"pid": ME})
    assert (out["ok"], out["session_id"], out["harness"]) == (True, MY_ID, "harness-cc")


def test_session_of_a_pid_no_harness_claims_is_not_known(fakes):
    out = run("session_of", {"pid": 31337})
    assert out["ok"] and out["known"] is False and "session_id" not in out


def test_cd_claims_the_new_scope_and_releases_the_old(fakes, monkeypatch):
    calls = []
    monkeypatch.setattr(hub_adapter, "_scopes_call",
                        lambda fn, a: calls.append((fn, a)) or {"ok": True, "claimed": True})
    out = run("cd", {"_caller_pid": ME, "path": "/w/projects/awm/new"})
    assert out["ok"] and out["claim"]["claimed"]
    assert [c[0] for c in calls] == ["claim", "release"]
    assert calls[0][1]["scope"] == "new" and calls[1][1]["scope"] == "old"
    assert fakes.cc.calls[-1] == ("set", {"pid": ME, "cwd": "/w/projects/awm/new"})


def test_cd_hands_the_new_scopes_brief_to_the_harness(fakes, monkeypatch):
    monkeypatch.setattr(hub_adapter, "_scopes_call", lambda fn, a: {"ok": True, "claimed": True})
    monkeypatch.setattr(hub_adapter, "_brief_line",
                        lambda project, scope: f"The brief of {project}/{scope}: For x.")
    run("cd", {"_caller_pid": ME, "path": "/w/projects/awm/new"})
    assert fakes.cc.calls[-1] == ("set", {"pid": ME, "cwd": "/w/projects/awm/new",
                                          "brief": "The brief of awm/new: For x."})


def test_the_brief_line_reads_the_journal(monkeypatch):
    import awm.gatewayclient as gc
    monkeypatch.setattr(gc, "call_sync", lambda svc, fn, a, timeout=None: {
        "brief": {"text": "For the scopes service."}} if (svc, fn) == ("journal", "brief_read") else {})
    assert hub_adapter._brief_line("awm", "s") == "The brief of awm/s: For the scopes service."
    monkeypatch.setattr(gc, "call_sync", lambda *a, **k: {"brief": None})
    assert hub_adapter._brief_line("awm", "s") == ""


def test_a_background_session_does_not_move_into_a_held_scope(fakes, monkeypatch):
    monkeypatch.setattr(hub_adapter, "_scopes_call", lambda fn, a: {
        "ok": False, "claimed": False, "holder": "cc99"})
    out = run("cd", {"_caller_pid": ME, "path": "/w/projects/awm/new"})
    assert out["ok"] is False and "cc99" in out["reason"]
    assert "set" not in fakes.cc.verbs()


def test_cd_says_when_scope_claims_are_unavailable(fakes):
    out = run("cd", {"_caller_pid": ME, "path": "/w/projects/awm/new"})
    assert out["claim"] == "unavailable"


def test_a_lost_event_raises_the_alarm_and_closes_its_ticket(fakes):
    run("compact", {"_caller_pid": ME, "followup": "go on"})
    out = run("harness_event", {"session_id": MY_ID, "ticket": "t-1", "kind": "lost",
                                "reason": "worker swapped twice"})
    assert out["ok"] and out["alarm"]["journal"]
    assert ledger.open_tickets() == []
    [entry] = alarm.recent()
    assert entry["reason"] == "worker swapped twice" and entry["command"] == "/compact"
    [(service, fn, post)] = fakes.posts
    assert (service, fn, post["project"], post["scope"]) == (
        "journal", "post", "awm", "redux-harness")


def test_a_done_event_closes_its_ticket_quietly(fakes):
    run("compact", {"_caller_pid": ME, "followup": "go on"})
    assert run("harness_event", {"session_id": MY_ID, "ticket": "t-1",
                                 "kind": "done"}) == {"ok": True}
    assert ledger.open_tickets() == [] and alarm.recent() == []


def test_an_event_that_beats_its_ticket_keeps_it_closed(fakes):
    run("harness_event", {"session_id": MY_ID, "ticket": "t-1", "kind": "done"})
    run("compact", {"_caller_pid": ME, "followup": "go on"})
    assert ledger.open_tickets() == []


def test_the_sweep_alarms_only_for_a_ticket_its_plugin_dropped(fakes):
    for t in ("held", "dropped", "unasked"):
        ledger.open_ticket(t, session_id=MY_ID, tool="harness-ds" if t == "unasked"
                           else "harness-cc", command="/compact", followup="go on")
    later = 10**13
    lost = ledger.sweep(lambda tool: ["held"] if tool == "harness-cc" else None,
                        now_ms=later)
    assert lost == ["dropped"]
    assert {t["ticket"] for t in ledger.open_tickets()} == {"held", "unasked"}
    assert "no longer holds" in alarm.recent()[0]["reason"]


def test_a_failure_is_logged_without_the_callers_text(fakes, caplog):
    fakes.cc.sessions.clear()
    with caplog.at_level(logging.INFO):
        run("message_self", {"_caller_pid": ME, "text": "secret words"})
    assert "secret words" not in caplog.text
    assert "message_self failed for caller pid 4242" in caplog.text


def test_the_providers_come_from_the_live_contract_table(monkeypatch):
    """No provider is named in reflection: the table lists them, each says its code."""
    import httpx

    monkeypatch.setattr(harness, "_table", (0.0, []))
    monkeypatch.setattr(harness, "_codes", {})
    asked = []

    class Reply:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"name": "harness-cc", "major": 1, "plugin": "cch", "peer": None},
                    {"name": "harness-ds", "major": 2, "plugin": "dsh", "peer": None},
                    {"name": "harness-x", "major": 1, "plugin": "x", "peer": "altair"}]

    monkeypatch.setattr(httpx, "get", lambda url, **kw: asked.append(url) or Reply())
    codes = {"harness-cc": "cc", "harness-x": "ds"}
    monkeypatch.setattr(harness, "_call", lambda p, verb, args, as_: {"ok": True,
                        "code": codes[p.tool]} if verb == "platform" else {})
    assert harness.providers() == [harness.Provider("harness-cc", "cc"),
                                   harness.Provider("harness-x", "ds", "altair")]
    assert asked[0].endswith("/hub/contract/harness@1")


def test_no_contract_table_is_no_provider(monkeypatch):
    import httpx

    monkeypatch.setattr(harness, "_table", (0.0, []))

    def down(url, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", down)
    assert harness.providers() == []
    with pytest.raises(harness.HarnessError):
        harness.for_platform("cc")


def test_an_isolated_caller_is_named_by_its_session_id(fakes):
    out = run("whoami", {"_caller_session_id": MY_ID})
    assert out["session_id"] == MY_ID
    assert ("status", {"session_id": MY_ID}) in fakes.cc.calls
    assert "resolve" not in fakes.cc.verbs()
    run("compact", {"_caller_session_id": MY_ID})
    assert fakes.cc.calls[-1][0] == "compact"
    assert fakes.cc.calls[-1][1]["session_id"] == MY_ID and "pid" not in fakes.cc.calls[-1][1]


def test_hint_asks_each_peer_and_names_the_holder():
    lists = {"altair": [{"job": "67212311", "name": "x"}],
             "deneb": [{"job": "11111111", "name": "y"}]}

    def lister(peer):
        if peer == "mira":
            raise OSError("asleep")
        return lists[peer]

    peers = ["altair", "deneb", "mira"]
    assert elsewhere.hint("67212311", peers=peers, lister=lister) == (
        ". It runs on altair: call again with peer='altair'")
    assert elsewhere.holders("Y", peers=peers, lister=lister) == ["deneb"]
    assert elsewhere.hint("nobody", peers=peers, lister=lister) == ""


@pytest.mark.parametrize("cwd,want", [
    ("/w/projects/awm/redux-harness", ("awm", "redux-harness")),
    ("/w/projects/metasmith/lab/a", ("metasmith", "lab/a")),
    ("/w/projects/awm", None),
    ("/home/tony", None),
    (None, None),
])
def test_scope_of(cwd, want):
    assert alarm.scope_of(cwd) == want


# --- wait (sessions.md R51) -----------------------------------------------------

OTHER = "cc" + "2" * 32
THIRD = "cc" + "3" * 32


@pytest.fixture
def lonely(monkeypatch):
    """No peers: every wait and retire stays on this node."""
    monkeypatch.setattr(hub_adapter, "_peers", lambda: [])


def test_wait_records_the_wait_then_stops_the_caller_keeping_its_job(fakes, lonely):
    out = run("wait", {"_caller_pid": ME, "until": [OTHER]})
    assert out["ok"] and out["waiting"]
    assert fakes.cc.calls[-1] == ("stop", {"ref": "j1", "pid": ME, "keep": True,
                                           "confirm": True})
    [row] = run("waits", {})["waits"]
    assert (row["session_id"], row["until"], row["job"]) == (MY_ID, [OTHER], "j1")
    assert (row["project"], row["scope"]) == ("awm", "redux-harness")


def test_wait_takes_session_uuids_and_refuses_names(fakes, lonely):
    uuid = "22222222-2222-2222-2222-222222222222"
    assert run("wait", {"_caller_pid": ME, "until": [uuid]})["until"] == [OTHER]
    out = run("wait", {"_caller_pid": ME, "until": ["builder"]})
    assert out["ok"] is False and "session ids" in out["reason"]


def test_a_wait_whose_stop_fails_is_not_left_behind(fakes, lonely):
    fakes.cc.answers["stop"] = {"ok": False, "reason": "claude stop failed"}
    out = run("wait", {"_caller_pid": ME, "until": [OTHER]})
    assert out["ok"] is False and run("waits", {})["waits"] == []


def test_a_terminal_session_waits_without_stopping(fakes, lonely):
    fakes.cc.sessions["j1"] = {**fakes.cc.sessions["j1"], "job": None}
    out = run("wait", {"_caller_pid": ME, "until": [OTHER]})
    assert out["ok"] and out["waiting"] and out["terminal"]
    assert "stop" not in fakes.cc.verbs()
    run("retired", {"session_id": OTHER})
    assert fakes.cc.calls[-1][0] == "send"
    assert fakes.cc.calls[-1][1]["session_id"] == MY_ID
    assert run("waits", {})["waits"] == []


def test_the_last_retire_resumes_the_waiter_with_the_wake_line(fakes, lonely):
    run("wait", {"_caller_pid": ME, "until": [OTHER, THIRD]})
    fakes.cc.sessions["w2"] = {"session_id": OTHER, "name": "w2", "job": "w2"}
    fakes.cc.answers["stop"] = {"ok": True, "session_id": OTHER}
    assert run("stop", {"job": "w2"})["woken"] == []
    assert "resume" not in fakes.cc.verbs()
    assert run("waits", {})["waits"][0]["retired"] == [OTHER]
    fakes.cc.answers["stop"] = {"ok": True, "session_id": THIRD}
    assert run("stop", {"job": THIRD})["woken"] == [MY_ID]
    verb, args = fakes.cc.calls[-1]
    assert verb == "resume" and args["ref"] == "j1" and OTHER in args["prompt"]
    assert run("waits", {})["waits"] == []


def test_a_waiter_that_itself_waits_has_not_finished(fakes, lonely):
    from awm.reflection import waits

    waits.add(session_id=OTHER, until=[THIRD], tool="harness-cc", job="w2")
    run("wait", {"_caller_pid": ME, "until": [OTHER]})
    run("retired", {"session_id": THIRD})
    rows = {w["session_id"]: w for w in run("waits", {})["waits"]}
    assert OTHER not in rows and rows[MY_ID]["retired"] == []


def test_a_session_already_retired_is_not_waited_on(fakes, lonely):
    from awm.reflection import waits

    waits.retired(OTHER, wake=lambda row: pytest.fail("no waiter yet"))
    out = run("wait", {"_caller_pid": ME, "until": [OTHER]})
    assert out["ok"] and out["waiting"] is False
    assert "stop" not in fakes.cc.verbs() and run("waits", {})["waits"] == []


def test_a_failed_wake_raises_an_alarm_and_keeps_the_wait(fakes, lonely):
    run("wait", {"_caller_pid": ME, "until": [OTHER]})
    fakes.cc.answers["resume"] = {"ok": False, "reason": "no transcript"}
    assert run("retired", {"session_id": OTHER})["woken"] == []
    assert "no transcript" in alarm.recent()[0]["reason"]
    assert len(run("waits", {})["waits"]) == 1


def test_a_retiring_waiter_clears_its_wait(fakes, lonely):
    from awm.reflection import waits

    waits.add(session_id=OTHER, until=[THIRD], tool="harness-cc", job="w2")
    waits.retired(OTHER, wake=lambda row: pytest.fail("woke"))
    assert run("waits", {})["waits"] == []


def test_a_resume_by_hand_clears_the_wait(fakes, lonely):
    run("wait", {"_caller_pid": ME, "until": [OTHER]})
    run("resume_other", {"job": "j1"})
    assert run("waits", {})["waits"] == []


def test_a_retire_tells_every_peer(fakes, monkeypatch):
    told = []
    monkeypatch.setattr(hub_adapter, "_peers", lambda: ["altair", "alcor"])
    from awm import gatewayclient
    monkeypatch.setattr(gatewayclient, "call_sync",
                        lambda tool, verb, args=None, *, peer=None, **kw: (lambda peer, svc, fn, a, timeout: told.append((peer, fn, a)))(peer, tool, verb, args, **kw))
    hub_adapter._tell_peers(OTHER)
    assert told == [("altair", "retired", {"session_id": OTHER}),
                    ("alcor", "retired", {"session_id": OTHER})]


def test_the_start_check_alarms_once_for_a_lost_session(fakes, lonely):
    from awm.reflection import waits

    run("wait", {"_caller_pid": ME, "until": [OTHER, THIRD]})
    state = {OTHER: "gone", THIRD: "live"}
    lost = waits.check(state=state.get, retired_elsewhere=lambda s: [],
                       wake=lambda row: pytest.fail("woke"))
    assert lost == [OTHER] and OTHER in alarm.recent()[0]["reason"]
    assert len(run("waits", {})["waits"]) == 1


def test_the_start_check_counts_a_retire_it_missed(fakes, lonely):
    from awm.reflection import waits

    run("wait", {"_caller_pid": ME, "until": [OTHER]})
    woke = []
    waits.check(state=lambda s: pytest.fail("asked"), retired_elsewhere=lambda s: [OTHER],
                wake=lambda row: woke.append(row["session_id"]) or {"ok": True})
    assert woke == [MY_ID] and run("waits", {})["waits"] == []


def test_whoami_asks_for_the_task_by_session_id(monkeypatch):
    """operations' task_for_session reads session_id (sessions.md R57 step 2)."""
    asked = []
    from awm import gatewayclient
    monkeypatch.setattr(gatewayclient, "call_sync", lambda svc, fn, a, timeout: asked.append(
        (svc, fn, a)) or {"task_id": "abcd1234"})
    assert hub_adapter._task_of(MY_ID) == "abcd1234"
    assert asked == [("operations", "task_for_session", {"session_id": MY_ID})]


def test_session_of_gives_the_gate_the_mode_and_the_trust(fakes):
    fakes.cc.sessions["j1"] = {**fakes.cc.sessions["j1"], "mode": "worker"}
    out = run("session_of", {"pid": ME})
    assert out["known"] and out["session_id"] == MY_ID
    assert (out["mode"], out["trust"]) == ("worker", "trusted")


def test_a_hand_started_session_reads_as_trusted_and_a_recorded_one_keeps_its_level(fakes):
    """Tony's own terminal sessions have no ledger entry: the gateway must not lock
    them out now that a failed lookup is untrusted (fleet.md R103 item 2)."""
    from awm.reflection import policy
    assert run("session_of", {"pid": ME})["trust"] == "trusted"
    policy.record(MY_ID, "cooperative", "test")
    assert run("session_of", {"pid": ME})["trust"] == "cooperative"


def test_session_of_a_pid_no_plugin_claims_carries_the_plugins_mode(fakes):
    real = fakes.cc.__call__

    def resolve(verb, args, as_):
        if verb == "resolve":
            return {"ok": True, "mine": False, "mode": None}
        return real(verb, args, as_)

    fakes.cc.answers.clear()
    harness_call = harness._call
    harness._call = lambda p, verb, args, as_: (resolve(verb, args, as_) if p.tool == CC.tool
                                                else harness_call(p, verb, args, as_))
    try:
        out = run("session_of", {"pid": 77})
    finally:
        harness._call = harness_call
    assert out["ok"] and out["known"] is False and out["mode"] is None


def test_session_of_with_no_plugin_answering_is_unknown(fakes):
    fakes.cc.down = fakes.ds.down = True
    out = run("session_of", {"pid": 77})
    assert out["known"] is False and out["mode"] == "unknown"


def test_the_hint_asks_only_the_nodes_this_node_reaches(tmp_path, monkeypatch):
    import yaml
    from awm.reflection import elsewhere

    edges = tmp_path / "edges.yml"
    edges.write_text(yaml.safe_dump({
        "nodes": {n: {"role": "homebase"} for n in ("here", "altair", "deneb", "mira")},
        "edges": {"here": {"altair": "lan", "mira": "lan"}, "deneb": {"here": "lan"}},
    }))
    monkeypatch.setenv("AWM_EDGES_FILE", str(edges))
    monkeypatch.setenv("AWM_NODE_NAME", "here")
    assert elsewhere._reached_peers() == ["altair", "mira"]
