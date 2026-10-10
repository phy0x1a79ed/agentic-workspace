# Installing the `cx` service

## Purpose & Contents

`cx` runs Claude Code sessions on a node. It does two jobs. It starts, lists and
stops named background sessions for other agents. It also keeps one background
session idling, and the `cx` command claims that session and attaches, so a
launch paints an already-started renderer instead of booting one.

This file holds what installing and operating the service needs, and the
reasoning that reading the code does not give: who may start a session and what
a restricted caller gets, where a session's mode lives, why a session is moved
exactly once, which of two files answers which question about a session, how a
session comes by its name, why the service refuses to seed or start on a node
with no daemon, which trust rule applies to a move and to a start, and what the
pool deliberately does not do. The verb list is in `awm cx --help`.

## Install

Both halves go on a node together. A node with the service and no command gains
nothing. A node with the command and no service launches cold, correctly and
silently.

    scripts/deploy-hook.sh                   # the naming hook, on every node
    bash install.sh                          # the service, into the awm env
    ln -sf "$PWD/bin/cx" ~/.local/bin/cx     # the command, on PATH
    awm services enable cx                   # this service is profile-gated off

**CAUTION:** the hook goes first, and it goes to every node, not just this one.
A node running the service without it names every session it hands out
`claimed <noun>` and never takes that name off, which reads as deliberate and is
worse than the name it replaced. A node with the hook and no service has nothing
named `claimed ` to act on. `scripts/deploy-hook.sh --check` reports drift and
writes nothing; a second run is a no-op.

`install.sh` editable-installs the `gatewayclient` and `claudedaemon` components
and this service into the `awm` env. Override the env with `AWM_ENV=<name>`.

`service.toml` gates discovery on the `cx` profile, so the service does not
start on a node that has not asked for it. An explicit `awm services enable cx`
overrides the gate and is the intended way to turn it on. The same file sets
`tier = "core"`, so the `cx` domain is on the default MCP list wherever the
service runs.

A station (`AWM_NODE_ROLE=station`) runs no sessions. `start` refuses there, and
the gateway logs a warning at boot if a station enables this service.

## Operate

    awm cx list              # every session on this node, and the pool summary
    awm cx start --help      # start a named session in a scope's worktree
    awm cx stop --help       # stop a session that start created
    awm cx remove            # what the pool may delete, and why. Deletes nothing
    awm cx remove --apply    # delete it
    awm cx seed              # start one warm session now, or say why it is refused

`list` derives the pool summary from the live session records. There is no
stored pointer to "the current session", because a stored pointer is a second
thing that has to agree with the records and it is the half that goes stale.

Every knob is an environment variable on the service process. `AWM_CX_WANT=0`
stops the pool without stopping the service. `AWM_CX_LOOP=0` stops the reconcile
loop alone. `AWM_CX_ROTATE_AGE_S` sets the age at which a session is replaced.
`AWM_CX_PREFIX`, `AWM_CX_SEED_DIR`, `AWM_CX_MODEL`, `AWM_CX_EFFORT` and
`AWM_CX_CLAUDE` shape what is seeded. `AWM_CX_PROJECTS` sets the root of the
scope worktrees that `start` uses, and `AWM_CX_MCP_SOURCE` names the MCP config a
strict session copies. See `awm/cx/config.py`.

`AWM_CX_CLAIMED_PREFIX` is the one knob the hook reads too, from its own copy of
the default. Change it on the service and the hook stops recognising the
sessions the service renames. A test asserts the two agree.

## Why the shape is what it is

### Sessions started for other agents

`start` creates a named background session in a scope's worktree and returns its
job id and the `claude attach` command. It creates the worktree through the
`scopes` service when the caller may and the worktree is absent. A session
defaults to skip-permissions, `sonnet[1m]` and medium effort. The model is a
`--model` flag, because the flag wins over `ANTHROPIC_MODEL` where the two
disagree.

`start` refuses when any of these holds:

1. The node is a station.
2. The caller is a peer that is not a verified, domestic node.
3. No Claude Code daemon runs.
4. The claude workspace-trust check fails (see *A directory must be trusted*).
5. A live session already holds the name.

`stop` ends only a session that `start` created, and it keeps the conversation.
It finds such a session by its lineage record. A session that cx started may
stop only the jobs it started itself. An operator, a service or a session that
cx did not start may stop any job `start` created.

A lineage record is one JSON file per started session, under `starts/` in cx's
state directory. It holds the name, scope, parent, caller and mode. `parent`
comes from the pid the gateway stamps on the call, never from an argument the
model supplies. A pending record goes to disk before the launch, so a launch
that outlives its timeout still has a declared mode. The reconcile loop adopts
the late job, or drops the record after five minutes.

#### A caller in a restricted mode starts a delegate

The representative and the secretary run in restricted modes (`awm.config.modes`).
A card must not talk either into starting a session stronger than itself, so cx
ignores what such a caller asks for. It accepts `project`, `scope`, `prompt`,
`name`, `effort` and a model from an allowlist. It refuses `permission`, `mode`,
`remote_control` and the tool and confinement arguments. It starts a `delegate`
with a fixed policy:

- Permission `dontAsk`, so a session nobody watches denies a prompt at once and
  cannot stall.
- A fixed built-in tool list with no Bash, and `SendMessage` and `ListAgents`
  disallowed, so a card cannot reach another session through it.
- `--restricted`, which confines the file tools to the worktree and ignores user
  settings.
- A strict MCP config that holds only the awm server, copied from the workspace
  `.mcp.json`.

The scope must already exist for such a caller. The names `representative` and
`secretary` and the modes `representative`, `secretary` and `delegate` are
reserved. A session that asks for one is refused. Only a caller with no session
pid (the front door service or an operator) may use them. A caller whose mode cannot be read starts
nothing.

The arguments `allowed_tools`, `tools`, `restricted` and `strict_mcp` exist for
that front door and for operators. They map to `claude --allowedTools`,
`--tools`, `--restricted` and `--strict-mcp-config`.

#### Where a mode lives

The gateway gates a calling session by its mode, and cx gates what that session
may start or stop by it. Both read the answer from `awm.claudedaemon.sessionmode`,
which reads the lineage records and Claude Code's own session records from disk.
The gateway therefore needs no call into this process. The answer is a mode
string, `None` for a session that cx did not start, or `unknown`. A caller reads
`unknown` as the most restricted mode.

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

### Two files describe a session and they answer different questions

The daemon roster carries liveness, the PTY lane, the CLI version and the start
time. Its recorded name is what the session was called when it was created, and
it never changes. The session's own record at `~/.claude/jobs/<short>/state.json`
is the one that follows a rename.

*Who does this session belong to now* is the record's question. On the box this
was built against, a session whose roster entry still read `<warm zorilla>` had
been renamed to "remote shell" and talked to for 27k tokens. Reading the name
from the roster would have handed that conversation to the next terminal that
ran `cx`.

*Did the pool make this session* is the roster's question, and the pool has to
ask it because it renames a session itself the moment a terminal takes one. A
predicate reading the current name would lose sight of every session the pool
ever gave away, and each of them would idle until the daemon retired it.

So `is_ours` reads the record and gates handing a session out. `was_ours` reads
the roster and gates collecting one. Everything that protects a session
somebody is using sits between them, on the widened side.

Usedness is read from the same record and never from the presence of a
transcript. The session id the roster records drifts on respawn, `/cd` creates a
transcript on arrival, and the archival job deletes transcripts after a week.
Any of the three would hand a live conversation to a second terminal.

### A session names itself once somebody uses it

Three names, in order. The pool seeds a spare as `<warm serval>`, so it reads as
furniture in `claude agents` and nobody deletes it thinking it is abandoned work.
The claim renames it to `claimed serval` by typing `/rename`, which is what stops
it advertising itself as a spare — that one rename is the whole fix for a picker
that showed two spares where the pool held one. Then the first real prompt takes
the name away entirely and Claude Code titles the session from the request.

Claude Code has done that last part all along. A side query turns the user's
request into a two-to-four word label and writes it with `nameSource: "auto"`.
It runs when the job's record has **no name** and **an intent**, and a pool
session fails both: the pool names it at launch, and the intent is captured once
at dispatch, from a prompt that a session launched empty never receives. So the
`UserPromptSubmit` hook supplies both — it drops the name and writes the prompt
in as the intent — and the classifier titles the session at the end of that turn.

**CAUTION:** the intent has to be non-empty, not merely present. Every later
write merges that field forward with `??`, so an empty string survives the whole
session and the namer never fires. This is why clearing the name alone does
nothing, which is worth knowing before anyone simplifies the hook.

A slash command is not a first prompt, and nothing here had to be taught that.
Claude Code's own intent capture skips meta messages and slash-command wrappers,
which is why the `/cd` the claim types has never titled anything. The hook skips
them too, so the two agree if that ever changes.

**CAUTION:** `/rename` rewrites the session's respawn flags and the automatic
title does not. A session that respawns comes back under whatever `/rename` last
put there, which is `claimed <noun>`. The next prompt fixes it.

### Seeding and starting refuse when no daemon is running

Every background Claude Code session on a node is a child of the first daemon
started after the reboot, and inherits that daemon's control group **and its
environment**. If the gateway were ever the process that started it, then
`systemctl restart awm` would kill every session the user is working in, and
every session on the box would come up with an environment that has no
`~/.local/bin` on its PATH.

So the service reads the roster's supervisor pid, checks it against `/proc`, and
refuses to seed or start when no daemon is running. No daemon means nobody is
using Claude Code on that node, and a cold first launch is the right answer
there.

The transient systemd unit covers the residual race where the daemon dies
between the check and the launch. It carries `KillMode=process`. Without that,
tearing the unit down kills its whole control group, which in that race holds
the fresh daemon and the session it was launching.

### A directory must be trusted before a move or a start

`/cd` into a directory the session has not worked in before opens a trust dialog
whose default answer is "No, stay put". A session sitting on that dialog has not
moved and is not idle, and the next claim's keystrokes answer the dialog instead
of moving. One untrusted directory therefore cost every claim after it.

The pool asks first. Trust is recorded per directory in `~/.claude.json`. For a
move, a trusted ancestor counts. An untrusted target is refused in milliseconds
and the caller launches cold, where the user answers the trust prompt
themselves.

`start` applies a stricter rule, because `claude --bg` does. Inside a git
repository the repository's own trust entry is required, and a trusted parent
such as the home directory does not count. A scope worktree belongs to a bare
clone, so the entry is the project's `.bare` directory. A start that fails the
rule would not error. The daemon refuses with "Workspace not trusted" and the
caller waits out its launch timeout, so cx checks first and names the directory.
Both rules live in `awm.claudedaemon.trust`.

### Almost nothing is collected once somebody has it

A session that has been prompted, or that a terminal is attached to, is never
taken and never deleted. Removal collects a corpse, a session that has never
been moved and is stale by version or by age, and one other case.

That case is the leak. A session claimed and then abandoned without ever being
prompted used to stay alive indefinitely — one was observed alive after sixteen
hours — because the rule protecting a live conversation refused every live
session that had been moved, and a session nobody spoke to has no conversation
to protect. It is now collected once it outlives `rotate_age_s`.

**CAUTION:** that is the one arm here that can delete a session a person is
looking at, and attachment is the only thing standing in front of it. Nothing
records attachment. The daemon multiplexes every attach over its single control
socket, so a session's own PTY and rendezvous sockets carry exactly the same two
connections whether a terminal is on them or not, and the state record says
nothing either. What is left is the attaching process: `cx` reaches a session by
running `claude attach <short>`, so the short id sits in an argv for as long as
that terminal is open. A terminal that arrived some other way is invisible, and
an unreadable `/proc` makes every session look unattached — which is why this is
the last guard before a deletion and never the only one. Age, zero tokens and an
empty intent all have to agree first.

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
dying. The `list` pool summary reports whether the answering process holds it.
