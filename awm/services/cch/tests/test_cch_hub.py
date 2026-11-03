"""cch's contract verbs: each maps the contract's arguments onto the code that does the work."""
from __future__ import annotations

import asyncio

import pytest

from awm.cch import control, hub_adapter, inject, lifecycle, session_target

SID = "0e380e38-0e38-0e38-0e38-0e380e380e38"
R9 = "cc" + SID.replace("-", "")


def run(verb, args, as_=None):
    fn = hub_adapter.HANDLERS[verb]
    import inspect
    out = fn(args, as_) if len(inspect.signature(fn).parameters) == 2 else fn(args)
    return asyncio.run(out) if asyncio.iscoroutine(out) else out


def _target(**kw):
    base = dict(pid=4242, kind="bg", job="abcd1234", name="me", alive=True,
                session_id=SID)
    return control.Target(**{**base, **kw})


def test_platform_names_claude_code_and_the_contract():
    out = run("platform", {})
    assert out["code"] == "cc" and out["contract"] == "harness@1"
    assert "revive_pinned" in out["capabilities"]


def test_resolve_answers_mine_false_for_a_pid_that_is_no_session(monkeypatch):
    def refuse(pid):
        raise session_target.ResolveError("no record")

    monkeypatch.setattr(inject, "describe_caller", refuse)
    out = run("resolve", {"pid": 7})
    assert (out["ok"], out["mine"], out["error"]) == (True, False, "no record") and "mode" in out


def test_resolve_gives_the_r9_id_and_the_raw_one(monkeypatch):
    monkeypatch.setattr(inject, "describe_caller", lambda pid: {
        "session": "me", "session_id": SID, "pid": pid, "hosting": "daemon"})
    monkeypatch.setattr(hub_adapter.transcript, "session_and_cwd_for",
                        lambda pid: (SID, "/w/projects/awm/x"))
    monkeypatch.setattr(hub_adapter.roster, "job_of_pid", lambda pid, **kw: "abcd1234")
    out = run("resolve", {"pid": 4242})
    assert (out["session_id"], out["claude_session_id"]) == (R9, SID)
    assert (out["hosting"], out["job"], out["cwd"]) == ("background", "abcd1234",
                                                        "/w/projects/awm/x")


def test_resolve_names_a_job_by_its_state_file_not_its_seed_name(monkeypatch, tmp_path):
    (tmp_path / "abcd1234").mkdir()
    (tmp_path / "abcd1234" / "state.json").write_text('{"name": "msm fed shaula"}')
    monkeypatch.setattr(hub_adapter.roster, "default_jobs_dir", lambda: tmp_path)
    monkeypatch.setattr(inject, "describe_caller", lambda pid: {
        "session": "stale seed name", "session_id": SID, "pid": pid, "hosting": "daemon"})
    monkeypatch.setattr(hub_adapter.transcript, "session_and_cwd_for", lambda pid: (SID, None))
    monkeypatch.setattr(hub_adapter.roster, "job_of_pid", lambda pid, **kw: "abcd1234")
    assert run("resolve", {"pid": 4242})["name"] == "msm fed shaula"


def test_an_r9_id_reaches_control_as_the_session_uuid(monkeypatch):
    seen = []
    monkeypatch.setattr(control, "interrupt",
                        lambda job, caller_pid: seen.append((job, caller_pid)) or {"ok": True})
    run("interrupt", {"session_id": R9, "pid": 9})
    assert seen == [(SID, 9)]


def test_another_platforms_id_is_refused():
    out = run("interrupt", {"session_id": "ds" + "a" * 32})
    assert out["ok"] is False and "not a Claude Code session id" in out["error"]


def test_set_with_one_key_answers_as_that_verb(monkeypatch):
    monkeypatch.setattr(control, "model", lambda v, job, caller_pid: {"ok": True, "model": v})
    out = run("set", {"pid": 1, "model": "opus"})
    assert out["ok"] and out["model"] == "opus" and "model" in out["applied"]


def test_stop_with_keep_keeps_the_job_and_stops_the_caller_later(monkeypatch):
    calls = {}
    monkeypatch.setattr(control, "resolve", lambda ref, pid: _target(is_self=True))

    def stop(ref, caller_pid, confirm, remove=None):
        calls.update(ref=ref, confirm=confirm, remove=remove)
        return {"ok": True, "stopping": True}

    monkeypatch.setattr(control, "stop", stop)
    out = run("stop", {"ref": "abcd1234", "pid": 4242, "confirm": True, "keep": True})
    assert out["stopping"] and calls["remove"] is hub_adapter._stop_keeping


def test_a_kept_stop_of_a_started_job_goes_through_its_lineage(monkeypatch):
    monkeypatch.setattr(control, "resolve", lambda ref, pid: _target())
    monkeypatch.setattr(lifecycle, "read_lineage", lambda job: {"job": job})

    async def lstop(args, as_):
        return {"ok": True, "via": "lifecycle", **args}

    monkeypatch.setattr(lifecycle, "stop", lstop)
    out = run("stop", {"ref": "abcd1234", "pid": 9, "keep": True})
    assert out["via"] == "lifecycle" and out["job"] == "abcd1234"


def test_stop_without_keep_removes_the_job(monkeypatch):
    calls = {}
    monkeypatch.setattr(control, "stop",
                        lambda ref, caller_pid, confirm, **kw: calls.update(kw) or {"ok": True})
    run("stop", {"ref": "abcd1234", "keep": False})
    assert "remove" not in calls


def test_stop_removes_the_job_by_default_and_names_the_session(monkeypatch):
    calls = {}
    monkeypatch.setattr(control, "resolve", lambda ref, pid: _target(
        session_id="0e384a75-a06e-43fa-a19e-298e409a2730"))
    monkeypatch.setattr(control, "stop",
                        lambda ref, caller_pid, confirm, **kw: calls.update(kw) or {"ok": True})
    out = run("stop", {"ref": "abcd1234"})
    assert "remove" not in calls
    assert out["session_id"] == "cc0e384a75a06e43faa19e298e409a2730"


def test_a_job_that_already_stopped_counts_as_stopped(monkeypatch):
    from awm.claudedaemon import cli

    monkeypatch.setattr(cli, "stop_keeping", lambda job, claude=None: {
        "stopped": False, "removed": False, "kept": True, "detail": "not running"})
    monkeypatch.setattr(cli.JobProbe, "worker_alive", lambda self, job: False)
    recorded = []
    monkeypatch.setattr(lifecycle, "_record_stop", recorded.append)
    out = hub_adapter._stop_keeping("abcd1234")
    assert out["stopped"] and recorded == ["abcd1234"]
    assert control._done(out)


def test_resume_can_adopt_a_job_with_no_lineage(monkeypatch):
    written = {}
    monkeypatch.setattr(lifecycle, "read_lineage", lambda job: None)
    monkeypatch.setattr(lifecycle, "_write_lineage", lambda job, rec: written.update(rec))

    async def resume(args, as_):
        return {"ok": True, **args}

    monkeypatch.setattr(lifecycle, "resume", resume)
    out = run("resume", {"ref": "abcd1234", "adopt_lineage": True})
    assert out["ok"] and written["origin"] == "wait" and "adopt_lineage" not in out


def test_revive_pinned_resumes_only_the_dead_pins(monkeypatch):
    monkeypatch.setattr(hub_adapter, "_live_jobs", lambda: {"aaaa1111"})
    monkeypatch.setattr(hub_adapter.pins, "pinned", lambda: ["aaaa1111", "bbbb2222"])
    monkeypatch.setattr(lifecycle, "read_lineage", lambda job: {"job": job})
    resumed = []

    async def resume(args, as_=None):
        resumed.append(args["job"])
        return {"ok": True}

    monkeypatch.setattr(lifecycle, "resume", resume)
    out = run("revive_pinned", {})
    assert (out["revived"], out["live"], out["failed"]) == (["bbbb2222"], ["aaaa1111"], [])


def test_compact_refuses_another_session():
    out = run("compact", {"ref": "abcd1234"})
    assert out["unsupported"] is True


def test_resolve_reports_the_mode_for_the_gateways_gate(monkeypatch):
    from awm.claudedaemon import sessionmode

    monkeypatch.setattr(sessionmode, "mode_of", lambda pid: "reader" if pid == 77 else None)
    monkeypatch.setattr(hub_adapter.inject, "describe_caller", lambda pid: (_ for _ in ()).throw(
        hub_adapter.session_target.ResolveError("not a session")))
    out = run("resolve", {"pid": 77})
    assert out["mine"] is False and out["mode"] == "reader"
