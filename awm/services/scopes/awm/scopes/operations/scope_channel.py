"""The federation-wide journal search entry, until the search service replaces it.

Posts, goals and subscribers moved to the journal service; this verb still fans
out over the peer book and asks the local journal over the gateway.
"""

from awm.scopes import archive

SCOPE_CHANNEL_MANIFEST_FUNCTIONS = [
    {
        "name": "scope_archive_search",
        "tool": "scope_archive_search",
        "effect": "read",
        "category": "journals",
        "description": (
            "Search journal entries across the federation: this node's and every "
            "peer's in the peer book, ranked by relevance. Each hit carries "
            "origin_swarm and origin_node. Returns {hits, peers}; peers maps each "
            "peer asked to 'ok' or {error} (unreachable, refused, timed out) — a "
            "failing peer never fails the search. Use scope_fetch to search this "
            "node alone."
        ),
        "params": [
            {"name": "query", "type": "string", "required": True},
            {"name": "project", "type": "string", "required": False},
            {"name": "scope", "type": "string", "required": False},
            {"name": "limit", "type": "integer", "required": False},
            {"name": "peers", "type": "array", "required": False,
             "description": "Peer names to ask. Default: every peer in the book."},
        ],
        "timeout": 30.0,
    },
]

SCOPE_CHANNEL_HANDLERS = {
    "scope_archive_search": archive.archive_search,
}
