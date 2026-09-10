# `awm.claudedaemon`

## Purpose & Contents

The Claude Code daemon's private PTY socket protocol. It is how a service types
into a *background* session — the wire format, the handshake that checks whose
REPL is on the far end, and the writer.

This file says what the component is for and what deliberately stayed outside
it. The protocol itself is documented in `awm/claudedaemon/pty.py`.

## What is here

`DaemonLane` addresses one session's PTY. `open_lane` opens it for a single
write. `connect` hands back a connection that stays open, for a caller that has
to read and press keys several times.

The component owns no state, supervises nothing, and has no third-party
dependencies. It is imported source with no `install.sh`, installed by name from
`awm/gateway/install.sh`.

## What stayed in `reflection`

Deciding *which* session a caller may reach. `awm.reflection.session_target`
resolves a caller to exactly one session by process ancestry and refuses
anything it cannot identify, which is that service's whole guarantee.

The two consumers need opposite things from that judgement, which is why it is
not here. `reflection` acts on its own caller and on nobody else. `cx` holds the
id of a session it created itself and addresses it straight from the roster.
Both then speak the same protocol.

**CAUTION:** producing a `DaemonLane` is the security decision. The handshake
here checks that the socket answers for the REPL it was told to expect — the
daemon hands out recycled socket paths that greet healthily before any job
claims them — but nothing here decides whether the caller was entitled to that
session in the first place.

## What this cannot promise

Injection confirms that the frames reached a socket whose host identified itself
correctly, and that the host did not reject them. It does not confirm that the
text is on screen, and it does not confirm that the command ran. Both callers
read the session's own state record afterwards instead.
