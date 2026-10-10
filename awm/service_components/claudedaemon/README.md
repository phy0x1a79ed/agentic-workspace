# `awm.claudedaemon`

## Purpose & Contents

Everything awm knows about Claude Code's background sessions. It reads the
records the daemon and its sessions write, launches a session, checks whether a
directory is trusted, says which restricted mode a session runs in, and types
into a session over the daemon's private PTY socket.

This file says what each part is for and what deliberately stayed outside. The
PTY wire protocol is documented in `awm/claudedaemon/pty.py`, and each module's
docstring holds the rest.

## What is here

- `roster` reads the daemon roster, each job's own record and each running
  REPL's session record. They disagree, so the module says which one answers
  which question.
- `launch` starts one background session. It refuses unless a daemon is already
  running, because a session inherits the control group and environment of
  whichever process started the daemon.
- `trust` answers whether `/cd` into a directory, or `claude --bg` inside it,
  would stop at a trust dialog. The two rules differ. A start inside a git
  repository needs the repository's own entry, which is the `.bare` directory for
  a scope worktree.
- `sessionmode` answers `mode_of(pid)`, the restricted mode a session was started
  in. It reads `cx start`'s lineage records from disk, so the gateway gates a
  caller with no call into the `cx` process. The answer is a mode string, `None`
  or `"unknown"`, and a reader treats `"unknown"` as the most restricted mode.
- `pty`, `lane` and `job` type into a session. `DaemonLane` addresses one
  session's PTY. `open_lane` opens it for a single write. `connect` hands back a
  connection that stays open, for a caller that has to read and press keys
  several times. `job.send_line` addresses a session by its job id.

The component owns no state, supervises nothing, and has no third-party
dependencies. It is imported source with no `install.sh`, installed by name from
`awm/gateway/install.sh`. The consumers are `cx`, `reflection`, `transcripts`,
the front door (`representative`) and the gateway.

## What stayed with the callers

Deciding *which* session a caller may reach, and whether it may start one.
`awm.reflection.session_target` resolves a caller to exactly one session by
process ancestry and refuses anything it cannot identify, which is that service's
whole guarantee. `cx` holds the id of a session it created itself and addresses
it straight from the roster. `cx` also decides who may start a session and in
what mode. `launch` takes a directory, a name, flags and a prompt, and starts
what it is told.

**CAUTION:** producing a `DaemonLane` is the security decision. The handshake
here checks that the socket answers for the REPL it was told to expect — the
daemon hands out recycled socket paths that greet healthily before any job
claims them — but nothing here decides whether the caller was entitled to that
session in the first place.

**CAUTION:** `sessionmode` resolves the lineage directory from the same
environment variables `cx` writes with. A reader that located it differently
would find no record for any session and answer "not restricted".

## What this cannot promise

Injection confirms that the frames reached a socket whose host identified itself
correctly, and that the host did not reject them. It does not confirm that the
text is on screen, and it does not confirm that the command ran. Both callers
read the session's own state record afterwards instead.
