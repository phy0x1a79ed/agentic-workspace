# SSH Service

Manages headless ControlMaster SSH connections to managed hosts (fir, nibi,
trillium and sockeye). Before connecting it takes the host's slot from
`ssh-arbiter` on alcor and arms the 2FA burst, so the caller gets a single-verb
API. A sockeye master jumps through the UBC tunnel on altair.

> **Never `ssh fir` directly.** `fir` (Alliance) locks the account after 10
> failed MFA attempts in a row (support ticket #0317299 — it happened on
> 2026-07-02). A bare `ssh fir` fires a fresh MFA attempt every time. Always go
> through this service (`ssh(verb=connect, host=fir)`); it holds a reusable
> ControlMaster that plain `ssh fir <cmd>` then multiplexes over with zero new
> auth. A `~/.ssh/config` guard now *enforces* this (see below).

## API

| Verb | Args | Description |
|------|------|-------------|
| `connect` | `host` (string, required) | Open ControlMaster connection to host; blocks until socket is live |
| `disconnect` | `host` (string, required) | Close ControlMaster connection to host |
| `status` | none | List all managed hosts with connection state |

## Managed hosts

| Host | Jump | 2FA device | SSH user | Guarded |
|------|------|------------|----------|---------|
| sockeye, sockeye1-3 | `vpn_ubc` (altair's tunnel) | cwl | txyliu | no |
| fir, nibi, trillium, trillium-gpu | — | alliance | phyberos | **yes** |

The 2FA column is the host's own `twofa_device`, which is what decides whether a
connect goes through the fleet slot arbiter.

## MFA-lockout hold (operator note)

A single failed connect that could have spent an MFA attempt puts that host into
a **hold**: further automated `connect`s are refused **before** any 2FA or ssh
runs, so a host that failed once can never march toward the provider's
MFA-lockout ceiling. The operator is paged in their Discord DM with the bot
(the node's operator DM, `AWM_OPERATOR_DM_CHANNEL` in `FEDERATION.md`). See *What is allowed to hold a host* for the
three failures that are exempt because they demonstrably spent nothing.

**Recovery is operator-gated, out of band.** There is no verb and no self-serve
clear — deliberately, so an autonomous caller cannot lift its own hold. To
restore access, the operator runs `/approve <device>` in Discord (the same
command that arms a 2FA burst); the next connect during that window clears the
hold and reconnects, and a successful connect clears the hold. A successful
connect is also the only automatic clear.

**One `/approve` authorises exactly one reconnect attempt** — the window is
*consumed* the moment it is spent. This is deliberate: it stops a caller that
keeps retrying a persistently-failing connect from re-clearing and re-firing an
MFA push every iteration (an unbounded run toward the provider lockout). A
consequence: when several hosts share a device (e.g. `sockeye`/`sockeye1` both on
`cwl`) and all are held, a single `/approve cwl` recovers only the **first** host
to reconnect; the siblings stay held and each need their own `/approve`.

Holds are per-host (a `fir` hold does not affect `sockeye`). Note `status` reports
a held host only as `unavailable` with no reason string — the failure reason lives
in the lockfile and the one-shot Discord alert, not in the verb output.

**One hold in one case lifts itself**, recorded as `approver-unavailable`: the
failure was our own Duo approver being unable to function, which is verifiably
over once the approver proves itself, so requiring a human then would be
theatre. Attribution asks the approver with a live `2fa ping` rather than
inferring from how long it has been since anyone did. That distinction is
load-bearing. The old test read a timestamp that only an explicit `ping`
refreshed, so it decayed with idle time and filed a healthy-but-quiet approver as
broken — and since this cause grants one automatic retry, the next request spent
a second Duo push at a host that was already down for maintenance.

## Dependencies

- `awm-config`, `awm-persistence`, `awm-gatewayclient` (shared components)
- System `ssh` (from PATH)
- `~/.ssh/awm-duo-askpass` — SSH_ASKPASS helper for Duo auto-approval.
  **Fail-closed:** answers only an exactly-matched Duo device menu; any
  unrecognized prompt (or no option naming one of our devices) makes it refuse
  and drop a deviation marker rather than guessing an option.
- `~/.ssh/awm-ssh-guard` — ProxyCommand guard for guarded hosts (see below).
- The `ssh-arbiter` and `2fa` singletons on alcor must answer. The arbiter
  keeps the holds, sends the operator alert and listens for `/approve`. A
  `social` hiccup can't mask a hold (the alert is best-effort, the hold is not).

## ControlMaster behaviour

Non-guarded hosts live in a shared `~/.ssh/config` block:

```
host sockeye* shamwow
    ControlMaster auto
    ControlPath ~/.ssh/live_connections/%h_%p_%r
    ServerAliveInterval 60s
```

The service opens connections with `ssh -f -N -M <host>`, which creates a
ControlMaster socket at the configured path. Subsequent `ssh`/`scp`/`rsync`
calls to the same host reuse this socket with no re-authentication.

### Guarded hosts (`fir`)

`fir` is deliberately **not** in the shared `ControlMaster auto` block. It has
its own block with `ControlMaster no` plus a ProxyCommand guard. List **both the
alias and the FQDN** on the `Host` line — OpenSSH matches `Host` patterns against
the name you *type*, so an alias-only block leaves `ssh fir.computecanada.ca`
unguarded (it would fire a fresh Duo push with nothing stopping it):

```
Host fir fir.computecanada.ca
    HostName fir.computecanada.ca
    User phyberos
    ...
    ControlMaster no
    ControlPath ~/.ssh/live_connections/%h_%p_%r
    ServerAliveInterval 60s
    ProxyCommand ~/.ssh/awm-ssh-guard %h %p
```

Because `HostName` sets `%h` to `fir.computecanada.ca` for either typed name, the
`ControlPath` resolves to one shared socket, so both names guard and multiplex
together (verify with `ssh -G fir` vs `ssh -G fir.computecanada.ca`).

OpenSSH runs a `ProxyCommand` **only when no live master exists**. So:

- **Master up** (service connected) → `ssh fir` multiplexes over the socket, the
  guard never runs, zero new auth.
- **No master** → a bare `ssh fir` (or `ssh fir.computecanada.ca`) hits the
  guard, which prints a loud "use the ssh service, never direct" message to
  stderr and exits non-zero **before any Duo push** — no MFA attempt is spent.

The service is the sole caller allowed to create the master. Its `-f -N -M`
connect adds `-o ProxyCommand=none` for a guarded host with no vpn (command-line
`-o` wins over config), bypassing the guard to lay down the socket. For a vpn
host it passes `-J vpn_<profile>` instead. ssh sets the first of ProxyJump and
ProxyCommand it sees, so the jump replaces the config's guard ProxyCommand. This is a *guardrail*,
not an OS boundary — the same Unix user can still bypass with `-o
ProxyCommand=none` — the point is to make the naive/default `ssh fir` safe.

To guard another host, set `guarded=True` on its `HostConfig` in `config.py` and
give it an equivalent `Host` block listing **every name it can be reached by**
(alias and FQDN).

`sockeye` carries the same guard as `fir`: a `ProxyCommand` guard in its config
block. A bare `ssh sockeye` with no master hits the guard before any Duo push.
The service's master bypasses the guard with `-J vpn_ubc`.

## Failure visibility

The `-f -N -M` connect's stderr is captured to
`~/.ssh/live_connections/<host>.connect.stderr` (a file, not a PIPE — a PIPE
makes the forked child hang), and notable lines (`Permission denied`, `Too many
authentication failures`, MFA/Duo/account strings) are logged and recorded with
the hold so the operator can see why a connect failed.

The capture is emptied when an attempt **starts**, not when ssh spawns. It is
read as evidence about the attempt being judged, and a failure that never
reached the exec would otherwise inherit the verdict of whichever attempt last
got that far — on 2026-09-01 a `vpn/up` timeout on sockeye was recorded, and
paged, quoting a successful login from six days earlier.

## What is allowed to hold a host

A hold refuses automated access until an operator runs `/approve`, so it is only
ever justified by an MFA attempt that could actually have been spent. Three
answers are treated as proof that none was:

1. **ssh never ran.** The lease, the 2FA arming and the exec all precede any
   packet ssh could send, so a failure before the exec cannot have reached a
   login. No stderr is consulted.
2. **ssh said it died before auth** — `kex_exchange_identification`,
   `Exceeded MaxStartups`, a host-key failure, and the rest of the list in
   `service.py`. Any auth-phase marker vetoes this.
3. **Duo saw nothing.** Every gated connect arms a burst on the approver, which
   polls Duo once a second for the length of the window and counts what it sees.
   An unchanged count across the attempt is Duo's own API asserting no login was
   presented. This is the only witness for a connect that hangs on the network,
   which says nothing at all — the shape of a vendor maintenance outage.

The count is per **device**, and `sockeye`/`sockeye1` share one. A sibling's
transaction therefore reads as ours and produces a spurious hold. That is the
mild direction: mistaking an auth failure for a pre-auth one keeps retrying and
walks the account toward lockout, so every uncertainty resolves to holding.

## Reaping the ssh we spawned

An ssh that hangs before authenticating never creates its ControlMaster socket,
so nothing that speaks through the socket can reach it — not `ssh -O exit`, not
the stale-socket sweep. Two such processes outlived their attempt by minutes on
2026-09-01, each holding an armed Duo window.

- Cancelling an attempt — the 120s timeout, a shutdown — signals the child's
  **process group**, SIGTERM then SIGKILL. The spawn passes
  `start_new_session=True` for exactly this: without it the group is the
  service's own and the signal is suicide. The group is also what covers the
  askpass helper ssh forks beneath itself.
- The pid is recorded at `~/.ssh/live_connections/<host>.master.pid` and removed
  when the attempt resolves. A service that is itself killed leaves the file, and
  the next start reaps what it names — but only after confirming the live process
  still carries this service's `AWM_SSH_ASKPASS_MARKER` in its environment. A pid
  is the least durable key there is, and matching on the command line instead
  would match any ssh on the box, including one a person is using.

## Masters survive a gateway restart

A master spawned into awm.service's cgroup dies on every `awm deploy`, because
stopping the unit kills the whole cgroup however the child forked. The service
spawns each master through `systemd-run --user --scope` instead, as a unit named
`awm-ssh-<host>-<hex>.scope`. Boot reconciliation adopts the live master after
the restart. The service probes `systemd-run` once per life. A node whose gateway
runs as a system unit (capella) fails the probe and spawns masters bare.

The service never brings a VPN up. altair's vpn service keeps the UBC tunnel up
by itself. When the tunnel is down, the jump is refused before auth, the error
names the tunnel, and no Duo push is spent.

**CAUTION** The askpass keeps its rate-cap state in `~/.ssh/awm-duo-locks/`, and
answering counts toward a cap that trips a 30-minute hold refusing all Duo
answers for the device. Anything that exercises the real helper must point
`AWM_DUO_LOCK_DIR` somewhere disposable. The test suite does. Three runs against
the default directory inside ten minutes break the live login path.
