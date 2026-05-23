"""Application-layer leader election for the AWM control panel.

Only the lowest-priority reachable peer mounts ``/ui/*`` + ``/auth/bootstrap``
and holds the Discord gateway connection. Lower-priority peers stand by with
federation (``/peer/*``) always on, returning 503+Location for UI hits so
operators land on the active peer.

See ``state.py`` for the singleton, ``poll.py`` for the probe loop,
``discord.py`` for the bot lifecycle hookup.
"""

from awm.services.leadership.state import (
    State,
    get_state,
    on_promote,
    on_demote,
)

__all__ = ["State", "get_state", "on_promote", "on_demote"]
