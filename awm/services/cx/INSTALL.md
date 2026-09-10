# Installing the `cx` service

## Purpose & Contents

`cx` is the warm start for Claude Code. The service keeps one background session
idling. The `cx` command claims it and attaches, so a launch paints an
already-started renderer instead of booting one.

This file holds what installing and operating the service needs, and the
reasoning that reading the code does not give: why a session is moved exactly
once, why identity comes from one file and not another, why the service refuses
to seed on a node with no daemon, and what the pool deliberately does not do.

## Install

Both halves go on a node together. A node with the service and no command gains
nothing. A node with the command and no service launches cold, correctly and
silently.

    bash install.sh                          # the service, into the awm env
    ln -sf "$PWD/bin/cx" ~/.local/bin/cx     # the command, on PATH
    awm services enable cx                   # this service is profile-gated off

`install.sh` editable-installs the `gatewayclient` and `claudedaemon` components
and this service into the `awm` env. Override the env with `AWM_ENV=<name>`.

`service.toml` gates discovery on the `cx` profile, so the service does not
start on a node that has not asked for it. An explicit `awm services enable cx`
overrides the gate and is the intended way to turn it on.

## Operate

    awm cx status            # what is held, and why anything is unavailable
    awm cx remove            # what may be deleted, and why. Deletes nothing
    awm cx remove --apply    # delete it
    awm cx seed              # start one session now, or say why it is refused

`status` derives everything from the live session records. There is no stored
pointer to "the current session", because a stored pointer is a second thing
that has to agree with the records and it is the half that goes stale.

Every knob is an environment variable on the service process. `AWM_CX_WANT=0`
stops the pool without stopping the service. `AWM_CX_LOOP=0` stops the reconcile
loop alone. `AWM_CX_ROTATE_AGE_S` sets the age at which a session is replaced.
`AWM_CX_PREFIX`, `AWM_CX_SEED_DIR`, `AWM_CX_MODEL`, `AWM_CX_EFFORT` and
`AWM_CX_CLAUDE` shape what is seeded. See `awm/cx/config.py`.

## Why the shape is what it is

### A session is moved exactly once

A session is seeded in a neutral directory and moved to the caller's directory
by typing `/cd`. Claude Code reloads project settings, project MCP servers,
project skills and the destination's CLAUDE.md on that move, so a moved session
is equivalent to one launched there. That is what makes the pool
directory-agnostic, and why the first `cx` in a project is as fast as the
hundredth.

**CAUTION:** CLAUDE.md accumulates across moves. The destination's file is added
and the origin's is not removed. The seed directory must therefore hold no
CLAUDE.md, and a session is moved once and only once, on its way to its user.
`precondition()` refuses to seed beside a CLAUDE.md and the claim refuses a
session that already carries an origin directory.

### Identity comes from the state record, never the roster

Two files describe a session and they disagree. The daemon roster carries
liveness, the PTY lane, the CLI version and the start time. Its recorded name is
what the session was called when it was created, and it never changes. The
session's own record at `~/.claude/jobs/<short>/state.json` is the one that
follows a rename.

On the box this was built against, a session whose roster entry still read
`<warm zorilla>` had been renamed to "remote shell" and talked to for 27k
tokens. Reading the name from the roster would have handed that conversation to
the next terminal that ran `cx`.

Usedness is read from the same record and never from the presence of a
transcript. The session id the roster records drifts on respawn, `/cd` creates a
transcript on arrival, and the archival job deletes transcripts after a week.
Any of the three would hand a live conversation to a second terminal.

### Seeding refuses when no daemon is running

Every background Claude Code session on a node is a child of the first daemon
started after the reboot, and inherits that daemon's control group **and its
environment**. If the gateway were ever the process that started it, then
`systemctl restart awm` would kill every session the user is working in, and
every session on the box would come up with an environment that has no
`~/.local/bin` on its PATH.

So the service reads the roster's supervisor pid, checks it against `/proc`, and
refuses to seed when no daemon is running. No daemon means nobody is using
Claude Code on that node, and a cold first launch is the right answer there.

The transient systemd unit covers the residual race where the daemon dies
between the check and the launch. It carries `KillMode=process`. Without that,
tearing the unit down kills its whole control group, which in that race holds
the fresh daemon and the session it was launching.

### A trusted directory is checked before the move

`/cd` into a directory the session has not worked in before opens a trust dialog
whose default answer is "No, stay put". A session sitting on that dialog has not
moved and is not idle, and the next claim's keystrokes answer the dialog instead
of moving. One untrusted directory therefore cost every claim after it.

The pool asks first. Trust is recorded per directory in `~/.claude.json` and
inherited from any trusted ancestor. An untrusted target is refused in
milliseconds and the caller launches cold, where the user answers the trust
prompt themselves.

### Nothing is collected once somebody has it

A session that has been renamed, prompted, or claimed and is still alive is
never taken and never deleted. Removal collects only what the pool made and
nobody took: a corpse, or a session that has never been moved and is stale by
version or by age.

**CAUTION:** a session claimed and then abandoned without being prompted stays
alive indefinitely. The daemon's idle retirement does not collect it — one such
session was observed alive after sixteen hours. Nothing here collects it either,
by design: once a session has been handed to a terminal it belongs to whoever
took it. Delete one by hand with `claude rm <short>` when you know it is yours.

### The command leaves nothing behind

`bin/cx` ends in `exec` on both branches, so once Claude Code is running no
process of this tooling remains. That is what keeps Ctrl+Z, Ctrl+C and the left
arrow doing exactly what `claude attach --help` says they do.

**CAUTION:** Ctrl+C in an attached session is a detach, and the client answers a
detach by replacing itself with `claude agents`. That picker does not return on
its own. Ctrl+Z is the documented way out. An earlier implementation ran a
watcher beside the attach to signal the picker away, which cost the left-arrow
route to the agent view; the vendor's behaviour is kept instead.

### The claim rides the ordinary gateway

The gateway is plain HTTP on loopback with no auth, and a round trip to a
service function measures 22ms. So the claim is a manifest verb like any other
and `bin/cx` speaks to it with bash's `/dev/tcp`, forking nothing. The injection
then happens inside a service that is already warm, which keeps a Python
interpreter start off the path of every launch.

The timeouts have to agree with each other. `MOVE_TIMEOUT_S` in `awm/cx/claim.py`
is how long a move may take, the manifest's `claim` timeout must exceed it, and
`CX_WAIT` in `bin/cx` must exceed that. A budget below the one beneath it aborts
a claim that was about to succeed, and the session is spent for nothing. A test
asserts the ladder.

## Run

You never invoke the service by hand in normal operation. The gateway discovers
this folder, starts it with `bash run.sh`, and injects `AWM_HUB_URL`,
`AWM_SERVICE_NAME` and `AWM_SERVICE_ID`. No auth.

Only one copy may own the pool. The reconcile loop takes a non-blocking
exclusive lock and skips the tick if it cannot get it, which covers an
`awm dev shadow` overlay, a dev sandbox running its own copy against the same
home directory, a stray manual run, and a predecessor that has not finished
dying. `status` reports whether the answering process holds it.
