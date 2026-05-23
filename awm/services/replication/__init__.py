"""Eventual DB replication across AWM peers via cr-sqlite.

Replicable tables (marked CRR): rooms, room_participants, room_posts, scopes,
session_logs, messages, artifacts, peers, discord_operators. Locks and
agent_sessions stay local-only.

Schema setup happens in :mod:`awm.services.replication.schema` (loads the
cr-sqlite extension, registers CRRs idempotently). The pull-based sync loop
lives in :mod:`awm.services.replication.sync`; the federated routes in
:mod:`awm.services.replication.endpoint`.
"""

from awm.services.replication.schema import (
    CRSQLITE_EXT_PATH,
    CRR_TABLES,
    crsqlite_available,
    finalize,
    load_extension,
    register_all_crrs,
)

__all__ = [
    "CRSQLITE_EXT_PATH",
    "CRR_TABLES",
    "crsqlite_available",
    "finalize",
    "load_extension",
    "register_all_crrs",
]
