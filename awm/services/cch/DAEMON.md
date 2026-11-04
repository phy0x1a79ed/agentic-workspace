# The kept Claude Code daemon

## Purpose & Contents

cch keeps one Claude Code daemon running on its node, in a user unit of its
own (`awm/cch/daemon.py`). Every background session on the node is a child of
that daemon. When the daemon dies, cch refuses to start, resume or stop a
session, and social's keeper waits. cch also warns when the node runs low on
memory.

This file holds what the code does not show: why the daemon lives in a
transient user unit, why the unit carries each of its properties, what a node
needs before cch can launch, what cch does with a daemon it did not
start, how to stop the daemon on purpose, and who hears a low-memory warning. The verbs are
`harness-cc` `daemon_status`, `daemon_ensure`, `daemon_hold` and `daemon_release`.

## Install

The daemon keeper is part of cch and needs no step of its own on a node with a
user systemd manager.

Check it with `awm harness-cc daemon-status`. A healthy node reports
`"kept": true`, `"origin": "service"`, `"unit": "awm-claude-daemon.service"`
and `"linger": true`.

### Prerequisites

1. **A user systemd manager.** cch launches the daemon with `systemd-run
   --user`. Without a user bus it launches nothing and says why in
   `daemon_status`. The only other place to launch from is the gateway's own
   control group, and a gateway restart would then kill every session.
2. **Linger.** Without linger the user manager, and with it the daemon, exits
   at logout and does not start at boot. cch runs `loginctl enable-linger`
   for its own user once. polkit allows that without sudo on Ubuntu 24.04. A
   host that refuses it needs `sudo loginctl enable-linger <user>`.
3. **A gateway that sees `/run/user` and the real `/tmp`.** A gateway run as a
   system unit (deneb) reaches the user manager through
   `BindPaths=/run/user`. The daemon's sockets live under
   `/tmp/cc-daemon-<uid>/`.

### Configuration

Set these in the gateway's environment (`/etc/awm/env` on a system-unit node):

- `AWM_KEEP_MEMORY_HIGH`, `AWM_KEEP_MEMORY_MAX`: the unit's `MemoryHigh` and
  `MemoryMax`, for example `2G` and `3G`. Leave them unset for no cap. A
  change applies the next time cch launches the unit.
- `AWM_KEEP=0`: cch launches nothing on this node.
- `AWM_KEEP_CLAUDE`: the `claude` binary, when it is not the default.
- `AWM_KEEP_MEMORY_LOW`: the fraction of memory that counts as low. Default
  `0.12`.

## Why the daemon runs where it runs

The daemon runs in the transient unit `awm-claude-daemon.service` of the user's
systemd manager. It is never a child of the gateway or of a terminal.

- **Not a child of the gateway.** `awm deploy` restarts the gateway, and the
  gateway's unit kills its whole control group. A daemon there would take
  every session with it on each deploy.
- **Not a child of a terminal.** Before the keeper, the first `claude agents` in a
  tmux pane started the daemon inside that pane's scope. On 2026-10-20 the
  kernel OOM-killed a 31.9 GB python in that scope. systemd's default
  `OOMPolicy=stop` then stopped the whole scope, the daemon and its sessions
  with it.
- **A transient unit, not a unit file.** A transient unit ends at reboot.
  cch starts with the gateway at boot and launches it again. No file on
  the host can drift from the code.

The unit's properties:

- `KillMode=process`: a stop or a self-upgrade ends the daemon only. Its
  sessions keep running, and the next daemon adopts them.
- `Restart=always`, `RestartSec=10s`, `RestartSteps=6`,
  `RestartMaxDelaySec=10min`, `StartLimitIntervalSec=0`: systemd restarts the
  daemon after any exit, including the clean exit of an upgrade. The delay
  grows from 10 s to 10 min and never stops. A daemon that fails on every
  start retries every 10 min, not in a tight loop.
- `OOMPolicy=continue`: an OOM kill of one session does not stop the unit.
- `OOMScoreAdjust=-200`: under global memory pressure the kernel prefers a
  process outside the unit. The sessions inherit the value.

cch runs `systemctl --user reset-failed` once the daemon has run for 10 min.
systemd's restart counter otherwise grows across self-upgrades, and every
later restart would wait the full 10 min.

**CAUTION** The daemon and every client must agree on `HOME` and
`CLAUDE_CONFIG_DIR`. The socket directory is
`/tmp/cc-daemon-<uid>/<sha256(config dir)[:8]>`. cch passes the unit only
the environment the sessions need (`PATH`, `MAMBA_ROOT_PREFIX`,
`AWM_WORKSPACE`, `CLAUDE_CONFIG_DIR`, `CLAUDE_PTY_ORPHAN_CHECK_MS`). The
gateway's own environment can carry a session's ids, so it is not copied.

## A daemon cch did not start

- **A transient daemon** (started on demand by a `claude` command): cch
  launches its own unit. A service-origin daemon asks the transient one to
  yield over its control socket and adopts its sessions. When the control
  socket does not answer, the new daemon refuses and exits. After 90 s cch
  sends SIGTERM to the transient daemon alone, and its sessions survive.
- **A daemon in another unit** (a hand-written unit file): cch reports it as
  `foreign_unit` and leaves it alone. Retire the old unit in this order:
  `disable`, `stop`, remove the file, `daemon-reload`. cch then launches its
  own unit within 30 s.

**WARNING** Stop the old unit before you remove its file. After a
`daemon-reload` without the file, systemd forgets `KillMode=process`, and the
stop kills every session in the unit.

## Stopping the daemon on purpose

`daemon_hold reason=<why> seconds=<n>` stops the daemon, and cch does not
restart it until `daemon_release` or until the hold expires. A hold lasts 600 s
by default and 1200 s at most. A hold whose holder died therefore never leaves a
node without its daemon.

**WARNING** A session cannot outlive its daemon by much. Claude Code's pty host
ends its session once it has had no daemon and no client for 30 ×
`CLAUDE_PTY_ORPHAN_CHECK_MS` (60 s by default). cch sets the check to 40 s,
for a 20-minute grace that outlasts the 10-minute restart backoff. It sets it on
the unit and in the user manager's environment, so a unit launched before it
takes it on its next restart. A pty host takes the value when it starts, so a
session started before then still dies after 60 s. Social's keeper resumes the
kept seats afterwards. Nothing resumes the others.

## Bringing a daemon back from another node

Any agent brings a node's daemon back with
`harness-cc daemon_ensure peer=<node>`, with no ssh. When that answers
`action: held`, `harness-cc daemon_release peer=<node>` ends the hold. When the
verb does not answer at all, cch itself is down: run
`awm services restart cch` on that node.

## Low memory

When available memory stays below `AWM_KEEP_MEMORY_LOW` for 2 min, cch
sends one notice to the operator's DM (`config.operator_dm()`, through social,
which `AWM_SOCIAL_PEER` locates). The notice names the three processes of its
user with the most resident memory, and the session that owns each. Each of
those sessions also gets a line in its own prompt. Both repeat at most once an
hour.

The warning exists because the kernel picks the victim when memory runs out.
On 2026-10-19 a 31.9 GB python ran altair out, and the kill took every
session down. The daemon unit's `OOMScoreAdjust=-200` steers the kernel away
from the sessions, but the warning is the only step that comes before the
kill.

**CAUTION** cch sees only its own user's processes. On a node whose
gateway runs as an app user (deneb), a hog of another user goes unnamed. A
node that runs no social and sets no `AWM_SOCIAL_PEER` only logs the notice.
