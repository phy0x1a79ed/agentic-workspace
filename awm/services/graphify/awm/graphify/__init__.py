"""awm.graphify — a queryable knowledge graph of the awm source tree.

Wraps the ``graphify`` CLI (https://github.com/safishamsi/graphify) as an awm
feature service. It indexes the awm tree the service runs in — AST-only, local,
no LLM backend, no API key — into a graph of code nodes/edges, and exposes a thin
``build`` / ``query`` / ``path`` / ``status`` surface so agents can ask
structural questions about awm through the normal MCP/CLI/HTTP catalog.

``hub_adapter`` is the entry point (the gateway runs ``python -m
awm.graphify.hub_adapter`` via run.sh); ``runner`` owns the subprocess +
on-disk layout (graphs live under ``$AWM_DIR/services/graphify/``, never inside
the indexed worktree).
"""
