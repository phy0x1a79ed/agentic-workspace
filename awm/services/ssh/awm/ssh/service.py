from __future__ import annotations

import asyncio
import enum
import fcntl
import json
import logging
import os
import secrets
import signal
import stat
import time
from dataclasses import dataclass
from typing import Any

from awm import config, gatewayclient
from awm.ssh.config import (
    KNOWN_HOSTS,
    LIVE_DIR,
    LOCK_DIR,
    SINGLETON_PATH,
    SSH_ASKPASS,
    HostConfig,
    lock_path,
    pid_path,
    resolve_host,
    stderr_path,
)

log = logging.getLogger("awm.ssh.service")

_CONNECT_TIMEOUT = 120.0
_CHECK_POLL_INTERVAL = 1.5
_CHECK_POLL_ATTEMPTS = 40
_DISCONNECT_POLL_ATTEMPTS = 8
# Hard cap on a teardown (`ssh -O exit` + confirm). Bounds the otherwise-unbounded
# `proc.communicate()` so a wedged control socket can't hang a teardown forever —
# which, on the connect timeout path, would strand the host in AUTHENTICATING with
# the breaker never tripping and every later connect absorbed onto a dead waiter.
_EXIT_TIMEOUT = 15.0
# How long a reaped ssh gets to die on SIGTERM before it is SIGKILLed. Short: the
# process we reap is by definition one that is not making progress.
_REAP_GRACE_S = 3.0
# Hard cap on a single `ssh -O check` probe. These are local control-socket probes
# (no remote round-trip) that normally return in well under a second, but a WEDGED
# master can make `-O check` block indefinitely. Because connect/disconnect now
# await boot reconciliation, which runs these probes, an unbounded probe would hang
# every verb forever — so each probe is bounded and a timeout is read as "not live".
_CHECK_TIMEOUT = 10.0
# Backstop: however slow boot reconciliation gets, a verb waits at most this long
# for it before proceeding (reconcile then continues in the background). With the
# per-probe cap above reconcile is already bounded; this only guards a pathological
# pile-up of simultaneously-wedged probes.
_RECONCILE_WAIT_TIMEOUT = 60.0

# Which node is speaking. Under federation two ssh services consume the same
# social/command stream and both will acknowledge an /approve; naming the node
# makes that informative rather than confusing. The name comes from the node env,
# not the OS hostname — mira's hostname is "pavilion", which named a machine
# nobody in the fleet calls mira.
_NODE_NAME = config.node_name()

# Discord notifications target for lockout alerts: unimatrix0#notifications.
_ALERT_ACCOUNT = "discord-bot"
_ALERT_CHANNEL = "1522674357762261112"

# Singleton re-homing selectors (federation). When 2fa / social run as canonical
# singletons on a peer node, this node borrows them by exporting these to the
# peer's name (e.g. AWM_TWOFA_PEER=mira, AWM_SOCIAL_PEER=mira). Unset/empty ⇒ the
# service is local and every call stays on this gateway. Read fresh per use via
# gatewayclient.peer_env so a reconnect picks up a change without a restart.
_TWOFA_PEER_ENV = "AWM_TWOFA_PEER"
_SOCIAL_PEER_ENV = "AWM_SOCIAL_PEER"
# The synthetic command name `social emit_probe` carries (mirrors social's
# PROBE_COMMAND). Not a registered slash command, so no operator can produce
# one and no domain handler will ever match it.
_PROBE_COMMAND = "__awm_probe__"

# Cause buckets recorded in the lockfile. A hold is a statement about WHY
# automated access is refused, and the two answers differ in what can lift it.
#   _CAUSE_EXTERNAL — the host, the credentials, or anything we cannot verify.
#     Only an operator /approve lifts it. This is the default for everything,
#     including an unrecognised cause: silence is never evidence.
#   _CAUSE_SELF — attributable to OUR OWN approver being unable to function
#     (Duo unreachable from this fleet). Verifiably gone once the approver is
#     healthy again, so requiring a human then is the trap the incident of
#     2026-07-26 walked into: the hold on fir recorded "connect exceeded 120s",
#     which was true but misattributed — nothing was wrong with fir.
_CAUSE_EXTERNAL = "external"
_CAUSE_SELF = "approver-unavailable"
# Connection-slot arbiter selector (federation). For a lockout-sensitive host,
# EXACTLY ONE attempt may be in flight across the whole fleet — the per-node
# breaker isn't enough once several nodes drive the same account. So a gated
# connect first acquires a slot from the single arbiter (ssh@<peer>). Set to the
# arbiter node's name (e.g. AWM_SSH_SLOT_PEER=mira) on every borrowing node; the
# node that OWNS the arbiter leaves it unset and acquires its slot in-process.
_SLOT_PEER_ENV = "AWM_SSH_SLOT_PEER"

# How long after a Discord /approve the operator-approval window stays open for
# a device. A locked host may re-connect only while this window is open. Kept
# ≥ the 2fa social burst window so the auto-approver is still armed for the
# connect the approval authorizes.
_APPROVE_WINDOW_SECONDS = 300.0

# Notable substrings in ssh stderr worth surfacing into ssh.log / the lock reason.
_NOTABLE_STDERR = (
    "Permission denied",
    "Too many authentication failures",
    "Authentication failed",
    "locked",
    "MFA",
    "Duo",
    "Connection refused",
    "Connection timed out",
    "Could not resolve hostname",
    "Operation timed out",
    "kex_exchange_identification",
    "Exceeded MaxStartups",
    "Host key verification failed",
)

# --- pre-auth vs auth failure classification -------------------------------
#
# The breaker exists for ONE reason: bound the MFA attempts spent toward provider
# lockout (fir re-locks at 10 failed Duo). A connect that dies BEFORE the auth
# phase spends nothing, so holding the host for it — and making the operator run
# `/approve` to clear it — is a false positive. Seen live 2026-07-15: both fir
# login nodes answered `Exceeded MaxStartups` (sshd's pre-auth connection cap),
# ssh died at `kex_exchange_identification`, zero Duo pushes fired, and the
# arbiter still locked fir fleet-wide.
#
# Classification is deliberately ASYMMETRIC, because the two mistakes are not
# equally bad:
#   * calling an AUTH failure "pre-auth"  -> we keep retrying and burn the MFA
#     budget toward a real lockout. CATASTROPHIC.
#   * calling a PRE-AUTH failure "auth"   -> a spurious hold + an /approve. Mild;
#     it is exactly today's behaviour.
# So: only these unambiguous markers count as pre-auth, and ANY auth-phase marker
# (below) VETOES the classification. Silence is not evidence — an empty stderr is
# treated as an auth failure, i.e. we hold.
_PREAUTH_STDERR = (
    "kex_exchange_identification",   # died during key exchange — before auth
    "Exceeded MaxStartups",          # sshd's pre-auth connection cap
    "Connection refused",            # no TCP listener
    "Could not resolve hostname",    # never left this host
    "No route to host",
    "Network is unreachable",
    # Host-key verification precedes authentication in the protocol, so a failure
    # here provably fires no Duo push. Seen live on altair's first fir connect
    # 2026-07-29: a new node had no host key for fir, ssh raised the
    # confirm-the-fingerprint prompt, and the arbiter held the host fleet-wide for
    # a failure that spent nothing.
    "Host key verification failed",
    "REMOTE HOST IDENTIFICATION HAS CHANGED",
)

# The askpass writes its own refusal to the same stderr stream ssh does, and that
# text contains the word "Duo" — so scanning the raw blob for auth-phase markers
# reads a *refusal to answer* as proof the Duo phase was reached. Classification
# looks at ssh's lines only; these are the askpass's.
_ASKPASS_PREFIX = "awm-duo-askpass:"

# Deviation reasons that mean the prompt was never a Duo menu. `ssh` invokes
# SSH_ASKPASS for a host-key confirmation too, so an askpass refusal can be the
# opposite of "Duo was reached". The askpass's other reasons (push-hold, rate cap,
# no matching option) all follow a real Duo menu and keep vetoing.
_NON_DUO_DEVIATIONS = ("not a known Duo menu",)

# Any of these means the auth phase was reached, so an MFA attempt may have been
# spent. Vetoes _PREAUTH_STDERR even if a pre-auth marker also appears (e.g. a
# retry inside one ssh invocation that failed auth, then lost the connection).
_AUTH_PHASE_STDERR = (
    "Permission denied",
    "Too many authentication failures",
    "Authentication failed",
    "locked",
    "MFA",
    "Duo",
)


class ConnState(enum.Enum):
    """The single canonical per-host connection lifecycle.

    Every host moves DISCONNECTED → AUTHENTICATING → CONNECTED → DISPOSING →
    DISCONNECTED and nowhere else. ``connect`` is the sole entry; duplicate/
    concurrent requests are absorbed into the one in-flight attempt rather than
    minting a second ssh/MFA. A breaker "locked" host is DISCONNECTED with a
    lockfile on disk — not a distinct state.
    """

    DISCONNECTED = "disconnected"
    AUTHENTICATING = "authenticating"
    CONNECTED = "connected"
    DISPOSING = "disposing"


@dataclass
class _AttemptRecord:
    """What one attempt actually did, readable by the caller after the attempt is
    gone — including when it was cancelled mid-flight and returned nothing.

    ``spawned`` is the load-bearing field. An attempt that never reached the ssh
    exec provably fired no Duo push, whatever the captured stderr says: that file
    belongs to whichever attempt last got as far as spawning, which may be one
    from weeks ago. On 2026-09-01 a `vpn/up` timeout was judged against a
    "Success. Logging you in..." line captured on 2026-08-25, and the arbiter
    held sockeye fleet-wide for it.

    ``twofa_seen_at_arm`` is the Duo observation count at the moment this attempt
    armed its burst. Compared against the same counter afterwards it answers "did
    Duo see anything at all while we were connecting" — the only positive
    evidence that no MFA attempt was spent. ``None`` means the question could not
    be asked, which is not the same as zero and must never be read as it.
    """

    spawned: bool = False
    twofa_seen_at_arm: int | None = None


@dataclass
class HostState:
    """Per-host state object. Mutated only under ``SSHService._lock`` — a single
    synchronous critical section per transition (no ``await`` between reading the
    state and writing the next one), which is what makes the dedup race-free on
    the cooperative event loop."""

    state: ConnState = ConnState.DISCONNECTED
    attempt: asyncio.Task | None = None   # the one in-flight auth (AUTHENTICATING)
    disposal: asyncio.Task | None = None  # the one in-flight teardown (DISPOSING)
    # A disconnect issued during AUTHENTICATING is not allowed to abort the auth;
    # it sets this flag and is honoured once the attempt resolves.
    pending_disconnect: bool = False


class _AttemptFailed(Exception):
    """Raised inside an attempt when the ControlMaster never came up. Carries the
    base failure reason; the caller enriches it with stderr / askpass deviation."""


class SSHService:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._hosts: dict[str, HostState] = {}
        # device -> monotonic deadline until which an operator approval (a
        # Discord /approve on that device) is considered active. Recovery from a
        # tripped breaker happens ONLY inside such a window; there is no verb.
        self._approve_until: dict[str, float] = {}
        # Slot arbiter (federation): host -> lease_id of the ONE in-flight attempt
        # holding this host's slot fleet-wide. Guards the LEASED state of the
        # SlotArbiter DFA (IDLE/LEASED/LOCKED); LOCKED is the on-disk lockfile
        # (authoritative, central on the arbiter node), IDLE is the absence of
        # both. Populated only on the node that owns the arbiter (or in-process
        # for that node's own gated attempts). See _slot_acquire / _slot_release.
        self._leased: dict[str, str] = {}
        self._arbiter_lock = asyncio.Lock()
        self._social_task: asyncio.Task | None = None
        self._reconcile_task: asyncio.Task | None = None
        # The live /approve subscription, once started. Read by `status` — the
        # only way to see this wire is broken before an emergency needs it.
        self._approve_sub: Any = None
        # nonce -> future, for the inbound self-test (`receive_test`).
        self._probe_waiters: dict[str, asyncio.Future] = {}
        # Held open for the process lifetime once acquired, so the flock stays
        # held (releasing on process death). Stored to keep the fd from being GC'd.
        self._singleton_fd: int | None = None

    def init(self) -> None:
        # Enforce the process singleton BEFORE anything else — if another svc-ssh
        # already holds the lock, stand down cleanly rather than racing the same
        # account. os._exit (not sys.exit) so it's a clean process exit and never
        # an exception into the adapter.
        self._acquire_singleton()
        os.makedirs(LIVE_DIR, exist_ok=True)
        os.makedirs(LOCK_DIR, exist_ok=True)
        # Subscribe to the social service's slash commands so a Discord /approve
        # opens the recovery window (best-effort + self-reconnecting; inert when
        # no social service is present). Mirrors the 2fa service's listener.
        # Started independently: a failure to create either must not take the
        # other down with it. They used to share one try/except.
        try:
            self._social_task = gatewayclient.spawn_supervised(
                "ssh/approve-listener", self._approve_listener)
        except RuntimeError as exc:  # no running loop (shouldn't happen at on_start)
            log.error("ssh: approve listener not started — operator /approve "
                      "will not be heard: %s", exc)
        try:
            # Rebuild per-host state from the world (adopt live masters, respect
            # breaker locks) and reap stale sockets — in the background so startup
            # never blocks on ssh probes.
            loop = asyncio.get_running_loop()
            self._reconcile_task = loop.create_task(self._reconcile_on_boot())
        except RuntimeError as exc:
            log.warning("ssh: boot reconcile not started: %s", exc)
        log.info("ssh service initialised (live_connections: %s)", LIVE_DIR)

    def _acquire_singleton(self) -> None:
        try:
            fd = os.open(SINGLETON_PATH, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            # Can't even open the lock file — fail open rather than crash; the
            # gateway already scopes one service per name in normal operation.
            log.error("ssh: cannot open singleton lock %s: %s", SINGLETON_PATH, exc)
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.error("ssh: another svc-ssh already holds %s — standing down so "
                      "two instances can't race the same account", SINGLETON_PATH)
            os.close(fd)
            os._exit(0)
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
        except OSError:
            pass
        self._singleton_fd = fd

    async def _reconcile_on_boot(self) -> None:
        """Reconstruct per-host state from reality after a (re)start: adopt any
        live ControlMaster, leave breaker-locked hosts alone, then reap stale
        control sockets left by a killed process."""
        try:
            for name, cfg in KNOWN_HOSTS.items():
                if await self._check_master(cfg.host):
                    hs = self._host(name)
                    async with self._lock:
                        if hs.state == ConnState.DISCONNECTED:
                            hs.state = ConnState.CONNECTED
                    # A live master is proof of a real successful auth, so it
                    # supersedes any leftover breaker lock (mirrors the runtime
                    # probe_start path). Without this, a host adopted CONNECTED but
                    # still carrying a stale lockfile would, once that master later
                    # dies and it demotes, read the stale lock and refuse to
                    # reconnect — an unrecoverable-looking hold with no real fault.
                    self._clear_lock(cfg)
                    log.info("ssh: reconciled %s → connected (adopted live master)",
                             name)
            await self._reap_stranded_masters()
            await self._reap_orphans()
        except Exception as exc:  # noqa: BLE001 — reconciliation is best-effort
            log.warning("ssh: boot reconciliation failed: %s", exc)

    async def _reap_stranded_masters(self) -> None:
        """Kill an ssh left running by a PREVIOUS life of this service.

        The in-process reap covers a connect this service is still driving. It
        cannot cover the case that produced the bug: the service itself dies (a
        SIGKILL, a crash, an operator stopping it) while an ssh is mid-auth. That
        process is then reparented to init, holds an armed Duo window, and is
        invisible to every socket-level probe here because it never made a socket
        — so it outlives restart after restart.

        Identity, not resemblance, decides. A pid is the least durable key there
        is, so a recorded pid is killed only when the live process still carries
        the askpass marker THIS service wrote into its environment. Matching on
        the command line instead would match any ssh on the box, including one a
        person is using.
        """
        for cfg in KNOWN_HOSTS.values():
            path = pid_path(cfg)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    pid = int(f.read().strip())
            except (OSError, ValueError):
                self._safe_unlink(path)
                continue
            if self._is_our_master(pid):
                log.warning("ssh: %s left a master running from a previous "
                            "service life — reaping pid %d", cfg.host, pid)
                await self._reap_group(pid, cfg.host)
            self._safe_unlink(path)

    @staticmethod
    def _is_our_master(pid: int) -> bool:
        """Is *pid* still an ssh this service started, for this workspace?

        Reads the process's own environment for the askpass marker we inject,
        and requires it to point inside our LIVE_DIR. Anything we cannot read —
        a dead pid, another user's process — answers False. Never guess toward
        killing.
        """
        try:
            with open(f"/proc/{pid}/environ", "rb") as f:
                environ = f.read()
        except OSError:
            return False
        want = f"AWM_SSH_ASKPASS_MARKER={LIVE_DIR}".encode()
        return any(entry.startswith(want) for entry in environ.split(b"\0"))

    async def _reap_orphans(self) -> None:
        """Remove dead ControlMaster socket files in LIVE_DIR — leftovers from a
        killed process. Conservative: a socket with a *live* master answers the
        probe and is left untouched (adoption above already claimed known ones)."""
        try:
            entries = os.listdir(LIVE_DIR)
        except OSError:
            return
        for fn in entries:
            path = os.path.join(LIVE_DIR, fn)
            try:
                if not stat.S_ISSOCK(os.stat(path).st_mode):
                    continue  # skip stderr / marker / lock regular files
            except OSError:
                continue
            if not await self._check_socket(path):
                # Double-check before reaping: a transient false-negative (a live
                # master briefly not answering `-O check` under load) must not get
                # its socket unlinked, which would force the next connect to re-auth
                # — a fresh, avoidable MFA. Only reap a socket that fails twice.
                await asyncio.sleep(0.5)
                if await self._check_socket(path):
                    continue
                log.info("ssh: reaping stale control socket %s", fn)
                self._safe_unlink(path)

    @staticmethod
    async def _run_check(argv: list[str]) -> bool:
        """Run an ``ssh -O check`` variant, bounded by ``_CHECK_TIMEOUT``. A wedged
        control master can make ``-O check`` hang forever; since reconcile (and thus
        the verbs that now await it) depend on these probes, we cap each one and
        read a timeout as 'not live' (False), killing the stuck probe so it can't
        leak. Never raises."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            log.warning("ssh: could not spawn probe %s: %s", argv, exc)
            return False
        try:
            await asyncio.wait_for(proc.communicate(), timeout=_CHECK_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("ssh: probe %s wedged >%.0fs — treating as not live",
                        argv, _CHECK_TIMEOUT)
            try:
                proc.kill()
                await proc.wait()
            except (ProcessLookupError, Exception):  # noqa: BLE001
                pass
            return False
        return proc.returncode == 0

    @staticmethod
    async def _check_socket(path: str) -> bool:
        """True iff a live master answers on the given control socket path."""
        return await SSHService._run_check(
            ["ssh", "-O", "check", "-S", path, "reap-probe"])

    # -- state access -------------------------------------------------------

    def _host(self, name: str) -> HostState:
        hs = self._hosts.get(name)
        if hs is None:
            hs = HostState()
            self._hosts[name] = hs
        return hs

    # -- public verbs -------------------------------------------------------

    async def _await_reconcile(self) -> None:
        """Block until boot reconciliation (master adoption + orphan reap) has
        finished. A verb must not start an ssh attempt while ``_reap_orphans`` may
        still be unlinking sockets — otherwise an in-flight attempt's half-open
        socket could be reaped out from under it. Awaiting a Task here does not
        cancel it if this verb is itself cancelled, so a slow verb can't abort the
        shared reconcile."""
        task = self._reconcile_task
        if task is None or task.done():
            return
        try:
            # shield: a timeout (or a cancelled verb) must not cancel the shared
            # reconcile task — it keeps running for the next verb.
            await asyncio.wait_for(asyncio.shield(task),
                                   timeout=_RECONCILE_WAIT_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("ssh: boot reconcile still running after %.0fs — proceeding "
                        "(it continues in the background)", _RECONCILE_WAIT_TIMEOUT)
        except Exception:  # noqa: BLE001 — reconcile is best-effort; proceed anyway
            pass

    async def connect(self, host: str) -> dict:
        cfg = resolve_host(host)
        hs = self._host(host)
        await self._await_reconcile()

        while True:
            async with self._lock:
                st = hs.state
                if st == ConnState.AUTHENTICATING:
                    waiter, kind = hs.attempt, "attempt"     # absorb
                elif st == ConnState.DISPOSING:
                    waiter, kind = hs.disposal, "dispose"    # defer
                elif st == ConnState.CONNECTED:
                    kind = "probe_connected"                 # verify liveness
                else:  # DISCONNECTED
                    # Circuit breaker FIRST — a held host does zero ssh/VPN/2FA
                    # work (not even an auth-free probe), so it can never march
                    # toward the provider's lockout ceiling. The hold lifts only
                    # while an operator approval window is open.
                    if self._read_lock(cfg) is not None:
                        if not self._approve_active(cfg.twofa_device):
                            # A hold recorded against our OWN approver may lift
                            # itself, but only on a fresh positive health
                            # assertion, and only here — on a request somebody
                            # actually made. Nothing polls for this, so no
                            # attempt is ever started that nobody asked for.
                            # _maybe_self_clear removes the lockfile itself.
                            if not await self._maybe_self_clear(cfg):
                                return self._status_dict(
                                    cfg, "unavailable",
                                    error=f"{cfg.host} is not available for "
                                          f"automated access right now")
                        else:
                            log.info(
                                "operator approval window open for %s (device %s) "
                                "— clearing hold and reconnecting (one-shot)",
                                cfg.host, cfg.twofa_device)
                        self._clear_lock(cfg)
                        # ONE-SHOT: consume the window as we spend it. Each operator
                        # /approve authorises exactly one reconnect attempt. Without
                        # this the window stays open for its full duration, so a
                        # caller that retries a failing connect re-trips then
                        # re-clears the breaker every iteration — an unbounded run of
                        # Duo pushes straight to the provider lockout the breaker
                        # exists to prevent. Consuming here also bounds the
                        # device-keyed window: only the first host on a shared device
                        # recovers per /approve, not every host sharing it.
                        self._approve_until.pop(cfg.twofa_device, None)
                    kind = "probe_start"

            if kind == "attempt":
                return await self._await_result(waiter, cfg)
            if kind == "dispose":
                # Let the teardown finish, then re-evaluate (→ DISCONNECTED →
                # a fresh attempt).
                await self._await_quietly(waiter)
                continue
            if kind == "probe_connected":
                # AUTH-FREE probe (`ssh -O check` never mints a login). Live →
                # done; dead → the master died out-of-band, demote and re-auth.
                if await self._check_master(cfg.host):
                    return self._status_dict(cfg, "connected")
                async with self._lock:
                    if hs.state == ConnState.CONNECTED:
                        hs.state = ConnState.DISCONNECTED
                continue

            # kind == "probe_start": past the breaker gate. Probe (auth-free) for
            # an already-live master — ours, adopted at boot, or made out-of-band
            # — so we never spawn `ssh -M` against an existing socket (which would
            # drop to a non-multiplexed login and fire MFA).
            if await self._check_master(cfg.host):
                async with self._lock:
                    if hs.state in (ConnState.DISCONNECTED, ConnState.CONNECTED):
                        hs.state = ConnState.CONNECTED
                        self._clear_lock(cfg)  # a live master supersedes a stale lock
                        return self._status_dict(cfg, "connected")
                continue
            # No master, not locked: CAS to AUTHENTICATING and start the one
            # attempt. Re-validate under the lock so a connect that raced us into
            # AUTHENTICATING is absorbed rather than duplicated.
            async with self._lock:
                if hs.state != ConnState.DISCONNECTED:
                    continue
                hs.state = ConnState.AUTHENTICATING
                hs.pending_disconnect = False
                hs.attempt = asyncio.create_task(self._run_attempt(cfg, hs))
                waiter = hs.attempt
            return await self._await_result(waiter, cfg)

    async def disconnect(self, host: str) -> dict:
        cfg = resolve_host(host)
        hs = self._host(host)
        await self._await_reconcile()

        while True:
            async with self._lock:
                st = hs.state
                if st == ConnState.DISCONNECTED:
                    return self._status_dict(cfg, "disconnected")
                if st == ConnState.AUTHENTICATING:
                    # Hold — do NOT abort the in-flight auth. Mark intent and let
                    # it resolve, keeping the single canonical path.
                    hs.pending_disconnect = True
                    waiter, kind = hs.attempt, "attempt"
                elif st == ConnState.CONNECTED:
                    hs.state = ConnState.DISPOSING
                    hs.disposal = asyncio.create_task(self._run_dispose(cfg, hs))
                    waiter, kind = hs.disposal, "dispose"
                else:  # DISPOSING
                    waiter, kind = hs.disposal, "dispose"    # absorb

            if kind == "attempt":
                # Wait for auth to resolve, then loop: a successful connect will
                # have queued disposal (→ DISPOSING), a failed one is already
                # DISCONNECTED. Either way the next pass finishes the disconnect.
                await self._await_quietly(waiter)
                continue
            cleared = await self._await_bool(waiter)
            if cleared:
                return self._status_dict(cfg, "disconnected")
            return self._status_dict(cfg, "disconnected",
                                     warning="master process may still be running")

    async def status(self) -> dict:
        connections: dict[str, dict] = {}
        for name, cfg in KNOWN_HOSTS.items():
            hs = self._host(name)
            st = hs.state
            if st == ConnState.CONNECTED:
                connections[name] = self._status_dict(cfg, "connected")
            elif st == ConnState.AUTHENTICATING:
                connections[name] = self._status_dict(cfg, "connecting")
            elif st == ConnState.DISPOSING:
                connections[name] = self._status_dict(cfg, "disconnecting")
            elif self._read_lock(cfg) is not None:
                # Held by the breaker. Reported neutrally — no mechanism detail.
                connections[name] = self._status_dict(cfg, "unavailable")
            else:
                connections[name] = self._status_dict(cfg, "disconnected")
        return {
            "connections": connections,
            "subscription": self._subscription_health(),
            "approval_windows": self._approval_windows(),
        }

    def _subscription_health(self) -> dict:
        """Health of the ``/approve`` wire — the only thing that lifts a hold.

        Before this, a deaf listener was indistinguishable from an idle one and
        was only discovered mid-emergency. Never raises: health has to be
        reportable precisely when things are broken.
        """
        sub = self._approve_sub
        if sub is None:
            return {"healthy": False, "connected": False,
                    "last_error": "approve listener not started"}
        try:
            return sub.health()
        except Exception as exc:  # noqa: BLE001
            return {"healthy": False, "last_error": f"unavailable: {exc}"}

    def _approval_windows(self) -> dict[str, int]:
        """Open operator windows, as remaining seconds. Read-only — there is no
        verb that opens or extends one, and this must not become it."""
        try:
            now = time.monotonic()
            return {d: int(until - now)
                    for d, until in self._approve_until.items() if until > now}
        except Exception:  # noqa: BLE001
            return {}

    # -- attempt / disposal drivers -----------------------------------------

    async def _run_attempt(self, cfg: HostConfig, hs: HostState) -> dict:
        """The single auth attempt. Always resolves via an internal outcome — a
        success or a breaker trip — never by outer cancellation, so the breaker
        can never be bypassed. Transitions the host on completion."""
        try:
            result = await self._do_connect(cfg)
        except Exception as e:  # noqa: BLE001
            # _do_connect is contracted never to raise, but guard anyway: an
            # unexpected raise (e.g. OSError writing the lockfile under disk/fd
            # exhaustion, which is outside _do_connect's own try) must NEVER leave
            # the host stranded in AUTHENTICATING — every later connect would absorb
            # onto this dead attempt and return error forever, with the breaker
            # never tripped. Force a terminal state and best-effort hold the host.
            log.error("connect to %s raised unexpectedly: %s", cfg.host, e)
            try:
                await self._trip_breaker(cfg, f"unexpected error: {e}")
            except Exception as e2:  # noqa: BLE001 — breaker is best-effort here
                log.error("could not trip breaker for %s after error: %s",
                          cfg.host, e2)
            result = self._status_dict(cfg, "error",
                                       error=f"connect to {cfg.host} failed")
        connected = result.get("status") == "connected"
        async with self._lock:
            hs.attempt = None
            want_disconnect = hs.pending_disconnect
            hs.pending_disconnect = False
            if connected and want_disconnect:
                # A disconnect was queued during auth: finish the canonical path
                # (we DID connect) and immediately dispose.
                hs.state = ConnState.DISPOSING
                hs.disposal = asyncio.create_task(self._run_dispose(cfg, hs))
            elif connected:
                hs.state = ConnState.CONNECTED
            else:
                # Failure already tripped the breaker inside _do_connect.
                hs.state = ConnState.DISCONNECTED
        return result

    async def _run_dispose(self, cfg: HostConfig, hs: HostState) -> bool:
        cleared = False
        try:
            cleared = await self._exit_master(cfg.host)
        finally:
            async with self._lock:
                hs.disposal = None
                hs.state = ConnState.DISCONNECTED
        return cleared

    async def _await_result(self, waiter: asyncio.Task, cfg: HostConfig) -> dict:
        try:
            return await waiter
        except Exception as e:  # noqa: BLE001 — an attempt should never raise out
            log.error("attempt for %s failed unexpectedly: %s", cfg.host, e)
            return self._status_dict(cfg, "error",
                                     error=f"connect to {cfg.host} failed")

    @staticmethod
    async def _await_quietly(waiter: asyncio.Task | None) -> None:
        if waiter is None:
            return
        try:
            await waiter
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    async def _await_bool(waiter: asyncio.Task | None) -> bool:
        if waiter is None:
            return True
        try:
            return bool(await waiter)
        except Exception:  # noqa: BLE001
            return False

    # -- the actual ssh work ------------------------------------------------

    async def _do_connect(self, cfg: HostConfig) -> dict:
        """Route the single attempt. A lockout-sensitive host (one with a 2FA
        device) goes through the fleet-global slot arbiter — a live-WS lease to
        ``ssh@<AWM_SSH_SLOT_PEER>``, or the in-process arbiter when this node owns
        the slot (selector unset). Every other host keeps the per-node
        local-breaker path unchanged. Never raises."""
        if cfg.twofa_device:
            return await self._connect_through_arbiter(cfg)
        return await self._do_connect_attempt(cfg, gated=False)

    # -- slot arbiter: fleet-global single-attempt gate ---------------------

    async def _connect_through_arbiter(self, cfg: HostConfig) -> dict:
        """Acquire the host's fleet-wide slot, run the one attempt while holding
        it, then report the verdict. Local when we own the arbiter, else a live-WS
        lease to the arbiter peer. Fails CLOSED — an unreachable arbiter refuses
        the connect (no VPN/2FA/ssh, no MFA spent)."""
        slot_peer = gatewayclient.peer_env(_SLOT_PEER_ENV)
        if not slot_peer:
            return await self._connect_via_local_arbiter(cfg)
        return await self._connect_via_arbiter_peer(cfg, slot_peer)

    async def _connect_via_local_arbiter(self, cfg: HostConfig) -> dict:
        """This node owns the arbiter: acquire in-process, hold across the attempt
        (the attempt task's liveness IS the hold), release with the verdict."""
        status, token = await self._slot_acquire(cfg.host)
        if status != "granted":
            return self._status_dict(
                cfg, "unavailable",
                error=f"{cfg.host} is not available for automated access right now")
        ok = False
        reason = ""
        try:
            result = await self._do_connect_attempt(cfg, gated=True)
            ok = self._verdict_ok(result)
            reason = result.pop("_lock_reason", "")
            return result
        finally:
            # We own the arbiter AND made the attempt, so we are the requester.
            await self._slot_release(cfg.host, token, ok=ok, reason=reason,
                                     requester=_NODE_NAME)

    @staticmethod
    def _verdict_ok(result: dict) -> bool:
        """The lease verdict answers "was the lockout budget spent?", NOT "did the
        connect succeed" — the slot exists solely to bound MFA attempts fleet-wide.

        So a connect that died before the auth phase reports ``ok``: it spent
        nothing, the account is not at risk, and the slot should simply be freed
        rather than held (which would cost the operator an /approve for a failure
        that was never their account's fault). Popping ``_preauth`` here keeps it
        off the dict the agent sees.
        """
        preauth = result.pop("_preauth", False)
        return result.get("status") == "connected" or bool(preauth)

    async def _connect_via_arbiter_peer(self, cfg: HostConfig,
                                        slot_peer: str) -> dict:
        """Acquire a slot from the arbiter peer over a CA-verified, peer-bearer
        WS lease; the open socket IS the lease. Hold it across the attempt, then
        send the verdict (a clean release) — a drop mid-attempt trips the arbiter
        LOCKED on its own."""
        try:
            lease = await gatewayclient.acquire_lease_maybe_peer(
                slot_peer, "ssh", cfg.host, node=_NODE_NAME)
        except Exception as e:  # noqa: BLE001 — arbiter unreachable → FAIL CLOSED
            log.error("connection arbiter %s unreachable for %s: %s",
                      slot_peer, cfg.host, e)
            return self._status_dict(
                cfg, "unavailable",
                error=f"{cfg.host} is not available for automated access right now")
        async with lease:
            if not lease.granted:
                log.info("slot arbiter %s refused %s: %s (%s)",
                         slot_peer, cfg.host, lease.status, lease.reason)
                return self._status_dict(
                    cfg, "unavailable",
                    error=f"{cfg.host} is not available for automated access right now")
            result = await self._do_connect_attempt(cfg, gated=True)
            ok = self._verdict_ok(result)
            reason = result.pop("_lock_reason", "")
            await lease.verdict(ok=ok, reason=reason)
            return result

    async def _slot_acquire(self, host: str) -> tuple[str, str | None]:
        """Arbiter ``open`` transition. Atomically grant at most one in-flight
        lease per host: returns ``("granted", lease_id)`` from IDLE,
        ``("busy", None)`` from LEASED, ``("locked", reason)`` from LOCKED — unless
        an operator approval window is open, which clears the hold and grants
        (mirroring the one-shot window consumption in :meth:`connect`)."""
        cfg = resolve_host(host)
        async with self._arbiter_lock:
            if host in self._leased:
                return ("busy", None)
            if self._read_lock(cfg) is not None:
                if not self._approve_active(cfg.twofa_device):
                    # Same lazy self-clear as connect(): a hold attributable to
                    # our own approver lifts on the approver's fresh positive
                    # assertion, on a request — never on a timer.
                    if not await self._maybe_self_clear(cfg):
                        return ("locked", "held after a prior failed connect")
                else:
                    # Approval window open → clear + consume one-shot (see connect()).
                    self._clear_lock(cfg)
                    self._approve_until.pop(cfg.twofa_device, None)
            token = secrets.token_urlsafe(8)
            self._leased[host] = token
            return ("granted", token)

    async def _slot_release(self, host: str, lease_id: str | None, *,
                            ok: bool, reason: str = "",
                            requester: str | None = None) -> None:
        """Arbiter ``verdict_ok`` / ``verdict_fail`` / ``drop`` transition.
        ``ok`` frees the slot (→ IDLE, clearing any lock); otherwise → LOCKED
        (persist the lockfile) and page the operator — the arbiter is the SOLE
        notifier on a LOCKED transition, so the requester's attempt stays silent.
        Idempotent by ``lease_id``: a duplicate or stale release is a no-op.

        ``requester`` is the node whose attempt failed, reported in the lease's
        init/verdict frame. It is NOT this node: the arbiter pages on behalf of
        someone else, so naming ourselves in that alert would name the node that
        did not make the attempt."""
        cfg = resolve_host(host)
        do_alert = False
        reason = reason or "attempt failed (slot arbiter)"
        # Classified outside the lock: it may make a gateway call, and the
        # arbiter lock guards the in-flight slot, not the network.
        cause = await self._classify_cause(cfg, reason)
        async with self._arbiter_lock:
            if lease_id is not None and self._leased.get(host) != lease_id:
                return  # superseded / already released — ignore
            self._leased.pop(host, None)
            if ok:
                self._clear_lock(cfg)
                return
            self._write_lock(cfg, reason, cause=cause)
            do_alert = True
        if do_alert:
            log.error("BREAKER TRIPPED (arbiter) — holding %s for %s: %s",
                      host, requester or "an unidentified node", reason)
            await self._alert(
                self._lock_alert_text(cfg, reason, requester=requester))

    async def _lease_session(self, ctx: Any) -> None:
        """Direct-session handler (the arbiter side of a WS lease). The OPEN bridge
        socket IS the lease: grant or refuse on the first frame, then hold until
        the requester reports a ``verdict`` or the socket drops. Drop without a
        verdict is treated as a failed attempt (LOCKED) — the ZooKeeper-ephemeral
        semantics that make a dead holder free (or trip) its slot at once."""
        init = ctx.init or {}
        host = init.get("host", "")
        # Who is asking. Carried in BOTH frames on purpose: the init frame covers
        # the drop-without-verdict case, where there is no verdict to read it from.
        requester = str(init.get("node") or "") or None
        bridge = await ctx.open_bridge()
        token: str | None = None
        try:
            try:
                resolve_host(host)
            except ValueError:
                await bridge.send(json.dumps(
                    {"lease": "error", "reason": f"unknown host {host!r}"}))
                return
            status, detail = await self._slot_acquire(host)
            frame = ({"lease": "granted"} if status == "granted"
                     else {"lease": status, "reason": detail or status})
            await bridge.send(json.dumps(frame))
            if status != "granted":
                return
            token = detail
            verdict: str | None = None
            vreason = ""
            try:
                async for raw in bridge:
                    if isinstance(raw, (bytes, bytearray)):
                        continue
                    try:
                        msg = json.loads(raw) or {}
                    except json.JSONDecodeError:
                        continue
                    v = msg.get("verdict")
                    if v in ("ok", "fail"):
                        verdict = v
                        vreason = msg.get("reason") or ""
                        requester = str(msg.get("node") or "") or requester
                        break
            except Exception:  # noqa: BLE001 — socket dropped mid-hold
                pass
            if verdict == "ok":
                await self._slot_release(host, token, ok=True,
                                         requester=requester)
            else:
                reason = (vreason or "requester reported connect failure"
                          if verdict == "fail"
                          else "requester dropped the lease without a verdict")
                await self._slot_release(host, token, ok=False, reason=reason,
                                         requester=requester)
            token = None
        finally:
            if token is not None:
                # Aborted after grant without a clean release — trip closed (safe).
                await self._slot_release(host, token, ok=False,
                                         reason="lease handler aborted",
                                         requester=requester)
            try:
                await bridge.close()
            except Exception:  # noqa: BLE001
                pass

    async def _do_connect_attempt(self, cfg: HostConfig, *,
                                  gated: bool = False) -> dict:
        """Bring up the ControlMaster (vpn + 2fa + ssh + poll), bounded by an
        INTERNAL timeout. Any failure — including the timeout — trips the breaker
        and returns an error dict; success returns a connected dict. Never raises.

        When ``gated`` the slot arbiter owns the breaker: a failure returns an
        error dict WITHOUT tripping the local breaker or alerting, stashing the
        reason under ``_lock_reason`` so the caller can pass it up as the lease
        verdict — the arbiter records LOCKED and pages the operator exactly once.

        A failure that died BEFORE the auth phase spent no MFA attempt, so it does
        not trip the breaker at all (and sets ``_preauth`` for the gated caller to
        release the slot without a hold). See :meth:`_is_preauth_failure`."""
        marker = self._deviation_marker(cfg)
        self._safe_unlink(marker)
        # Truncate the stderr capture HERE, not at the spawn. It was only ever
        # truncated when ssh actually started, so a failure that never got that
        # far was judged against whatever the last spawning attempt left behind
        # — on 2026-09-01 a vpn timeout on sockeye was recorded, and alerted on,
        # quoting a successful login from six days earlier.
        self._truncate_stderr(cfg)
        rec = _AttemptRecord()
        try:
            await asyncio.wait_for(self._attempt_master(cfg, marker, rec),
                                   timeout=_CONNECT_TIMEOUT)
            log.info("connected to %s", cfg.host)
            return self._status_dict(cfg, "connected")
        except _AttemptFailed as e:
            # The poll window (_CHECK_POLL_ATTEMPTS * _CHECK_POLL_INTERVAL) is
            # shorter than the wait_for cap, so a master whose auth completed late
            # can appear just after we gave up polling — and this, not TimeoutError,
            # is the common give-up path. Re-check once: if it's now up, ADOPT it as
            # success. That auth already succeeded (an MFA attempt was spent and
            # approved), so tripping the breaker on it would waste a live connection
            # AND spuriously page the operator, while leaving a master orphaned
            # behind a lock. Only if there is genuinely no master do we fail through.
            if await self._check_master(cfg.host):
                self._clear_lock(cfg)
                log.info("connected to %s (master appeared just after poll window)",
                         cfg.host)
                return self._status_dict(cfg, "connected")
            reason = self._failure_reason(cfg, marker, str(e))
        except asyncio.TimeoutError:
            # The ssh we spawned is already dead: cancelling _attempt_master
            # reaps its process group on the way out. This clears a half-open
            # master that DID reach the socket stage.
            await self._exit_master(cfg.host)
            reason = self._failure_reason(
                cfg, marker, f"connect exceeded {_CONNECT_TIMEOUT:.0f}s")
        except Exception as e:  # noqa: BLE001
            log.error("connect to %s failed: %s", cfg.host, e)
            reason = self._failure_reason(cfg, marker, str(e))
        preauth = (self._is_preauth_failure(cfg, marker, rec)
                   or await self._duo_saw_nothing(cfg, rec))
        if preauth:
            # Nothing was spent, so there is nothing to protect: no hold, no page.
            # Tell the caller it is safe to retry — unlike a held host, this needs
            # no /approve, and the host is simply refusing connections right now.
            log.warning("pre-auth failure on %s (no MFA attempt spent, breaker "
                        "NOT tripped): %s", cfg.host, reason)
            result = self._status_dict(
                cfg, "error",
                error=(f"{cfg.host} refused the connection before authentication "
                       f"— no MFA attempt was spent, safe to retry later"))
        else:
            # Neutral to the caller — the detailed reason goes to the lock + the
            # operator Discord alert, not to the agent.
            result = self._status_dict(
                cfg, "error", error=f"connect to {cfg.host} failed")
        if not gated:
            if not preauth:
                await self._trip_breaker(cfg, reason)
        else:
            # The slot arbiter owns LOCKED + the alert (via the lease verdict);
            # surface the reason so the caller can hand it up.
            result["_lock_reason"] = reason
            result["_preauth"] = preauth
        return result

    async def _attempt_master(self, cfg: HostConfig, marker: str,
                              rec: _AttemptRecord) -> None:
        """Orchestrate vpn + 2fa + ssh and poll for the ControlMaster socket.
        Returns on success (lock cleared); raises :class:`_AttemptFailed` if the
        master never appears.

        ``rec`` is the caller's object, filled in as we go, because this
        coroutine can be cancelled and then returns nothing at all — and what it
        got as far as doing is exactly what the caller needs to judge the
        failure."""
        log.info("connecting to %s (vpn=%s, 2fa=%s)",
                 cfg.host, cfg.vpn_profile or "none",
                 cfg.twofa_device or "none")

        if cfg.needs_vpn and cfg.vpn_profile:
            vpn_result = await gatewayclient.call(
                "vpn", "up", {"profile": cfg.vpn_profile})
            log.info("vpn up %s: %s", cfg.vpn_profile,
                     vpn_result.get("status", "ok"))

        if cfg.twofa_device:
            # Always arm +1: this connect is about to fire exactly ONE Duo push, so
            # it must contribute exactly one approval to the device's budget. The
            # 2fa start_burst verb accumulates overlapping arms (grant totals N) and
            # reuses the single poll task, so arming unconditionally is correct and
            # never double-spawns. Do NOT gate on an existing "burst_active": a
            # second concurrent connect to a host sharing this device (e.g.
            # sockeye/sockeye1 both on cwl) would then skip its grant, leaving budget
            # at 1 for 2 pushes — the 2nd push is held, times out, and trips its
            # breaker. That was the original overlapping-login failure.
            armed = await gatewayclient.call_maybe_peer(
                gatewayclient.peer_env(_TWOFA_PEER_ENV),
                "2fa", "burst", {
                    "device": cfg.twofa_device,
                    "window": 120,
                    "count": 1,
                })
            # Duo's own observation count as of arming. If it has not moved by
            # the time this attempt fails, Duo never heard from the login and no
            # MFA attempt was spent. A 2fa too old to report it leaves this None,
            # which the caller reads as "no evidence" rather than as zero.
            seen = (armed or {}).get("transactions_seen")
            rec.twofa_seen_at_arm = int(seen) if isinstance(seen, int) else None
            log.info("2fa burst armed (+1) for %s on device %s%s",
                     cfg.host, cfg.twofa_device,
                     f" via 2fa@{gatewayclient.peer_env(_TWOFA_PEER_ENV)}"
                     if gatewayclient.peer_env(_TWOFA_PEER_ENV) else "")

        env = os.environ.copy()
        env.update({
            "SSH_ASKPASS": SSH_ASKPASS,
            "SSH_ASKPASS_REQUIRE": "force",
            "AWM_DUO_DEVICES": "awm|Mira",
            # The askpass drops this marker (and refuses) on any prompt it
            # can't exactly character-match to one of our devices, so the
            # failure path below can report "askpass deviation" as the cause.
            "AWM_SSH_ASKPASS_MARKER": marker,
        })

        # Cap keyboard-interactive (Duo) to a SINGLE attempt. OpenSSH's default
        # `NumberOfPasswordPrompts` is 3, so one `ssh` invocation will retry the
        # kbd-interactive/Duo exchange up to THREE times before giving up — and
        # each retry is a fresh Duo push. That silently turns one connect into up
        # to three failed MFA attempts on a failing-auth path, so the one-strike
        # breaker (which counts *connects*) under-counts *pushes* 3:1 and a
        # handful of connects can still march the provider to its 10-strike
        # lockout. Forcing =1 makes the invariant exact: one connect ⇒ at most one
        # Duo push. (Confirmed in the wild: fir.connect.stderr showed three
        # back-to-back kbd-interactive attempts inside a single ssh. #0317299.)
        # Harmless for pubkey-only hosts, which never enter kbd-interactive.
        argv = ["ssh", "-f", "-N", "-M", "-o", "NumberOfPasswordPrompts=1"]
        # A guarded host carries a ProxyCommand guard in ~/.ssh/config that
        # blocks bare `ssh <host>` when no master exists. The service is the
        # sole allowed master-creator, so it overrides the guard here.
        # (Command-line `-o` is first-match-wins over config.) Do NOT do this
        # for VPN-bounced hosts — their ProxyCommand is the required tunnel.
        if cfg.guarded:
            argv += ["-o", "ProxyCommand=none"]
        argv.append(cfg.host)

        # Capture ssh stderr to a file (NOT a PIPE — a PIPE makes the forked
        # `-f -N -M` child hang, per the README). This is what makes a failed
        # connect visible instead of silently swallowed.
        errfile = stderr_path(cfg)
        with open(errfile, "wb") as ef:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=ef,
                # Give the child its own process group so the reap below can
                # signal the GROUP. Without this the child shares the service's
                # own group and a group signal kills the service. The group is
                # what covers the askpass helper ssh forks under itself.
                start_new_session=True,
            )
            rec.spawned = True
            self._write_pid(cfg, proc.pid)
            try:
                await proc.wait()
            except BaseException:
                # Any exit other than ssh finishing on its own — the 120s
                # wait_for cancelling us, a service shutdown, a stray cancel.
                # Before this the process was simply abandoned: the handle was a
                # local in this frame, the frame was discarded, and the only
                # cleanup on the timeout path (`ssh -O exit`) speaks through a
                # ControlMaster socket a pre-auth-hung ssh has never created. So
                # it survived, holding an armed Duo window, until something else
                # killed the whole service.
                await self._reap_group(proc.pid, cfg.host)
                raise
            finally:
                self._safe_unlink(pid_path(cfg))

        for _ in range(_CHECK_POLL_ATTEMPTS):
            if await self._check_master(cfg.host):
                self._clear_lock(cfg)
                return
            await asyncio.sleep(_CHECK_POLL_INTERVAL)

        raise _AttemptFailed(
            f"ControlMaster did not appear within "
            f"{_CHECK_POLL_ATTEMPTS * _CHECK_POLL_INTERVAL:.0f}s")

    async def _exit_master(self, host: str) -> bool:
        """Tear down a host's ControlMaster (`ssh -O exit`) and confirm it's gone.
        Returns True if the socket is gone, False if it may still be running or the
        teardown exceeded ``_EXIT_TIMEOUT`` (bounded so a wedged socket can't hang
        an in-progress connect/disconnect indefinitely)."""
        try:
            return await asyncio.wait_for(self._exit_master_inner(host),
                                          timeout=_EXIT_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("ssh: teardown of %s exceeded %.0fs — leaving it be",
                        host, _EXIT_TIMEOUT)
            return False

    async def _exit_master_inner(self, host: str) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-O", "exit", host,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        for _ in range(_DISCONNECT_POLL_ATTEMPTS):
            if not await self._check_master(host):
                return True
            await asyncio.sleep(0.5)
        return False

    @staticmethod
    async def _check_master(host: str) -> bool:
        return await SSHService._run_check(["ssh", "-O", "check", host])

    # -- reaping the ssh we spawned -----------------------------------------
    #
    # `ssh -O exit` and `-O check` both speak through the ControlMaster socket,
    # so neither can see an ssh that hung before it ever made one. These two are
    # the pid-level handle on exactly that process, and nothing else in this file
    # signals anything a user did not ask it to.

    def _write_pid(self, cfg: HostConfig, pid: int) -> None:
        """Record the spawned ssh's pid so a LATER service life can still reap it.

        Best-effort: failing to write it must not fail the connect, since the
        in-process reap does not depend on the file.
        """
        try:
            with open(pid_path(cfg), "w", encoding="utf-8") as f:
                f.write(f"{pid}\n")
        except OSError as exc:
            log.warning("ssh: could not record master pid for %s: %s",
                        cfg.host, exc)

    async def _reap_group(self, pid: int, host: str) -> None:
        """SIGTERM the process group led by *pid*, then SIGKILL what survives.

        Signals the GROUP, not the pid: ssh forks the askpass helper beneath
        itself, and killing only ssh strands it. This is safe solely because the
        spawn passes ``start_new_session=True`` — without it the group is the
        service's own and this would be suicide.
        """
        if not self._signal_group(pid, signal.SIGTERM, host):
            return
        log.warning("ssh: reaping stranded master pid %d for %s", pid, host)
        try:
            await asyncio.sleep(_REAP_GRACE_S)
        except asyncio.CancelledError:
            # We are usually already unwinding a cancellation, and a second one
            # must not cost us the SIGKILL. Fall through and send it now.
            pass
        self._signal_group(pid, signal.SIGKILL, host)

    @staticmethod
    def _signal_group(pid: int, sig: signal.Signals, host: str) -> bool:
        """Signal the group led by *pid*. False when there was nothing to signal."""
        try:
            os.killpg(pid, sig)
            return True
        except ProcessLookupError:
            return False        # already gone — the normal case after SIGTERM
        except OSError as exc:
            log.warning("ssh: could not send %s to master %d for %s: %s",
                        sig.name, pid, host, exc)
            return False

    # -- operator approval window (Discord /approve) ------------------------

    async def _approve_listener(self) -> None:
        """Open a recovery window when the operator runs a Discord /approve.

        Subscribes to the social service's ``command`` emit (the same stream the
        2fa service arms bursts from) — local, or a peer's social@<peer> stream
        when AWM_SOCIAL_PEER is set. Reconnect, backoff, and the idle-deadline
        re-subscribe belong to :class:`SupervisedSubscription`; inert when no
        social service is present. This is the ONLY thing that lifts a breaker
        hold — there is no verb, so an agent cannot clear its own lock, which
        is exactly why the wire has to heal itself and report its own health.
        """
        def _stream():
            # peer selector read per connection, so a re-home takes effect on
            # the next reconnect without a restart.
            return gatewayclient.subscribe_maybe_peer(
                gatewayclient.peer_env(_SOCIAL_PEER_ENV), "social", "command")

        self._approve_sub = gatewayclient.SupervisedSubscription(
            "ssh/social.command", _stream, self._on_approve_event,
            intercept=self._intercept_probe)
        await self._approve_sub.run()

    async def _on_approve_event(self, ev: object) -> None:
        """One social ``command`` event: open the window, then acknowledge.

        Split deliberately. ``_handle_approve`` is synchronous and decides the
        window; the acknowledgement is a separate awaited step afterwards, so
        an ack that fails (Discord down, peer unreachable) can never affect
        whether the window opened.
        """
        verdict = self._handle_approve(ev)
        if verdict is not None:
            await self._ack_approve(ev, verdict)

    def _handle_approve(self, ev: object) -> str | None:
        """Decide the recovery window for one ``/approve``. Pure and sync.

        Returns the text to acknowledge with, or ``None`` when this event is
        not ours to answer (not an ``approve``, or no device named — another
        consumer's business). Sending the acknowledgement is the caller's job;
        keeping it out of here is what guarantees a Discord failure cannot
        change whether the window opened.
        """
        if not isinstance(ev, dict) or ev.get("command") != "approve":
            return None
        device = str(ev.get("device") or "").strip()
        if not device:
            return None
        # Only accept a device some managed host actually uses. An unknown string
        # (a typo, or another service's device) can never authorise an ssh recovery
        # anyway, and storing it would leak into _approve_until forever. Mirrors the
        # 2fa service, which validates the device before arming a burst.
        known = {c.twofa_device for c in KNOWN_HOSTS.values() if c.twofa_device}
        if device not in known:
            log.warning("ssh: /approve for unknown device %r — ignoring "
                        "(known: %s)", device, ", ".join(sorted(known)))
            return (f"⚠️ ssh: `{device}` is not a device any managed host uses "
                    f"(known: {', '.join(sorted(known))})")
        now = time.monotonic()
        # Opportunistically drop expired windows so the dict can't grow unbounded
        # over a long-lived process.
        for d in [d for d, until in self._approve_until.items() if until <= now]:
            self._approve_until.pop(d, None)
        self._approve_until[device] = now + _APPROVE_WINDOW_SECONDS
        log.info("ssh: operator /approve → recovery window open for device %r "
                 "(%.0fs)", device, _APPROVE_WINDOW_SECONDS)
        mins = int(_APPROVE_WINDOW_SECONDS // 60) or 1
        return (f"🔓 ssh [{_NODE_NAME}]: breaker recovery window open for "
                f"`{device}` — {mins} min")

    async def _ack_approve(self, ev: object, text: str) -> None:
        """Receipt back to the originating DM. Best-effort, never load-bearing.

        Why this exists: on 2026-07-26 the operator saw Discord's own ack and
        2fa's "Duo approvals armed" and reasonably concluded the chain was
        live, while the one consumer that mattered — this one — was deaf.
        With a receipt, **silence from ssh is itself the symptom**, visible at
        the moment the operator acts rather than during the emergency.

        Worded as a statement of fact, never an instruction: nothing here
        should train a reflex to re-run ``/approve``, which spends multi-factor
        budget.
        """
        account = (ev or {}).get("account") if isinstance(ev, dict) else None
        channel = (ev or {}).get("channel_id") if isinstance(ev, dict) else None
        if not account or not channel:
            return
        try:
            await gatewayclient.call_maybe_peer(
                gatewayclient.peer_env(_SOCIAL_PEER_ENV),
                "social", "send",
                {"account": account, "channel": str(channel), "text": text})
        except Exception as exc:  # noqa: BLE001 — a receipt is not the window
            log.warning("ssh: /approve acknowledgement failed (the window is "
                        "open regardless): %s", exc)

    def _approve_active(self, device: str) -> bool:
        if not device:
            return False
        return self._approve_until.get(device, 0.0) > time.monotonic()

    # -- circuit breaker ----------------------------------------------------

    @staticmethod
    def _deviation_marker(cfg: HostConfig) -> str:
        return os.path.join(LIVE_DIR, f"{cfg.host}.askpass_deviation")

    @staticmethod
    def _truncate_stderr(cfg: HostConfig) -> None:
        """Empty a host's ssh stderr capture at the start of an attempt.

        Every reader of that file treats it as evidence about the attempt being
        judged. It must therefore be emptied when the attempt begins, not when
        ssh happens to start, or a failure that never spawned inherits an older
        attempt's verdict.
        """
        try:
            with open(stderr_path(cfg), "wb"):
                pass
        except OSError as exc:
            log.warning("ssh: could not clear stderr capture for %s: %s",
                        cfg.host, exc)

    @staticmethod
    def _safe_unlink(path: str) -> None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("could not unlink %s: %s", path, e)

    @staticmethod
    def _read_lock(cfg: HostConfig) -> dict | None:
        try:
            with open(lock_path(cfg), "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            # A malformed/unreadable lock still means "locked" — fail safe.
            return {"reason": "lockfile present but unreadable"}

    def _write_lock(self, cfg: HostConfig, reason: str, *,
                    cause: str = _CAUSE_EXTERNAL) -> None:
        os.makedirs(LOCK_DIR, exist_ok=True)
        payload = {
            "host": cfg.host,
            "reason": reason,
            # Which side the failure is attributable to. Only _CAUSE_SELF is
            # ever eligible for a self-clear, and only on a fresh positive
            # health assertion — see _self_clear_ok.
            "cause": cause,
            "ts": time.time(),
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        # Temp-and-rename: a torn read already degrades to "locked for an
        # unreadable reason", and would now also lose the cause bucket —
        # which fails safe, but silently.
        path = lock_path(cfg)
        tmp = f"{path}.tmp.{os.getpid()}"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, path)
        except OSError:
            self._safe_unlink(tmp)
            raise

    def _clear_lock(self, cfg: HostConfig) -> None:
        self._safe_unlink(lock_path(cfg))

    def _is_preauth_failure(self, cfg: HostConfig, marker: str,
                            rec: _AttemptRecord) -> bool:
        """Did this attempt die before the auth phase — i.e. spend no MFA?

        Reads the captured ssh stderr rather than the folded reason string, and
        within it only ssh's OWN lines — the askpass writes its refusal to the same
        stream and that text mentions Duo, so including it turns "refused to answer"
        into "the Duo phase was reached". Conservative by construction: an
        auth-phase marker vetoes, a Duo-menu deviation vetoes, and no positive
        evidence at all -> False (hold the host).
        """
        if not rec.spawned:
            # Nothing ran. The vpn call, the 2fa arming and the exec all precede
            # any packet ssh could send, so a failure before the exec cannot have
            # reached a login — no stderr reading required, and none wanted,
            # since the file describes a different attempt entirely.
            return True
        try:
            with open(stderr_path(cfg), "r", encoding="utf-8",
                      errors="replace") as f:
                blob = f.read()
        except FileNotFoundError:
            return False  # no evidence — hold, don't guess
        if not blob.strip():
            return False
        ssh_only = "\n".join(
            ln for ln in blob.splitlines()
            if not ln.lstrip().startswith(_ASKPASS_PREFIX)).lower()
        if any(s.lower() in ssh_only for s in _AUTH_PHASE_STDERR):
            return False  # auth was reached — the veto
        if os.path.exists(marker) and not self._deviation_was_non_duo(marker):
            return False  # a Duo menu was presented — a push may have fired
        return any(s.lower() in ssh_only for s in _PREAUTH_STDERR)

    async def _duo_saw_nothing(self, cfg: HostConfig,
                               rec: _AttemptRecord) -> bool:
        """Did Duo observe no transaction at all while this attempt ran?

        The stderr rules above can only classify a failure that SAID something. A
        connect that hangs on the network says nothing, so "silence is not
        evidence" holds the host — correctly, in the absence of another witness.
        There is another witness. This attempt armed a burst on the 2fa approver,
        which polls Duo every second for the length of the window and counts what
        it sees. An unmoved count is a positive assertion from Duo's own API that
        no login was ever presented, which is exactly "no MFA attempt was spent".
        On 2026-09-01 fir was under vendor maintenance, Duo saw nothing across the
        whole window, and the host was held anyway and paged the operator twice.

        Every uncertainty answers False, i.e. hold:

        * a host with no 2FA device has no witness to ask;
        * a count we could not read at arm time, or cannot read now;
        * a count that went BACKWARDS, which means the approver restarted and its
          counter reset — not that nothing happened.

        The count is per DEVICE, and two hosts can share one (sockeye and
        sockeye1 both use cwl). So a sibling's transaction can make this answer
        False when our own attempt really did spend nothing. That is the mild
        direction of the mistake — a spurious hold — and it is the one the
        classification in this file has always preferred.
        """
        if not cfg.twofa_device or rec.twofa_seen_at_arm is None:
            return False
        try:
            info = await gatewayclient.call_maybe_peer(
                gatewayclient.peer_env(_TWOFA_PEER_ENV),
                "2fa", "status", {"device": cfg.twofa_device})
        except Exception as exc:  # noqa: BLE001 — cannot ask ⇒ cannot assert
            log.info("ssh: cannot check Duo activity for %s (2fa unreachable: "
                     "%s) — holding", cfg.host, exc)
            return False
        now = (info or {}).get("transactions_seen")
        if not isinstance(now, int) or now < rec.twofa_seen_at_arm:
            return False
        if now > rec.twofa_seen_at_arm:
            return False        # Duo heard a login — an attempt may have been spent
        log.warning("ssh: %s failed but Duo observed no transaction on device "
                    "%s for the whole attempt — no MFA attempt was spent, so "
                    "the host is NOT held", cfg.host, cfg.twofa_device)
        return True

    @staticmethod
    def _deviation_was_non_duo(marker: str) -> bool:
        """True when EVERY recorded deviation says the prompt was not a Duo menu.

        The marker is appended to, so one ssh run can record several reasons; a
        single Duo-menu deviation among them is enough to keep the veto.
        """
        try:
            with open(marker, "r", encoding="utf-8", errors="replace") as f:
                reasons = [ln.strip() for ln in f if ln.strip()]
        except OSError:
            return False
        if not reasons:
            return False
        return all(any(nd in r for nd in _NON_DUO_DEVIATIONS) for r in reasons)

    def _failure_reason(self, cfg: HostConfig, marker: str, base: str) -> str:
        parts = [base]
        if os.path.exists(marker):
            parts.append("askpass deviation (unrecognized Duo prompt — "
                         "refused rather than guessing)")
        tail = self._read_stderr_tail(cfg)
        if tail:
            parts.append(f"ssh: {tail}")
        return "; ".join(parts)

    @staticmethod
    def _read_stderr_tail(cfg: HostConfig) -> str:
        """Notable lines from the captured ssh stderr, folded to one string."""
        try:
            with open(stderr_path(cfg), "r", encoding="utf-8",
                      errors="replace") as f:
                lines = [ln.strip() for ln in f if ln.strip()]
        except FileNotFoundError:
            return ""
        notable = [ln for ln in lines
                   if any(s.lower() in ln.lower() for s in _NOTABLE_STDERR)]
        picked = notable or lines[-3:]
        return " | ".join(picked[-3:])

    def _lock_alert_text(self, cfg: HostConfig, reason: str,
                         *, requester: str | None = None) -> str:
        """The operator lockout page. Shared by the per-node breaker trip and the
        slot arbiter's LOCKED transition so both read identically.

        ``requester`` is the node whose attempt failed — which is only *this* node
        on the local-breaker path. On the arbiter path the alert is generated on
        the arbiter for someone else's attempt, so it must be passed in; naming
        ``_NODE_NAME`` unconditionally would attribute every fleet failure to the
        arbiter, which is the same defect as mira's alerts saying "pavilion"."""
        device = cfg.twofa_device or "your device"
        who = requester or "an unidentified node"
        return (
            f"🔒 awm-ssh held **{cfg.host}** after a failed connect from "
            f"**{who}** — further automated connects are refused so they can't "
            f"burn an MFA attempt toward provider lockout.\n"
            f"Reason: {reason}\n"
            f"To recover once you've checked it out: run `/approve {device}` in "
            f"Discord. While that window is open the service will reconnect on "
            f"its own.")

    async def _classify_cause(self, cfg: HostConfig, reason: str) -> str:
        """Which side is this failure attributable to? Decided at write time.

        Strict allowlist, never a fallback. A hold is bucketed ``_CAUSE_SELF``
        only when BOTH hold:

        * the failure was our own connect timeout — not an ssh-reported auth
          rejection, whose marker vetoes outright; and
        * a LIVE probe of the Duo approver fails right now, i.e. our approver is
          demonstrably unable to function.

        Anything else — an unrecognised reason, an unreachable 2fa service, a
        device we cannot ask about — is ``_CAUSE_EXTERNAL`` and stays
        operator-only. Failing to establish self-attribution is not evidence
        of it.

        The probe used to be a recency test against ``reachability``, which only
        ever moved when somebody called ``ping``. So the timestamp decayed with
        idle time alone, and a quiet approver was indistinguishable from a broken
        one. On 2026-09-01 that filed a maintenance outage on fir as
        "approver-unavailable" — and since that cause licenses one automatic
        retry, the next request cleared the hold and spent a second Duo push on a
        host that was switched off. Ask the approver instead of inferring from
        how long it has been quiet.
        """
        if not cfg.twofa_device:
            return _CAUSE_EXTERNAL
        blob = reason.lower()
        if any(s.lower() in blob for s in _AUTH_PHASE_STDERR):
            return _CAUSE_EXTERNAL          # auth was reached — the veto
        if "exceeded" not in blob or "s" not in blob:
            return _CAUSE_EXTERNAL          # not the timeout shape
        try:
            info = await gatewayclient.call_maybe_peer(
                gatewayclient.peer_env(_TWOFA_PEER_ENV),
                "2fa", "ping", {"device": cfg.twofa_device})
        except Exception as exc:  # noqa: BLE001 — cannot ask ⇒ cannot attribute
            log.info("ssh: cannot attribute %s's failure (2fa unreachable: %s) "
                     "— holding as external", cfg.host, exc)
            return _CAUSE_EXTERNAL
        if (info or {}).get("ok"):
            # The approver just proved itself — so this failure is not ours.
            return _CAUSE_EXTERNAL
        log.warning("ssh: %s timed out while our own Duo approver was NOT "
                    "verifiably reachable — recording a self-inflicted hold, "
                    "clearable once the approver proves itself healthy",
                    cfg.host)
        return _CAUSE_SELF

    async def _self_clear_ok(self, cfg: HostConfig) -> bool:
        """May this hold be lifted without an operator, right now?

        Evaluated LAZILY, on an inbound request — deliberately not by a
        background sweep over lockfiles, which is one small step from "and
        then reconnect". Clearing a hold grants permission for an attempt
        somebody asks for; it never starts one.

        Requires a FRESH POSITIVE assertion: a live ``2fa ping`` that actually
        round-trips to Duo. "No error seen" is not an assertion.
        """
        lock = self._read_lock(cfg)
        if lock is None or lock.get("cause") != _CAUSE_SELF:
            return False
        if not cfg.twofa_device:
            return False
        try:
            res = await gatewayclient.call_maybe_peer(
                gatewayclient.peer_env(_TWOFA_PEER_ENV),
                "2fa", "ping", {"device": cfg.twofa_device})
        except Exception as exc:  # noqa: BLE001
            log.info("ssh: %s stays held — could not verify the approver: %s",
                     cfg.host, exc)
            return False
        if not (res or {}).get("reachable"):
            log.info("ssh: %s stays held — the approver still cannot reach Duo",
                     cfg.host)
            return False
        log.warning("ssh: self-clearing the hold on %s — it was recorded as "
                    "%s (%s) and the Duo approver has just verified a live "
                    "round-trip for device %r. This grants ONE attempt.",
                    cfg.host, _CAUSE_SELF, lock.get("reason"), cfg.twofa_device)
        return True

    async def _maybe_self_clear(self, cfg: HostConfig) -> bool:
        """Clear a self-inflicted hold if the approver proves itself healthy.

        One-shot by construction: the lockfile is removed, so the very next
        failure re-writes it. Pages the operator informationally — a hold that
        lifted itself must never be silent.
        """
        if not await self._self_clear_ok(cfg):
            return False
        lock = self._read_lock(cfg) or {}
        self._clear_lock(cfg)
        await self._alert(
            f"🔁 awm-ssh [{_NODE_NAME}] released its own hold on "
            f"**{cfg.host}**.\n"
            f"The hold was recorded against our own Duo approver being "
            f"unreachable ({lock.get('reason')}), and the approver has just "
            f"verified a live round-trip. One attempt is now permitted; a "
            f"genuine failure will hold it again immediately.\n"
            f"No action needed.")
        return True

    async def _trip_breaker(self, cfg: HostConfig, reason: str) -> None:
        """Hold the host after a failed connect and page the operator. Threshold=1."""
        cause = await self._classify_cause(cfg, reason)
        self._write_lock(cfg, reason, cause=cause)
        log.error("BREAKER TRIPPED — holding %s (%s): %s", cfg.host, cause, reason)
        # Local (non-arbiter) path: the attempt was ours, so we are the requester.
        await self._alert(
            self._lock_alert_text(cfg, reason, requester=_NODE_NAME))

    async def _send_social(self, text: str) -> Any:
        """Post ``text`` to Discord unimatrix0#notifications and RETURN the social
        send result. Touches no lockfile. The one federated notify wire, shared by
        the swallowing lock alert (:meth:`_alert`) and the result-surfacing
        self-test (:meth:`notify_test`) so both exercise the identical path."""
        return await gatewayclient.call_maybe_peer(
            gatewayclient.peer_env(_SOCIAL_PEER_ENV),
            "social", "send", {
                "account": _ALERT_ACCOUNT,
                "channel": _ALERT_CHANNEL,
                "text": text,
            })

    async def _alert(self, text: str) -> None:
        """Post to Discord unimatrix0#notifications. Never raises into connect."""
        try:
            await self._send_social(text)
        except Exception as e:
            log.error("failed to post lock alert to Discord: %s", e)

    async def notify_test(self, host: str = "[selftest]") -> dict:
        """Fire the Discord lock-alert wire on demand so the operator can confirm
        the ssh service can actually reach them BEFORE a real lockout depends on it.

        Sends through the EXACT :meth:`_send_social` path a real breaker trip uses
        (same peer selector, edge, bearer, account + channel), but writes NO
        lockfile and does NOT mutate breaker state, and — unlike :meth:`_alert` —
        does NOT swallow: the send result is surfaced so a broken notify wire fails
        loudly (that failure IS the signal). The message is unmistakably a test and
        carries no ``/approve`` instruction, so it can't train a reflex to clear a
        device."""
        peer = gatewayclient.peer_env(_SOCIAL_PEER_ENV)
        text = (
            f"🧪 awm-ssh [{_NODE_NAME}] notify self-test for **{host}** — the "
            f"Discord alert wire is working. No action needed; this is NOT a "
            f"real lock."
        )
        result = await self._send_social(text)  # surfaces on failure — no try/except
        return {
            "sent": True,
            "peer": peer or "local",
            "account": _ALERT_ACCOUNT,
            "channel": _ALERT_CHANNEL,
            "result": result,
        }

    async def receive_test(self, timeout: float = 10.0) -> dict:
        """Prove the INBOUND ``/approve`` wire on demand — the twin of
        :meth:`notify_test`.

        ``notify_test`` proves this service can reach the operator. Nothing
        proved it could still *hear* them, and the only evidence was a real
        successful ``/approve`` — which is why three of them were swallowed on
        2026-07-26 with no signal at all.

        Asks social to emit a synthetic, inert probe carrying only a nonce,
        then waits for the subscription to hand that nonce back. The probe
        never reaches the domain handler (it is intercepted first), carries no
        device, and cannot open a window or arm anything.
        """
        sub = self._approve_sub
        if sub is None:
            raise RuntimeError(
                "ssh: the /approve subscription was never started — this "
                "service cannot hear an operator approve")
        nonce = secrets.token_urlsafe(8)
        waiter: asyncio.Future = asyncio.get_running_loop().create_future()
        self._probe_waiters[nonce] = waiter
        peer = gatewayclient.peer_env(_SOCIAL_PEER_ENV)
        try:
            # Emit failures must NOT read as "you are deaf" — an un-upgraded
            # peer that lacks emit_probe is a different fault entirely.
            try:
                await gatewayclient.call_maybe_peer(
                    peer, "social", "emit_probe", {"nonce": nonce})
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"ssh: could not ask social@{peer or 'local'} to emit a "
                    f"probe ({exc}) — this says nothing about whether this "
                    f"service can receive; check social is up and upgraded"
                ) from exc
            try:
                await asyncio.wait_for(waiter, timeout=timeout)
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"ssh: probe emitted but not received within {timeout:.0f}s "
                    f"— this service is DEAF to social/command, so an operator "
                    f"/approve would not reach it"
                ) from None
        finally:
            self._probe_waiters.pop(nonce, None)
        return {
            "received": True,
            "peer": peer or "local",
            "subscription": self._subscription_health(),
        }

    def _intercept_probe(self, ev: object) -> bool:
        """Claim a self-test probe before it can reach ``_handle_approve``."""
        if not isinstance(ev, dict) or ev.get("command") != _PROBE_COMMAND:
            return False
        waiter = self._probe_waiters.get(str(ev.get("nonce") or ""))
        if waiter is not None and not waiter.done():
            waiter.set_result(True)
        return True

    @staticmethod
    def _status_dict(cfg: HostConfig, status: str, *,
                     error: str = "", warning: str = "") -> dict:
        d: dict = {
            "host": cfg.host,
            "user": cfg.user,
            "port": cfg.port,
            "status": status,
        }
        if error:
            d["error"] = error
        if warning:
            d["warning"] = warning
        return d
