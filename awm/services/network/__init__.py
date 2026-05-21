"""Cross-machine federation primitives (peers, routing, fanout).

This cluster handles awm-to-awm communication. Local-only workspace state
lives in awm/services/*.py (the existing layout) — this directory is the
boundary where requests can leave the host.
"""
