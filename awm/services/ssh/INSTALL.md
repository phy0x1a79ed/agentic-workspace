# SSH Service

Manages headless ControlMaster SSH connections to managed hosts (sockeye,
fir, chamois, micb0). Orchestrates VPN and 2FA burst automatically before
connecting, so the caller gets a seamless single-verb API.

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

| Host | VPN required | 2FA device | SSH user | Guarded |
|------|-------------|------------|----------|---------|
| sockeye, sockeye1-3 | ubc | cwl | txyliu | no |
| fir | — | alliance | phyberos | **yes** |
| chamois | ubc | cwl | tliu | no |
| micb0 | ubc | cwl | tliu | no |

## MFA-lockout hold (operator note)

A single failed connect to a host puts that host into a **hold**: further
automated `connect`s are refused **before** any VPN/2FA/ssh runs, so a host that
failed once can never march toward the provider's MFA-lockout ceiling. The
operator is paged on the Discord `unimatrix0#notifications` channel.

**Recovery is operator-gated, out of band.** There is no verb and no self-serve
clear — deliberately, so an autonomous caller cannot lift its own hold. To
restore access, the operator runs `/approve <device>` in Discord (the same
command that arms a 2FA burst); while that approval window is open the service
reconnects on its own, and a successful connect clears the hold. A successful
connect is also the only automatic clear.

Holds are per-host (a `fir` hold does not affect `sockeye`).

## Dependencies

- `awm-config`, `awm-persistence`, `awm-gatewayclient` (shared components)
- System `ssh` (from PATH)
- `~/.ssh/awm-duo-askpass` — SSH_ASKPASS helper for Duo auto-approval.
  **Fail-closed:** answers only an exactly-matched Duo device menu; any
  unrecognized prompt (or no option naming one of our devices) makes it refuse
  and drop a deviation marker rather than guessing an option.
- `~/.ssh/awm-ssh-guard` — ProxyCommand guard for guarded hosts (see below).
- The `vpn`, `2fa`, and `social` services must be running on the gateway.
  `social` carries both the operator alert and the `/approve` recovery signal
  (the service subscribes to social's `command` emit, like `2fa` does); a
  `social` hiccup can't mask a hold (the alert is best-effort, the hold is not).

## ControlMaster behaviour

Non-guarded hosts live in a shared `~/.ssh/config` block:

```
host sockeye* chamois shamwow micb0
    ControlMaster auto
    ControlPath ~/.ssh/live_connections/%h_%p_%r
    ServerAliveInterval 60s
```

The service opens connections with `ssh -f -N -M <host>`, which creates a
ControlMaster socket at the configured path. Subsequent `ssh`/`scp`/`rsync`
calls to the same host reuse this socket with no re-authentication.

### Guarded hosts (`fir`)

`fir` is deliberately **not** in the shared `ControlMaster auto` block. It has
its own block with `ControlMaster no` plus a ProxyCommand guard:

```
Host fir
    HostName fir.computecanada.ca
    User phyberos
    ...
    ControlMaster no
    ControlPath ~/.ssh/live_connections/%h_%p_%r
    ServerAliveInterval 60s
    ProxyCommand ~/.ssh/awm-ssh-guard %h %p
```

OpenSSH runs a `ProxyCommand` **only when no live master exists**. So:

- **Master up** (service connected) → `ssh fir` multiplexes over the socket, the
  guard never runs, zero new auth.
- **No master** → a bare `ssh fir` hits the guard, which prints a loud "use the
  ssh service, never direct" message to stderr and exits non-zero **before any
  Duo push** — no MFA attempt is spent.

The service is the sole caller allowed to create the master: its `-f -N -M`
connect adds `-o ProxyCommand=none` for guarded hosts (command-line `-o` wins
over config), bypassing the guard to lay down the socket. This is a *guardrail*,
not an OS boundary — the same Unix user can still bypass with `-o
ProxyCommand=none` — the point is to make the naive/default `ssh fir` safe.

To guard another host, set `guarded=True` on its `HostConfig` in `config.py` and
give it an equivalent `Host` block. (VPN-bounced hosts like `sockeye*` can't take
this guard as-is — their `ProxyCommand` is the required tunnel; the circuit
breaker still covers them at the service layer.)

## Failure visibility

The `-f -N -M` connect's stderr is captured to
`~/.ssh/live_connections/<host>.connect.stderr` (a file, not a PIPE — a PIPE
makes the forked child hang), and notable lines (`Permission denied`, `Too many
authentication failures`, MFA/Duo/account strings) are logged and recorded with
the hold so the operator can see why a connect failed.
