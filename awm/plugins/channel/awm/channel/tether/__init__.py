"""The machine kind's transport: the tether binaries and the stream they report.

An operator session invites itself onto an owner's machine with a code the
owner types at their own keyboard. The session crosses a relay neither side has
to be reachable from, runs in front of the owner, and either side can cut it.
Every byte of a session is handled by the tether binaries, which are built and
released from the ``phy0x1a79ed/tether`` repository and fetched here by the
version ``RELEASE`` pins. This package supervises them and reads their events.
"""
