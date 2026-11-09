from __future__ import annotations

import asyncio
import enum
import fcntl
import logging
import os
import secrets
import signal
import stat
from dataclasses import dataclass

from awm import config, gatewayclient
from awm.ssh.config import (
    AUTH_PHASE_STDERR,
    KNOWN_HOSTS,
    LIVE_DIR,
    SINGLETON_PATH,
    SSH_ASKPASS,
    HostConfig,
    pid_path,
    resolve_host,
    stderr_path,
)

log = logging.getLogger("awm.ssh.service")

_CONNECT_TIMEOUT = 120.0
# Budget for bringing the host's VPN exit up, spent BEFORE the ssh clock above
# starts. A cold UBC dial is a docker run plus a Duo-approved openconnect login;
# it must cover the vpn service's own worst case (`vpn.container.UP_BUDGET_S`,
# 240s) or a slow docker daemon reads here as a dead host. It used to be the
# gateway's 30s default, and on 2026-10-16 every connect to sockeye after the
# tunnel dropped gave up while the vpn service was still dialing.
_VPN_UP_TIMEOUT = 270.0
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

# Which node is speaking. Comes from the node env, not the OS hostname.
_NODE_NAME = config.node_name()

# The 2fa service runs as a canonical singleton on a peer node, named here.
# Read per use via gatewayclient.peer_env.
_TWOFA_PEER_ENV = "AWM_TWOFA_PEER"
# The node that runs ssh-arbiter, which grants the one in-flight login per host
# across the fleet.
_ARBITER_PEER_ENV = "AWM_SSH_SLOT_PEER"


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


class ConnState(enum.Enum):
    """The single canonical per-host connection lifecycle.

    Every host moves DISCONNECTED → AUTHENTICATING → CONNECTED → DISPOSING →
    DISCONNECTED and nowhere else. ``connect`` is the sole entry; duplicate/
    concurrent requests are absorbed into the one in-flight attempt rather than
    minting a second ssh/MFA.
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


class _DependencyFailed(Exception):
    """Something the login needs (the VPN exit, the Duo approver) failed before
    ssh ran. The message names it, because the caller has to know what to wait
    for; "the host refused" sent agents chasing sockeye when the tunnel was down."""


# A ControlMaster outlives the connect that made it, but by default it lives in
# the cgroup of whoever spawned it, and on a systemd node that is awm.service:
# every `awm deploy` or gateway restart stops the unit, systemd kills the whole
# cgroup, and every master on the node dies with it (six times on altair on
# 2026-10-16). A transient scope of its own takes the master out of that cgroup,
# and boot reconciliation adopts it again after the restart. `--scope` execs the
# command in place, so the pid and process group the reap relies on are ssh's.
# This works only under a user manager that owns our cgroup (altair, mira). On a
# node whose gateway is a system unit (capella) the probe fails and masters are
# spawned as before.
_DETACH_PREFIX = ["systemd-run", "--user", "--scope", "--quiet", "--collect"]


def master_argv(cfg: HostConfig) -> list[str]:
    """The ssh argv that lays down a host's ControlMaster."""
    argv = ["ssh", "-f", "-N", "-M", "-o", "NumberOfPasswordPrompts=1"]
    # ssh reads the command line before the config, and the first of ProxyJump
    # and ProxyCommand set wins. A vpn host gets `-J vpn_<profile>`, so the
    # service's master skips the config's guard ProxyCommand while every other
    # caller hits it. A guarded host with no vpn gets `ProxyCommand=none` instead.
    if cfg.needs_vpn and cfg.vpn_profile:
        argv += ["-J", f"vpn_{cfg.vpn_profile}"]
    elif cfg.guarded:
        argv += ["-o", "ProxyCommand=none"]
    argv.append(cfg.host)
    return argv


class SSHService:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._hosts: dict[str, HostState] = {}
        self._reconcile_task: asyncio.Task | None = None
        # Held open for the process lifetime once acquired, so the flock stays
        # held (releasing on process death). Stored to keep the fd from being GC'd.
        self._singleton_fd: int | None = None
        # Whether masters can be spawned in their own systemd scope; probed once,
        # on the first connect.
        self._can_detach: bool | None = None

    def init(self) -> None:
        # Enforce the process singleton BEFORE anything else — if another svc-ssh
        # already holds the lock, stand down cleanly rather than racing the same
        # account. os._exit (not sys.exit) so it's a clean process exit and never
        # an exception into the adapter.
        self._acquire_singleton()
        os.makedirs(LIVE_DIR, exist_ok=True)
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
            os.makedirs(os.path.dirname(SINGLETON_PATH), mode=0o700, exist_ok=True)
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
        live ControlMaster, then reap stale control sockets left by a killed
        process."""
        try:
            for name, cfg in KNOWN_HOSTS.items():
                if await self._check_master(cfg.host):
                    hs = self._host(name)
                    async with self._lock:
                        if hs.state == ConnState.DISCONNECTED:
                            hs.state = ConnState.CONNECTED
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
            else:
                connections[name] = self._status_dict(cfg, "disconnected")
        return {"connections": connections}


    # -- attempt / disposal drivers -----------------------------------------

    async def _run_attempt(self, cfg: HostConfig, hs: HostState) -> dict:
        """The single auth attempt. Always resolves via an internal outcome — a
        success or a breaker trip — never by outer cancellation, so the breaker
        can never be bypassed. Transitions the host on completion."""
        try:
            result = await self._do_connect(cfg)
        except Exception as e:  # noqa: BLE001
            # A raise must not strand the host in AUTHENTICATING. The lease
            # dropped without a verdict, so the arbiter holds the host.
            log.error("connect to %s raised unexpectedly: %s", cfg.host, e)
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
        """Lease the host's fleet-wide slot from ``ssh-arbiter``, run the one attempt
        under it, then report the verdict. The open socket is the lease, so a drop
        mid-attempt locks the host on the arbiter. An unset or unreachable arbiter
        refuses the connect before any VPN, 2FA or ssh work."""
        arbiter = gatewayclient.peer_env(_ARBITER_PEER_ENV)
        if not arbiter:
            return self._status_dict(
                cfg, "error",
                error=(f"{_ARBITER_PEER_ENV} names no connection arbiter on this "
                       f"node, so no login to {cfg.host} was attempted"))
        try:
            lease = await gatewayclient.acquire_lease("ssh-arbiter", cfg.host, peer=arbiter, node=_NODE_NAME)
        except Exception as e:  # noqa: BLE001 — arbiter unreachable, fail closed
            log.error("connection arbiter %s unreachable for %s: %s", arbiter, cfg.host, e)
            return self._status_dict(
                cfg, "error",
                error=(f"the connection arbiter (ssh-arbiter@{arbiter}) did not answer, "
                       f"so no login to {cfg.host} was attempted and no MFA "
                       f"attempt was spent — safe to retry once {arbiter} "
                       f"is reachable"))
        async with lease:
            if not lease.granted:
                log.info("arbiter %s refused %s: %s (%s)",
                         arbiter, cfg.host, lease.status, lease.reason)
                return self._status_dict(
                    cfg, "unavailable",
                    error=f"{cfg.host} is not available for automated access right now")
            result = await self._do_connect_attempt(cfg)
            ok = self._verdict_ok(result)
            reason = result.pop("_lock_reason", "")
            await lease.verdict(ok=ok, reason=reason)
            return result

    @staticmethod
    def _verdict_ok(result: dict) -> bool:
        """The verdict answers "was the lockout budget spent?", not "did it connect".

        A connect that died before the auth phase reports ``ok``, so the arbiter
        frees the slot instead of holding the host for a failure that spent nothing.
        """
        preauth = result.pop("_preauth", False)
        return result.get("status") == "connected" or bool(preauth)

    async def _do_connect_attempt(self, cfg: HostConfig) -> dict:
        """Bring up the ControlMaster (vpn + 2fa + ssh + poll) within an internal
        timeout. Never raises. A failure returns an error dict carrying
        ``_lock_reason`` and ``_preauth`` for the lease verdict. See
        :meth:`_is_preauth_failure`."""
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
            log.info("connecting to %s (vpn=%s, 2fa=%s)",
                     cfg.host, cfg.vpn_profile or "none",
                     cfg.twofa_device or "none")
            await self._bring_up_vpn(cfg)
            await asyncio.wait_for(self._attempt_master(cfg, marker, rec),
                                   timeout=_CONNECT_TIMEOUT)
            log.info("connected to %s", cfg.host)
            return self._status_dict(cfg, "connected")
        except _DependencyFailed as e:
            # Nothing ran, so nothing was spent: no hold, no page. Say what
            # failed, since it is not the host.
            log.warning("connect to %s stopped before any login: %s",
                        cfg.host, e)
            result = self._status_dict(
                cfg, "error",
                error=(f"{e} — no login to {cfg.host} was attempted and no MFA "
                       f"attempt was spent, safe to retry"))
            result["_lock_reason"] = str(e)
            result["_preauth"] = True
            return result
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
            detail = str(e) or type(e).__name__
            log.error("connect to %s failed: %s", cfg.host, detail)
            reason = self._failure_reason(cfg, marker, detail)
        preauth = (self._is_preauth_failure(cfg, marker, rec)
                   or await self._duo_saw_nothing(cfg, rec))
        if preauth:
            # Nothing was spent, so there is nothing to protect: no hold, no page.
            # Tell the caller it is safe to retry — unlike a held host, this needs
            # no /approve.
            log.warning("pre-auth failure on %s (no MFA attempt spent): %s",
                        cfg.host, reason)
            if rec.spawned:
                what = f"{cfg.host} refused the connection before authentication"
            else:
                what = (f"connect to {cfg.host} failed before ssh ran "
                        f"({reason or 'no detail'})")
            result = self._status_dict(
                cfg, "error",
                error=f"{what} — no MFA attempt was spent, safe to retry later")
        else:
            # Neutral to the caller — the detailed reason goes to the lock + the
            # operator Discord alert, not to the agent.
            result = self._status_dict(
                cfg, "error", error=f"connect to {cfg.host} failed")
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
            twofa_peer = gatewayclient.peer_env(_TWOFA_PEER_ENV)
            try:
                armed = await gatewayclient.call("2fa", "burst", {
                        "device": cfg.twofa_device,
                        "window": 120,
                        "count": 1,
                    }, peer=twofa_peer)
            except Exception as exc:  # noqa: BLE001 — named for the caller
                # Without an armed approver the push would wait on a phone
                # nobody is holding, and time out as a failed MFA attempt.
                raise _DependencyFailed(
                    f"the Duo auto-approver (2fa@{twofa_peer or _NODE_NAME}) "
                    f"did not answer: {str(exc) or type(exc).__name__}") from exc
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
        argv = master_argv(cfg)
        argv = await self._detach_prefix(cfg) + argv

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
                return
            await asyncio.sleep(_CHECK_POLL_INTERVAL)

        raise _AttemptFailed(
            f"ControlMaster did not appear within "
            f"{_CHECK_POLL_ATTEMPTS * _CHECK_POLL_INTERVAL:.0f}s")

    async def _bring_up_vpn(self, cfg: HostConfig) -> None:
        """Ensure the host's VPN exit is up, within its own budget.

        Idempotent on the vpn side: a live exit answers at once, a dead one is
        re-dialed. Raises :class:`_DependencyFailed` naming the exit."""
        if not (cfg.needs_vpn and cfg.vpn_profile):
            return
        try:
            result = await gatewayclient.call(
                "vpn", "up", {"profile": cfg.vpn_profile},
                timeout=_VPN_UP_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 — named for the caller
            raise _DependencyFailed(
                f"the '{cfg.vpn_profile}' VPN exit could not be brought up: "
                f"{str(exc) or type(exc).__name__}") from exc
        log.info("vpn up %s: %s", cfg.vpn_profile,
                 (result or {}).get("status", "ok"))

    async def _detach_prefix(self, cfg: HostConfig) -> list[str]:
        """The argv prefix that spawns a master in its own systemd scope, or
        ``[]`` where that is not possible. See ``_DETACH_PREFIX``."""
        if self._can_detach is None:
            self._can_detach = await self._run_check(
                [*_DETACH_PREFIX, "--", "true"])
            log.info("ssh: masters %s", "run in their own systemd scope"
                     if self._can_detach else
                     "share the service's cgroup (no user systemd scope here)")
        if not self._can_detach:
            return []
        unit = f"awm-ssh-{cfg.host}-{secrets.token_hex(4)}"
        return [*_DETACH_PREFIX, f"--unit={unit}", "--"]

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

    # -- what an attempt left behind ----------------------------------------

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
        if any(s.lower() in ssh_only for s in AUTH_PHASE_STDERR):
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
            info = await gatewayclient.call("2fa", "status", {"device": cfg.twofa_device}, peer=gatewayclient.peer_env(_TWOFA_PEER_ENV))
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
