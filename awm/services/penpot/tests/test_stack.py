"""Tests for `awm.penpot.stack` — no live docker daemon involved.

Every test drives `Stack` through an injected fake `docker compose` runner
(`FakeRunner` below), which records argv and answers `ps --all --format
json` from a fixed container list. None of this shells out for real; a test
that needs to prove something about a real running stack belongs in the
plan's end-to-end verification pass, not here.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from awm.penpot import stack

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def _entry(service: str, state: str = "running", health: str | None = None,
           exit_code: int = 0) -> dict:
    """One `docker compose ps --format json` row."""
    return {"Service": service, "State": state, "Health": health or "",
            "ExitCode": exit_code}


class FakeRunner:
    """A `docker compose` stand-in that never touches a real daemon.

    Answers `ps --all --format json` from a fixed container list (encoded
    JSONL, the shape most compose versions emit) and records every
    invocation's argv, so a test can assert on what was actually run without
    spawning `docker`. Non-`ps` calls answer a plain success unless
    `returncode` says otherwise.
    """

    def __init__(self, ps_entries: list[dict] | None = None, *, returncode: int = 0):
        self.ps_entries = ps_entries or []
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        if "ps" in args:
            stdout = "\n".join(json.dumps(e) for e in self.ps_entries)
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
        return subprocess.CompletedProcess(args, self.returncode, stdout="",
                                            stderr="boom" if self.returncode else "")


#: The service set the real stack runs, per the plan's post-slimming pass —
#: no penpot-mcp, no penpot-mailcatch.
SERVICES = ("penpot-frontend", "penpot-backend", "penpot-exporter",
            "penpot-postgres", "penpot-valkey")


@pytest.fixture
def config(tmp_path):
    (tmp_path / "docker-compose.yaml").write_text("services: {}\n")
    return stack.StackConfig(
        compose_dir=tmp_path,
        project="penpot-local",
        compose_file="docker-compose.yaml",
        override_files=("docker-compose.local.yml",),
        env_file=".env.local",
        services=SERVICES,
    )


# -- reconcile(): container health, not a pid --------------------------


def test_reconcile_reports_unhealthy_when_a_container_is_down(config):
    """Prevents an exited-but-still-listed container (an OOM-killed postgres,
    say) from being folded into a "stack is fine" report just because
    `docker compose ps --all` still has a row for it."""
    entries = [
        _entry("penpot-frontend", "running", "healthy"),
        _entry("penpot-backend", "running", "healthy"),
        _entry("penpot-exporter", "running"),
        _entry("penpot-postgres", "exited", exit_code=1),
        _entry("penpot-valkey", "running", "healthy"),
    ]
    s = stack.Stack(config, runner=FakeRunner(ps_entries=entries))
    result = s.reconcile()
    assert result["stack_state"] == "unhealthy"
    assert result["unhealthy"] == ["penpot-postgres"]
    assert result["missing"] == []


def test_reconcile_reports_healthy_only_when_every_expected_service_is_up(config):
    """Baseline for the two failure tests above/below: a stack where every
    expected container is present and running must not be misreported as
    unhealthy just because one of them carries no healthcheck at all
    (`Health: ""`, which is normal for postgres/valkey here)."""
    entries = [_entry(name, "running", "healthy" if name == "penpot-frontend" else None)
               for name in SERVICES]
    s = stack.Stack(config, runner=FakeRunner(ps_entries=entries))
    result = s.reconcile()
    assert result["stack_state"] == "healthy"
    assert result["missing"] == []
    assert result["unhealthy"] == []


def test_stopped_stack_is_distinguishable_from_unhealthy_stack(config):
    """Prevents an operator's "just start it" action from looking identical
    to a "half the stack crashed" alert in `status()` — the former needs a
    plain `up -d`, the latter needs figuring out which container died."""
    stopped = stack.Stack(config, runner=FakeRunner(ps_entries=[]))
    assert stopped.reconcile()["stack_state"] == "stopped"

    limping = stack.Stack(config, runner=FakeRunner(ps_entries=[
        _entry("penpot-frontend", "running", "healthy"),
        _entry("penpot-backend", "exited", exit_code=137),
    ]))
    assert limping.reconcile()["stack_state"] == "unhealthy"


# -- compose invocation shape -------------------------------------------


def test_compose_invocation_carries_project_and_override_files(config):
    """Prevents a start that silently targets the wrong compose project, or
    drops the override file carrying the loopback port bindings and memory
    caps, because the argv was rebuilt ad hoc at the call site instead of
    going through `StackConfig.compose_args` every time."""
    fake = FakeRunner(ps_entries=[_entry(name, "running", "healthy") for name in SERVICES])
    s = stack.Stack(config, runner=fake)
    s.start(wait=False)
    assert fake.calls[0] == [
        "docker", "compose", "-p", "penpot-local",
        "-f", "docker-compose.yaml", "-f", "docker-compose.local.yml",
        "--env-file", ".env.local", "up", "-d",
    ]


def test_stop_tears_down_rather_than_pausing(config):
    """Prevents `stop()` from quietly becoming `docker compose stop` (which
    leaves containers present-but-exited) — that would make a deliberate
    stop indistinguishable from a crash in `reconcile()`'s three-way
    classification, defeating the whole point of the `stopped` state."""
    fake = FakeRunner()
    s = stack.Stack(config, runner=fake)
    s.stop()
    assert fake.calls[0] == [
        "docker", "compose", "-p", "penpot-local",
        "-f", "docker-compose.yaml", "-f", "docker-compose.local.yml",
        "--env-file", ".env.local", "down",
    ]


def test_logs_forwards_service_and_tail(config):
    """Prevents `logs(service=...)` from silently tailing every container
    when the caller asked for one — that turns a targeted debug request into
    a wall of unrelated output."""
    fake = FakeRunner()
    s = stack.Stack(config, runner=fake)
    s.logs(service="penpot-backend", tail=50)
    assert fake.calls[0][-3:] == ["--tail", "50", "penpot-backend"]


# -- failure surfaces -----------------------------------------------------


def test_start_refuses_when_no_compose_file_is_present(tmp_path):
    """Prevents `start()` from handing `docker compose` a `-f` path that
    doesn't exist and letting compose's own opaque error stand in for a
    clear one — this is the difference between "misconfigured" and "docker
    is broken" for whoever reads the exception."""
    missing = stack.StackConfig(compose_dir=tmp_path, services=SERVICES)
    s = stack.Stack(missing, runner=FakeRunner())
    with pytest.raises(FileNotFoundError):
        s.start(wait=False)


def test_containers_treats_an_unreachable_docker_as_no_containers(config):
    """Prevents a missing `docker` binary (or a daemon that refuses to
    answer) from raising out of `reconcile()` — the caller must see "not
    up", the same as a genuinely stopped stack, not an unhandled exception."""
    def _raise(args, **kwargs):
        raise FileNotFoundError("docker: command not found")
    s = stack.Stack(config, runner=_raise)
    assert s.containers() == {}
    assert s.reconcile()["stack_state"] == "stopped"


# -- ps output parsing ------------------------------------------------------


def test_parse_ps_output_accepts_both_json_shapes():
    """Prevents a compose-version difference (JSON-array output vs. one JSON
    object per line) from silently reading every container as absent —
    guessing the wrong shape here looks exactly like a stopped stack."""
    entries = [_entry("penpot-frontend", "running", "healthy"),
               _entry("penpot-backend", "running", "healthy")]
    as_array = json.dumps(entries)
    as_jsonl = "\n".join(json.dumps(e) for e in entries)
    assert stack._parse_ps_output(as_array) == entries
    assert stack._parse_ps_output(as_jsonl) == entries
    assert stack._parse_ps_output("") == []


# --- the operator's stop must survive the supervisor -----------------------

def test_a_held_stop_is_visible_in_status(config):
    """An operator who stopped the stack must be able to tell a stack that
    will stay down from one the supervision loop is about to bring back."""
    s = stack.Stack(config, runner=FakeRunner(ps_entries=[]))
    assert s.status()["held"] is False
    s.stop(hold=True)
    assert s.status()["held"] is True


def test_start_releases_a_held_stop(config):
    """The hold must not be a state an operator can wedge the stack into."""
    s = stack.Stack(config, runner=FakeRunner(ps_entries=[
        _entry(name, "running") for name in SERVICES]))
    s.stop(hold=True)
    assert s._held is True
    s.start(wait=False)
    assert s._held is False


def test_the_stop_verb_holds_the_stack_down():
    """The bug this pins was found live: `stop` returned `stack_state:
    stopped`, and the supervision loop had the whole stack back up nine
    seconds later, because the handler took `Stack.stop`'s non-holding
    default. The verb is only meaningful if it holds."""
    import asyncio

    from awm.penpot import hub_adapter

    calls: list[dict] = []

    class _Spy:
        def stop(self, **kwargs):
            calls.append(kwargs)
            return {"action": "stopped"}

    original = hub_adapter.STACK
    hub_adapter.STACK = _Spy()
    try:
        asyncio.run(hub_adapter._h_stop({}, as_=None))
    finally:
        hub_adapter.STACK = original
    assert calls == [{"hold": True}]


def test_a_held_stop_survives_a_respawn(config, tmp_path, monkeypatch):
    """The gateway respawns this service on any crash, deploy or restart. A
    hold kept only in memory would un-hold there -- exactly when nobody is
    watching -- and `_on_start` would bring the stack back up."""
    hold = tmp_path / "held"
    monkeypatch.setattr(stack, "HOLD_FILE", hold)
    first = stack.Stack(config, runner=FakeRunner(ps_entries=[]))
    first.stop(hold=True)
    assert hold.exists()

    respawned = stack.Stack(config, runner=FakeRunner(ps_entries=[]))
    assert respawned.held is True

    respawned.start(wait=False)
    assert respawned.held is False
    assert not hold.exists()


def test_a_failed_down_does_not_record_a_hold(config, tmp_path, monkeypatch):
    """Recording the hold before the `down` would leave an operator told the
    stack is held stopped while every container is still running."""
    monkeypatch.setattr(stack, "HOLD_FILE", tmp_path / "held")
    s = stack.Stack(config, runner=FakeRunner(ps_entries=[], returncode=1))
    result = s.stop(hold=True)
    assert result["action"] == "stop-failed"
    assert s.held is False
    assert not (tmp_path / "held").exists()
