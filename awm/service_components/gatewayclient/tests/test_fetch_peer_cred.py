"""Tests for ``fetch_peer_cred`` hardening — the cred fetch that gates the ssh
service's slot arbiter.

The arbiter fails CLOSED: if it cannot reach the arbiter peer it refuses the ssh
connect outright. That makes this fetch's robustness load-bearing — a momentary
route blip (WSL2 NAT churn when a docker network comes up, say) must not read as
"arbiter down" and refuse a legitimate connect to a lockout-prone host.

Three properties are asserted here against a fake ``subprocess.run`` (no real
ssh, no sockets):

1. **Retry, but only when a retry could help** — ssh exit 255 (ssh's own
   "couldn't connect", distinct from the remote command's status) and a
   subprocess timeout are transient; a non-255 exit or an empty cred is the peer
   answering definitively and must raise at once. Retrying is safe ONLY because
   this fetch is a plain ``cat`` — it spends no MFA attempt and cannot advance a
   lockout counter.
2. **Fail-fast connects** — ssh must be invoked with an explicit
   ``ConnectTimeout``; peer aliases otherwise inherit ``connecttimeout none`` and
   a blackholed route burns the entire subprocess timeout.
3. **Off the event loop** — ``fetch_peer_cred_async`` must not block the loop,
   or one slow peer freezes every other host the ssh service is managing.

Sync tests driven via ``asyncio.run`` (the dist carries no pytest-asyncio
config), matching ``test_subscribe_peer.py``.
"""

from __future__ import annotations

import asyncio
import subprocess
import time

import pytest

from awm import gatewayclient as gc


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def fake_run_factory(outcomes, calls):
    """``subprocess.run`` stand-in replaying ``outcomes`` in order. An outcome is
    a FakeCompleted to return or an Exception to raise."""

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        out = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        if isinstance(out, Exception):
            raise out
        return out

    return fake_run


@pytest.fixture(autouse=True)
def _clear_cred_cache(monkeypatch):
    """The cred cache is module-global and positive-only; a hit would mask the
    fetch behaviour under test. Also collapse backoff so retry tests don't sleep."""
    gc._peer_cred_cache.clear()
    monkeypatch.setattr(gc, "_PEER_CRED_BACKOFF", 0.0)
    yield
    gc._peer_cred_cache.clear()


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


def test_retries_transient_exit_255_then_succeeds(monkeypatch):
    """ssh exit 255 is ssh failing to connect — retry, and a later success wins."""
    calls: list = []
    outcomes = [
        FakeCompleted(255, "", "ssh: connect to host miraz port 22: Connection timed out"),
        FakeCompleted(0, "CRED-abc\n", ""),
    ]
    monkeypatch.setattr(subprocess, "run", fake_run_factory(outcomes, calls))

    assert gc.fetch_peer_cred("miraz") == "CRED-abc"
    assert len(calls) == 2, "expected one retry after the transient 255"


def test_retries_subprocess_timeout_then_succeeds(monkeypatch):
    """A blackholed route trips the subprocess timeout — the exact shape of the
    observed 'connection arbiter mira unreachable' failures. Retryable."""
    calls: list = []
    outcomes = [
        subprocess.TimeoutExpired(cmd="ssh", timeout=15.0),
        FakeCompleted(0, "CRED-xyz\n", ""),
    ]
    monkeypatch.setattr(subprocess, "run", fake_run_factory(outcomes, calls))

    assert gc.fetch_peer_cred("miraz") == "CRED-xyz"
    assert len(calls) == 2


def test_gives_up_after_max_attempts_and_raises_peer_error(monkeypatch):
    """Fail-closed is preserved: exhausted retries still raise, so the arbiter
    refuses rather than proceeding without a lease."""
    calls: list = []
    monkeypatch.setattr(subprocess, "run",
                        fake_run_factory([FakeCompleted(255, "", "unreachable")], calls))

    with pytest.raises(gc.PeerError, match="failed within"):
        gc.fetch_peer_cred("miraz")
    assert len(calls) == gc._PEER_CRED_ATTEMPTS


def test_does_not_retry_definitive_nonzero_exit(monkeypatch):
    """The peer answered — the remote `cat` failed. Retrying burns the caller's
    fail-closed budget for an answer that will not change."""
    calls: list = []
    monkeypatch.setattr(subprocess, "run",
                        fake_run_factory([FakeCompleted(1, "", "cat: No such file")], calls))

    with pytest.raises(gc.PeerError) as exc:
        gc.fetch_peer_cred("miraz")
    assert "failed within" not in str(exc.value)
    assert len(calls) == 1, "a definitive error must not be retried"


# ---------------------------------------------------------------------------
# Total budget
# ---------------------------------------------------------------------------


def test_timeout_is_a_total_budget_not_per_attempt(monkeypatch):
    """Retries must fit INSIDE the caller's deadline. Without this the worst case
    multiplies (attempts x timeout) and a fail-closed caller — the slot arbiter
    refusing an ssh connect — hangs far longer than it used to."""
    calls: list = []

    def slow_fail(cmd, **kwargs):
        # Faithful to subprocess.run: block for at most the timeout we're handed,
        # then raise. An unfaithful fake that ignores it would overrun on its own
        # and prove nothing about the caller's budgeting.
        calls.append(cmd)
        budget = kwargs.get("timeout")
        time.sleep(min(0.2, budget))
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=budget)

    monkeypatch.setattr(subprocess, "run", slow_fail)
    monkeypatch.setattr(gc, "_PEER_CRED_BACKOFF", 0.0)

    start = time.monotonic()
    with pytest.raises(gc.PeerError):
        gc.fetch_peer_cred("miraz", timeout=0.5)
    elapsed = time.monotonic() - start

    assert calls, "no attempt was made"
    # The pre-fix code ran ONE 15s attempt; a naive retry would run three, so the
    # budget — not the attempt count — is what has to hold.
    assert elapsed < 0.5 + 0.15, (
        f"overran the 0.5s total budget ({elapsed:.2f}s) — retries must fit inside it")


def test_per_attempt_timeout_shrinks_to_remaining_budget(monkeypatch):
    """Each attempt gets only what's left, so attempt N cannot overrun the total."""
    seen: list = []

    def record(cmd, **kwargs):
        seen.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", record)
    monkeypatch.setattr(gc, "_PEER_CRED_BACKOFF", 0.0)

    with pytest.raises(gc.PeerError):
        gc.fetch_peer_cred("miraz", timeout=5.0)

    assert seen, "no attempt was made"
    assert seen[0] <= 5.0
    assert all(b <= a for a, b in zip(seen, seen[1:])), (
        f"per-attempt timeouts must be non-increasing, got {seen}")


def test_does_not_retry_empty_cred(monkeypatch):
    """$AWM_PEER_CRED unset on the peer is misconfiguration, not flakiness."""
    calls: list = []
    monkeypatch.setattr(subprocess, "run",
                        fake_run_factory([FakeCompleted(0, "  \n", "")], calls))

    with pytest.raises(gc.PeerError, match="empty"):
        gc.fetch_peer_cred("miraz")
    assert len(calls) == 1


def test_os_error_is_not_retried(monkeypatch):
    """Can't spawn ssh at all — a local defect; retrying cannot fix it."""
    calls: list = []
    monkeypatch.setattr(subprocess, "run",
                        fake_run_factory([OSError("no such binary: ssh")], calls))

    with pytest.raises(gc.PeerError):
        gc.fetch_peer_cred("miraz")
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Fail-fast connect
# ---------------------------------------------------------------------------


def test_passes_explicit_connect_timeout(monkeypatch):
    """Peer aliases inherit `connecttimeout none`, so without this a blackholed
    route hangs for the whole subprocess timeout instead of failing fast."""
    calls: list = []
    monkeypatch.setattr(subprocess, "run",
                        fake_run_factory([FakeCompleted(0, "CRED\n", "")], calls))

    gc.fetch_peer_cred("miraz")

    cmd = calls[0][0]
    assert "-o" in cmd and f"ConnectTimeout={gc._PEER_CRED_CONNECT_TIMEOUT}" in cmd
    assert "BatchMode=yes" in cmd, "must stay non-interactive"


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_second_call_hits_cache(monkeypatch):
    calls: list = []
    monkeypatch.setattr(subprocess, "run",
                        fake_run_factory([FakeCompleted(0, "CRED\n", "")], calls))

    assert gc.fetch_peer_cred("miraz") == "CRED"
    assert gc.fetch_peer_cred("miraz") == "CRED"
    assert len(calls) == 1, "the second call must be served from cache"


def test_force_bypasses_cache(monkeypatch):
    """The 401 rotation path depends on force= actually re-fetching."""
    calls: list = []
    outcomes = [FakeCompleted(0, "OLD\n", ""), FakeCompleted(0, "NEW\n", "")]
    monkeypatch.setattr(subprocess, "run", fake_run_factory(outcomes, calls))

    assert gc.fetch_peer_cred("miraz") == "OLD"
    assert gc.fetch_peer_cred("miraz", force=True) == "NEW"
    assert len(calls) == 2


def test_ttl_measured_after_the_round_trip(monkeypatch):
    """A slow/retried fetch must not cache a cred whose TTL already partly
    elapsed — the expiry is stamped after the ssh round-trip, not before it."""
    calls: list = []
    outcomes = [
        FakeCompleted(255, "", "unreachable"),
        FakeCompleted(0, "CRED\n", ""),
    ]
    monkeypatch.setattr(subprocess, "run", fake_run_factory(outcomes, calls))
    monkeypatch.setattr(gc, "_PEER_CRED_BACKOFF", 0.05)

    before = time.monotonic()
    gc.fetch_peer_cred("miraz")
    expiry = gc._peer_cred_cache["miraz"][0]

    assert expiry >= before + gc._PEER_CRED_TTL, (
        "expiry must be stamped after the retries, not from a pre-retry clock")


# ---------------------------------------------------------------------------
# Off the event loop
# ---------------------------------------------------------------------------


def test_async_fetch_does_not_block_the_event_loop(monkeypatch):
    """The whole point of the async wrapper: a slow ssh must not stall the loop.
    A concurrent heartbeat must keep ticking while the fetch is in flight."""
    def slow_run(cmd, **kwargs):
        time.sleep(0.3)  # stand-in for a blackholed route burning ConnectTimeout
        return FakeCompleted(0, "CRED\n", "")

    monkeypatch.setattr(subprocess, "run", slow_run)

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        hb = asyncio.create_task(heartbeat())
        cred = await gc.fetch_peer_cred_async("miraz")
        hb.cancel()
        return cred, ticks

    cred, ticks = asyncio.run(scenario())
    assert cred == "CRED"
    assert ticks > 3, (
        f"event loop was blocked during the fetch (only {ticks} heartbeats)")


def test_async_fetch_respects_monkeypatched_sync_fn(monkeypatch):
    """Callers patch the sync ``fetch_peer_cred`` in tests; the async wrapper must
    resolve it at call time so those patches keep taking effect."""
    monkeypatch.setattr(gc, "fetch_peer_cred", lambda alias, **k: "PATCHED")
    assert asyncio.run(gc.fetch_peer_cred_async("miraz")) == "PATCHED"
