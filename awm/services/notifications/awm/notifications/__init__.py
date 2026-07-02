"""AWM notifications service — the agent-attention board.

Harness-level producers (a global Claude Code hook, a global OpenCode plugin)
POST normalized lifecycle events to this service's ``report`` verb; it
classifies them into attention items (question / idle / error), auto-resolves
them when the user responds to the agent, and streams deltas to the board page
over the ``feed`` emitter.
"""
