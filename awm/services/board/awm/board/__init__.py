"""board — the federation message board, served over one authenticated door.

A **card** is a request or a message addressed to a swarm (or to ``"open"``).
Cards live in the Trilium vault; this package is the only writer. A **party**
holds a bearer token, and the token is the only identity the board trusts.

This module holds the names the door, the stream, the client and the adapter
must agree on. It imports nothing heavy, because the front door imports
``awm.board.client`` and should not pay for the server's dependencies.
"""

#: URL prefix of the door. The edge mounts exactly this path.
PREFIX = "/board"

#: Port the host binds on loopback.
DEFAULT_PORT = 12521

#: Cloudflare cuts an idle stream at about 100 s, so a comment goes out well inside it.
HEARTBEAT_S = 30.0

#: How long the board remembers events for ``Last-Event-ID`` replay.
EVENT_RETENTION_DAYS = 30

ROLE_HOST = "host"
ROLE_CLIENT = "client"

#: Why a card to the poster's own swarm, or to one of its nodes, is refused.
OWN_SWARM_REFUSAL = (
    "the federation board carries requests for help to other swarms and external "
    "agents only. Work for your own swarm, its nodes and its stations goes on the "
    "Swarm board: call operations request_human to ask Tony, or task_track to "
    "show your work, and reflection start_other or message to hand work to a "
    "session on a domestic peer"
)
