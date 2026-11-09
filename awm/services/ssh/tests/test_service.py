from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
from pathlib import Path

import pytest

from awm.ssh import config as ssh_config
from awm.ssh import service as ssh_service
from awm.ssh.config import resolve_host
from awm.ssh.service import ConnState, SSHService

_NO_2FA_VPN_HOST = ssh_config.HostConfig(
    "nodevice", "u", needs_vpn=True, vpn_profile="ubc")


async def _coro(value):
    """Wrap a plain value so it can stand in for an async method's return."""
    return value


async def _false(_host):
    return False


async def _true(_host):
    return True


async def _noop_alert(_text):
    return None


def _lease_from(monkeypatch, status="granted") -> list:
    """Answer the lease as ``ssh-arbiter`` would. Returns the verdicts it receives."""
    verdicts: list = []

    class _Lease:
        granted, reason = status == "granted", None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def verdict(self, ok, reason=""):
            verdicts.append((ok, reason))

    _Lease.status = status

    async def _acquire(service, host, *, peer, node):
        assert (service, peer) == ("ssh-arbiter", "alcor")
        return _Lease()

    monkeypatch.setenv("AWM_SSH_SLOT_PEER", "alcor")
    monkeypatch.setattr(ssh_service.gatewayclient, "acquire_lease", _acquire)
    return verdicts


def _spawned() -> "ssh_service._AttemptRecord":
    """An attempt record for a connect that DID reach the ssh exec.

    The classification tests are all about what ssh's own output means, which
    presupposes ssh ran. A default record means the opposite — that nothing was
    spawned — and short-circuits the predicate to True.
    """
    return ssh_service._AttemptRecord(spawned=True)


class TestConfig:
    def test_unknown_host_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown host"):
            resolve_host("nosuchhost")

    def test_fir_is_guarded_others_are_not(self) -> None:
        # Only MFA-lockout-prone fir carries the direct-ssh guard flag.
        assert resolve_host("fir").guarded is True
        assert resolve_host("sockeye").guarded is False
        assert resolve_host("sockeye1").guarded is False

@pytest.fixture(autouse=True)
def _no_live_gateway(monkeypatch):
    """No test may reach the live fleet.

    The service now asks the 2fa service to attribute a failure and to verify
    the approver's health. Left unpatched, those calls hit whatever gateway is
    running on the developer's machine, and the suite's verdict starts
    depending on whether Duo happens to be reachable right now. Tests that
    care about those answers patch this themselves — a later setattr wins.
    """
    async def _refuse(*_a, **_kw):
        raise RuntimeError("test tried to reach the live gateway")

    monkeypatch.setattr(ssh_service.gatewayclient, "call", lambda tool, verb, args=None, *, peer=None, **kw: (_refuse)(peer, tool, verb, args, **kw))
    monkeypatch.setattr(ssh_service.gatewayclient, "call", _refuse)


_REAL_DETACH_PREFIX = SSHService._detach_prefix


@pytest.fixture(autouse=True)
def _no_systemd_scope(monkeypatch):
    """Spawn masters bare, as on a node without a user systemd manager.

    The real probe runs `systemd-run`, which the spawn mocks below would
    swallow. TestMasterScope exercises the real method."""
    async def _bare(self, cfg):
        return []

    monkeypatch.setattr(SSHService, "_detach_prefix", _bare)


@pytest.fixture
def isolated_dirs(tmp_path, monkeypatch):
    """Redirect the live dir so tests never touch the real ~/.ssh."""
    for mod in (ssh_config, ssh_service):
        monkeypatch.setattr(mod, "LIVE_DIR", str(tmp_path), raising=False)
    return tmp_path


class TestLeaseVerdict:
    @pytest.mark.parametrize("status", ["locked", "busy"])
    async def test_a_refused_lease_does_no_work(
            self, isolated_dirs, monkeypatch, status) -> None:
        svc = SSHService()

        async def _no_attempt(*a, **k):
            raise AssertionError("no VPN, 2FA or ssh without the slot")

        monkeypatch.setattr(svc, "_check_master", _false)
        monkeypatch.setattr(svc, "_do_connect_attempt", _no_attempt)
        verdicts = _lease_from(monkeypatch, status)

        result = await svc.connect("fir")
        # Opaque to the caller: no mechanism, no unlock hint.
        assert result["status"] == "unavailable"
        assert "lock" not in result["error"].lower()
        assert verdicts == []

    async def test_a_preauth_failure_frees_the_slot(
            self, isolated_dirs, monkeypatch) -> None:
        """Seen live: both fir login nodes at `Exceeded MaxStartups`, no push fired."""
        live = isolated_dirs
        svc = SSHService()

        async def _fail_attempt(c, marker, rec):
            # After the spawn, because the attempt truncates the capture on entry.
            rec.spawned = True
            (live / "fir.connect.stderr").write_text(
                "kex_exchange_identification: Connection closed by remote host\n")
            raise ssh_service._AttemptFailed("no master")

        monkeypatch.setattr(svc, "_check_master", _false)
        monkeypatch.setattr(svc, "_attempt_master", _fail_attempt)
        verdicts = _lease_from(monkeypatch)

        result = await svc.connect("fir")
        assert result["status"] == "error"
        assert "safe to retry" in result["error"]
        assert [ok for ok, _ in verdicts] == [True]

    async def test_an_auth_failure_reports_failure_with_its_reason(
            self, isolated_dirs, monkeypatch) -> None:
        live = isolated_dirs
        svc = SSHService()

        async def _fail_attempt(c, marker, rec):
            rec.spawned = True
            (live / "fir.connect.stderr").write_text(
                "phyberos@fir: Permission denied (keyboard-interactive).\n")
            raise ssh_service._AttemptFailed("no master")

        monkeypatch.setattr(svc, "_check_master", _false)
        monkeypatch.setattr(svc, "_attempt_master", _fail_attempt)
        verdicts = _lease_from(monkeypatch)

        result = await svc.connect("fir")
        assert result["status"] == "error"
        assert "_lock_reason" not in result and "_preauth" not in result
        assert verdicts[0][0] is False and "Permission denied" in verdicts[0][1]

    async def test_no_arbiter_means_no_login(
            self, isolated_dirs, monkeypatch) -> None:
        monkeypatch.delenv("AWM_SSH_SLOT_PEER", raising=False)
        svc = SSHService()

        async def _no_attempt(cfg):
            raise AssertionError("no login without an arbiter")

        monkeypatch.setattr(svc, "_do_connect_attempt", _no_attempt)
        assert (await svc._do_connect(resolve_host("fir")))["status"] == "error"

    def test_the_verbs_are_only_direct_ssh(self) -> None:
        # Agents must not be able to clear their own hold.
        from awm.ssh import hub_adapter
        assert set(hub_adapter.HANDLERS) == {"connect", "disconnect", "status"}
        verbs = {f["name"] for f in hub_adapter.API_MANIFEST["functions"]}
        assert verbs == set(hub_adapter.HANDLERS)

class TestPreAuthClassification:
    """`_is_preauth_failure` decides whether a failed connect spent an MFA attempt.

    The two mistakes are NOT symmetric: calling an auth failure "pre-auth" means we
    keep retrying and burn the budget toward a real provider lockout, while calling
    a pre-auth failure "auth" merely costs a spurious /approve. Every test here
    pins the conservative direction.
    """

    def _svc_cfg(self, live, stderr: str | None):
        svc = SSHService()
        cfg = resolve_host("fir")
        if stderr is not None:
            (live / "fir.connect.stderr").write_text(stderr)
        return svc, cfg

    @pytest.mark.parametrize("blob", [
        "kex_exchange_identification: Connection closed by remote host",
        "Exceeded MaxStartups",
        "ssh: connect to host fir port 22: Connection refused",
        "ssh: Could not resolve hostname fir: Name or service not known",
        "ssh: connect to host fir port 22: No route to host",
        "ssh: connect to host fir port 22: Network is unreachable",
        "Host key verification failed.",
        "@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@",
    ])
    def test_unambiguous_preauth_markers(self, isolated_dirs, blob) -> None:
        live = isolated_dirs
        svc, cfg = self._svc_cfg(live, blob)
        assert svc._is_preauth_failure(cfg, svc._deviation_marker(cfg), _spawned()) is True

    @pytest.mark.parametrize("blob", [
        "Permission denied (keyboard-interactive).",
        "Too many authentication failures",
        "Authentication failed.",
        "Your account is locked",
        "Duo two-factor login for phyberos",
        "MFA required",
    ])
    def test_auth_phase_markers_are_not_preauth(self, isolated_dirs, blob) -> None:
        live = isolated_dirs
        svc, cfg = self._svc_cfg(live, blob)
        assert svc._is_preauth_failure(cfg, svc._deviation_marker(cfg), _spawned()) is False

    def test_auth_marker_vetoes_a_preauth_marker(self, isolated_dirs) -> None:
        """THE safety-critical case: if both appear, auth wins and we hold. An ssh
        run that failed auth and then lost the connection must never read as
        'spent nothing, retry freely'."""
        live = isolated_dirs
        svc, cfg = self._svc_cfg(
            live,
            "Permission denied (keyboard-interactive).\n"
            "kex_exchange_identification: Connection closed by remote host\n")
        assert svc._is_preauth_failure(cfg, svc._deviation_marker(cfg), _spawned()) is False

    def test_askpass_deviation_decides_when_ssh_named_no_phase(
            self, isolated_dirs) -> None:
        """With no phase marker in ssh's own stderr, the deviation marker is the
        only evidence and its conservative reading stands: hold."""
        live = isolated_dirs
        svc, cfg = self._svc_cfg(live, "something we do not recognise")
        marker = svc._deviation_marker(cfg)
        Path(marker).write_text("deviation")
        assert svc._is_preauth_failure(cfg, marker, _spawned()) is False

    def test_a_duo_menu_deviation_still_vetoes(self, isolated_dirs) -> None:
        """The askpass's other refusal reasons all follow a real Duo menu, so they
        keep holding — narrowing one case must not loosen those."""
        live = isolated_dirs
        svc, cfg = self._svc_cfg(live, "Host key verification failed.")
        marker = svc._deviation_marker(cfg)
        Path(marker).write_text(
            "device 'alliance' is on Duo push-hold (auto-clears 900s)")
        assert svc._is_preauth_failure(cfg, marker, _spawned()) is False

    def test_missing_stderr_is_not_preauth(self, isolated_dirs) -> None:
        """No evidence is not evidence of safety — hold."""
        live = isolated_dirs
        svc, cfg = self._svc_cfg(live, None)
        assert svc._is_preauth_failure(cfg, svc._deviation_marker(cfg), _spawned()) is False

class TestStateMachine:
    """The connect/disconnect state machine: dedup, breaker-safe timeout,
    disconnect-during-auth deferral, and the CONNECTED liveness probe."""

    async def test_concurrent_connects_dedup_to_one_attempt(
            self, isolated_dirs, monkeypatch) -> None:
        svc = SSHService()
        cfg = resolve_host("fir")
        calls = {"n": 0}

        async def _one_attempt(c):
            calls["n"] += 1
            await asyncio.sleep(0.01)  # keep it AUTHENTICATING while peers arrive
            return SSHService._status_dict(c, "connected")

        monkeypatch.setattr(svc, "_check_master", _false)  # no existing master
        monkeypatch.setattr(svc, "_do_connect", _one_attempt)

        results = await asyncio.gather(
            svc.connect("fir"), svc.connect("fir"), svc.connect("fir"))

        assert calls["n"] == 1                              # exactly one ssh auth
        assert all(r["status"] == "connected" for r in results)
        assert svc._host("fir").state == ConnState.CONNECTED

    async def test_attempt_timeout_reports_a_failed_verdict(
            self, isolated_dirs, monkeypatch) -> None:
        svc = SSHService()

        async def _hang(c, marker, rec):   # never brings up the master
            rec.spawned = True
            await asyncio.sleep(5.0)

        monkeypatch.setattr(ssh_service, "_CONNECT_TIMEOUT", 0.05)
        monkeypatch.setattr(svc, "_check_master", _false)
        monkeypatch.setattr(svc, "_attempt_master", _hang)
        monkeypatch.setattr(svc, "_exit_master", _true)      # reap is a no-op here
        verdicts = _lease_from(monkeypatch)

        result = await svc.connect("fir")
        # An internal timeout is a verdict, never an outer cancel that skips one.
        assert result["status"] == "error"
        assert [ok for ok, _ in verdicts] == [False]
        assert svc._host("fir").state == ConnState.DISCONNECTED

    async def test_disconnect_during_auth_defers_then_disposes(
            self, isolated_dirs, monkeypatch) -> None:
        svc = SSHService()
        gate = asyncio.Event()
        completed = {"v": False}

        async def _held_attempt(c):
            await gate.wait()            # stay in AUTHENTICATING until released
            completed["v"] = True
            return SSHService._status_dict(c, "connected")

        monkeypatch.setattr(svc, "_check_master", _false)
        monkeypatch.setattr(svc, "_do_connect", _held_attempt)
        monkeypatch.setattr(svc, "_exit_master", _true)

        ctask = asyncio.create_task(svc.connect("fir"))
        for _ in range(200):
            await asyncio.sleep(0.001)
            if svc._host("fir").state == ConnState.AUTHENTICATING:
                break
        assert svc._host("fir").state == ConnState.AUTHENTICATING

        dtask = asyncio.create_task(svc.disconnect("fir"))
        await asyncio.sleep(0.02)
        # The in-flight auth is NOT aborted — the disconnect only records intent.
        assert svc._host("fir").pending_disconnect is True
        assert not ctask.done()

        gate.set()
        cres = await ctask
        dres = await dtask
        assert cres["status"] == "connected"
        assert completed["v"] is True            # attempt ran to completion
        assert dres["status"] == "disconnected"
        assert svc._host("fir").state == ConnState.DISCONNECTED

    async def test_late_master_is_adopted_not_tripped(
            self, isolated_dirs, monkeypatch) -> None:
        # If the ControlMaster appears just after the poll window gives up
        # (_AttemptFailed), _do_connect must RE-CHECK and adopt it as connected —
        # not trip the breaker on an auth that actually succeeded late (which would
        # waste a live connection and spuriously page the operator).
        svc = SSHService()
        cfg = resolve_host("fir")
        checks = {"n": 0}

        async def _fail_to_bring_up(c, marker, rec):
            rec.spawned = True
            raise ssh_service._AttemptFailed("ControlMaster did not appear")

        async def _master_late(_host):
            checks["n"] += 1
            return checks["n"] > 1   # False on probe_start, True on the adopt re-check

        monkeypatch.setattr(svc, "_check_master", _master_late)
        monkeypatch.setattr(svc, "_attempt_master", _fail_to_bring_up)
        _lease_from(monkeypatch)
        result = await svc.connect("fir")
        assert result["status"] == "connected"       # adopted, not errored
        assert svc._host("fir").state == ConnState.CONNECTED

    async def test_do_connect_raise_does_not_strand_authenticating(
            self, isolated_dirs, monkeypatch) -> None:
        svc = SSHService()

        async def _boom(_c):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(svc, "_check_master", _false)
        monkeypatch.setattr(svc, "_do_connect", _boom)

        res = await svc.connect("fir")
        assert res["status"] == "error"
        assert svc._host("fir").state == ConnState.DISCONNECTED


class TestSingletonAndReconcile:
    @pytest.fixture
    def isolated_singleton(self, tmp_path, monkeypatch):
        path = str(tmp_path / "awm-ssh.singleton")
        for mod in (ssh_config, ssh_service):
            monkeypatch.setattr(mod, "SINGLETON_PATH", path, raising=False)
        return path

    def test_second_instance_stands_down(
            self, isolated_singleton, monkeypatch) -> None:
        first = SSHService()
        first._acquire_singleton()
        assert first._singleton_fd is not None      # holds the flock

        class _Stood(Exception):
            pass

        def _fake_exit(code):
            raise _Stood()

        monkeypatch.setattr(ssh_service.os, "_exit", _fake_exit)
        second = SSHService()
        with pytest.raises(_Stood):
            second._acquire_singleton()             # contended → stands down

    async def test_reap_double_checks_before_removing_socket(
            self, isolated_dirs, monkeypatch) -> None:
        # A transient false-negative (a live master briefly not answering) must not
        # get its socket reaped — reap only removes a socket that fails twice.
        live = isolated_dirs
        svc = SSHService()
        sockpath = live / "fir.computecanada.ca_22_phyberos"
        s = socket.socket(socket.AF_UNIX)
        s.bind(str(sockpath))
        calls = {"n": 0}

        async def _flaky(_path):
            calls["n"] += 1
            return calls["n"] > 1    # first probe fails, second says alive

        monkeypatch.setattr(svc, "_check_socket", _flaky)
        try:
            await svc._reap_orphans()
        finally:
            s.close()

        assert sockpath.exists()     # survived the transient false-negative
        assert calls["n"] == 2       # it re-probed rather than reaping on one miss


_HOME = Path(os.path.expanduser("~"))
_GUARD = _HOME / ".ssh" / "awm-ssh-guard"
_ASKPASS = _HOME / ".ssh" / "awm-duo-askpass"


class TestGuardScripts:
    """Behavioural checks on the installed shell helpers (skip if absent).

    Every one of these points the askpass at a throwaway AWM_DUO_LOCK_DIR. It
    keeps its rate-cap state there, and answering counts toward a cap that trips
    a 30-minute push-hold refusing ALL Duo answers for the device. Run against
    the default dir, three runs of this suite inside ten minutes therefore break
    the real login path — which is exactly what happened on 2026-09-01.
    """

    @pytest.mark.skipif(not _GUARD.exists(), reason="guard not installed")
    def test_guard_exits_nonzero_and_is_loud(self) -> None:
        r = subprocess.run([str(_GUARD), "fir", "22"], capture_output=True,
                           text=True)
        assert r.returncode != 0
        assert "BLOCKED" in r.stderr

    @pytest.mark.skipif(not _ASKPASS.exists(), reason="askpass not installed")
    def test_askpass_refuses_unrecognized_prompt(self, tmp_path) -> None:
        marker = tmp_path / "mk"
        r = subprocess.run([str(_ASKPASS), "password: "],
                           capture_output=True, text=True,
                           env={**os.environ,
                                "AWM_SSH_ASKPASS_MARKER": str(marker),
                                "AWM_DUO_LOCK_DIR": str(tmp_path)})
        assert r.returncode != 0
        assert r.stdout.strip() == ""
        assert marker.exists()

    @pytest.mark.skipif(not _ASKPASS.exists(), reason="askpass not installed")
    def test_askpass_refuses_when_no_device_matches(self, tmp_path) -> None:
        marker = tmp_path / "mk"
        prompt = ("Enter a passcode or select one of the following options:\n"
                  " 1. Duo Push to SomeoneElse\n 2. Phone call\n"
                  "Passcode or option (1-2): ")
        r = subprocess.run([str(_ASKPASS), prompt], capture_output=True,
                           text=True,
                           env={**os.environ,
                                "AWM_SSH_ASKPASS_MARKER": str(marker),
                                "AWM_DUO_LOCK_DIR": str(tmp_path)})
        assert r.returncode != 0
        assert r.stdout.strip() == ""
        assert marker.exists()


class _FakeProc:
    """An ssh that never finishes authenticating, and remembers being signalled."""

    def __init__(self, pid: int = 987654) -> None:
        self.pid = pid
        self.signals: list[int] = []

    async def wait(self):
        await asyncio.sleep(30.0)
        return 0


class TestChildReaping:
    """A connect that fails must leave no ssh behind.

    The process is spawned inside a coroutine the 120s wait_for cancels. Before
    this, the handle was a local in that frame and the frame simply went away —
    and the one cleanup on the path, `ssh -O exit`, speaks through a socket a
    pre-auth-hung ssh has never created. So it survived, holding an armed Duo
    window, until something killed the whole service.
    """

    def _patch_spawn(self, monkeypatch, proc):
        async def _spawn(*a, **k):
            assert k.get("start_new_session") is True, (
                "the child must lead its own process group, or reaping the "
                "group kills the service itself")
            return proc

        monkeypatch.setattr(ssh_service.asyncio, "create_subprocess_exec", _spawn)

    def _patch_killpg(self, monkeypatch, proc, *, alive_after_term=False):
        """Stand in for the group signal. Unless *alive_after_term*, the group is
        gone once SIGTERM lands, which is what the kernel reports as
        ProcessLookupError and what stops the escalation."""
        def _killpg(pid, sig):
            assert pid == proc.pid
            if proc.signals and not alive_after_term:
                raise ProcessLookupError(pid)
            proc.signals.append(sig)

        monkeypatch.setattr(ssh_service.os, "killpg", _killpg)

    async def test_timeout_reaps_the_spawned_ssh(
            self, isolated_dirs, monkeypatch) -> None:
        proc = _FakeProc()
        svc = SSHService()
        cfg = _NO_2FA_VPN_HOST        # no 2FA device: no burst to fake
        self._patch_spawn(monkeypatch, proc)
        self._patch_killpg(monkeypatch, proc)
        monkeypatch.setattr(ssh_service, "_CONNECT_TIMEOUT", 0.05)
        monkeypatch.setattr(ssh_service, "_REAP_GRACE_S", 0.01)
        monkeypatch.setattr(svc, "_check_master", _false)
        monkeypatch.setattr(svc, "_exit_master", _true)
        monkeypatch.setattr(ssh_service.gatewayclient, "call",
                            lambda *a, **k: _coro({"status": "up"}))

        await svc._do_connect_attempt(cfg)

        assert proc.signals == [ssh_service.signal.SIGTERM], \
            "a cancelled attempt must terminate the ssh it spawned, and stop " \
            "there once it is gone"

    async def test_a_process_that_ignores_sigterm_is_killed(
            self, isolated_dirs, monkeypatch) -> None:
        proc = _FakeProc()
        svc = SSHService()
        cfg = _NO_2FA_VPN_HOST
        self._patch_spawn(monkeypatch, proc)
        self._patch_killpg(monkeypatch, proc, alive_after_term=True)
        monkeypatch.setattr(ssh_service, "_CONNECT_TIMEOUT", 0.05)
        monkeypatch.setattr(ssh_service, "_REAP_GRACE_S", 0.01)
        monkeypatch.setattr(svc, "_check_master", _false)
        monkeypatch.setattr(svc, "_exit_master", _true)
        monkeypatch.setattr(ssh_service.gatewayclient, "call",
                            lambda *a, **k: _coro({"status": "up"}))

        await svc._do_connect_attempt(cfg)

        assert proc.signals == [ssh_service.signal.SIGTERM,
                                ssh_service.signal.SIGKILL]

    async def test_boot_leaves_a_pid_that_is_not_ours_alone(
            self, isolated_dirs, monkeypatch) -> None:
        """A pid is the least durable key there is — reuse must not kill a stranger."""
        live = isolated_dirs
        (live / "fir.master.pid").write_text("31337\n")
        svc = SSHService()
        reaped: list[int] = []

        monkeypatch.setattr(svc, "_is_our_master", lambda pid: False)
        monkeypatch.setattr(svc, "_reap_group",
                            lambda pid, host: _coro(reaped.append(pid)))

        await svc._reap_stranded_masters()

        assert reaped == []
        assert not (live / "fir.master.pid").exists()

class TestNothingWasSpent:
    """The breaker must hold a host only when an MFA attempt could be spent."""

    async def test_a_failure_before_the_spawn_is_never_a_hold(
            self, isolated_dirs, monkeypatch) -> None:
        """A dependency that fails before ssh runs cannot have fired a push.

        Seen live 2026-09-01: a dependency answered HTTP 504, ssh never ran, and
        the arbiter held sockeye fleet-wide and paged the operator for it.
        """
        svc = SSHService()
        cfg = resolve_host("sockeye")

        async def _dependency_times_out(c, marker, rec):
            raise RuntimeError("gateway call 2fa/burst failed: HTTP 504")

        monkeypatch.setattr(svc, "_attempt_master", _dependency_times_out)
        monkeypatch.setattr(svc, "_check_master", _false)

        result = await svc._do_connect_attempt(cfg)

        assert result["status"] == "error"
        assert result["_preauth"] is True, "nothing ran, so nothing can have been spent"
        assert "safe to retry" in result["error"]

    def _fake_2fa_status(self, monkeypatch, seen):
        async def _call(_peer, service, fn, args=None, **kw):
            if service == "2fa" and fn == "status":
                return {} if seen is None else {"transactions_seen": seen}
            return {}

        monkeypatch.setattr(ssh_service.gatewayclient, "call", lambda tool, verb, args=None, *, peer=None, **kw: (_call)(peer, tool, verb, args, **kw))

    async def _saw_nothing(self, monkeypatch, *, armed, now):
        svc = SSHService()
        cfg = resolve_host("fir")
        self._fake_2fa_status(monkeypatch, now)
        rec = ssh_service._AttemptRecord(spawned=True, twofa_seen_at_arm=armed)
        return await svc._duo_saw_nothing(cfg, rec)

    async def test_a_duo_transaction_still_holds(
            self, isolated_dirs, monkeypatch) -> None:
        assert await self._saw_nothing(monkeypatch, armed=7, now=8) is False

    async def test_an_unaskable_count_is_not_zero(
            self, isolated_dirs, monkeypatch) -> None:
        """No evidence is not evidence of none — the protective default stands."""
        assert await self._saw_nothing(monkeypatch, armed=None, now=7) is False
        assert await self._saw_nothing(monkeypatch, armed=7, now=None) is False

    async def test_a_count_that_went_backwards_is_not_zero(
            self, isolated_dirs, monkeypatch) -> None:
        """A lower reading means the approver restarted, not that nothing ran."""
        assert await self._saw_nothing(monkeypatch, armed=7, now=2) is False

    async def test_an_unreachable_approver_is_not_zero(
            self, isolated_dirs, monkeypatch) -> None:
        async def _boom(*a, **k):
            raise RuntimeError("2fa unreachable")

        monkeypatch.setattr(ssh_service.gatewayclient, "call", lambda tool, verb, args=None, *, peer=None, **kw: (_boom)(peer, tool, verb, args, **kw))
        svc = SSHService()
        rec = ssh_service._AttemptRecord(spawned=True, twofa_seen_at_arm=7)
        assert await svc._duo_saw_nothing(resolve_host("fir"), rec) is False

class TestDependenciesBeforeTheLogin:
    """What a login needs is named when it fails, and is never the host's fault.

    On 2026-10-16 the UBC tunnel was down, the vpn dial outlived the 30s
    gateway default, and every connect to sockeye answered "sockeye refused the
    connection" — so agents went looking at sockeye.
    """

    async def test_a_refused_jump_names_the_tunnel_and_spends_nothing(
            self, isolated_dirs, monkeypatch) -> None:
        """Altair's tunnel down: the jump host cannot reach the bounce, so sockeye never saw a packet."""
        svc = SSHService()
        cfg = resolve_host("sockeye")

        async def _jump_refused(c, marker, rec):
            rec.spawned = True
            with open(ssh_service.stderr_path(c), "w") as f:
                f.write("channel 0: open failed: connect failed: Connection refused\n"
                        "stdio forwarding failed\n"
                        "kex_exchange_identification: Connection closed by remote host\n")
            raise ssh_service._AttemptFailed("ControlMaster did not appear within 60s")

        monkeypatch.setattr(svc, "_attempt_master", _jump_refused)
        monkeypatch.setattr(svc, "_check_master", _false)

        result = await svc._do_connect_attempt(cfg)

        assert result["_preauth"] is True
        assert "ubc VPN tunnel on altair is down" in result["error"]
        assert "no MFA attempt was spent" in result["error"]

    async def test_an_unreachable_approver_is_named_before_any_ssh(
            self, isolated_dirs, monkeypatch) -> None:
        svc = SSHService()
        cfg = resolve_host("fir")
        spawned: list = []

        async def _mira_restarting(*a, **k):
            raise ConnectionError("All connection attempts failed")

        async def _spawn(*a, **k):
            spawned.append(a)
            raise AssertionError("no push may fire without the approver")

        monkeypatch.setattr(ssh_service.gatewayclient, "call",
                            lambda tool, verb, args=None, *, peer=None, **kw: (_mira_restarting)(peer, tool, verb, args, **kw))
        monkeypatch.setattr(ssh_service.asyncio, "create_subprocess_exec", _spawn)
        monkeypatch.setattr(svc, "_check_master", _false)

        result = await svc._do_connect_attempt(cfg)

        assert result["status"] == "error"
        assert "Duo auto-approver" in result["error"]
        assert result["_preauth"] is True, "the slot is freed, not held"
        assert spawned == []

class TestMasterArgv:
    def test_a_guarded_host_overrides_the_guard(self):
        argv = ssh_service.master_argv(ssh_config.KNOWN_HOSTS["fir"])
        assert argv[-3:] == ["-o", "ProxyCommand=none", "fir"]
        assert "-J" not in argv
