"""Goal operation definitions for the gateway manifest.

Goals ride the scope post log as ``kind='goal'`` — see :mod:`awm.scopes.goals`
for the model. Four verbs, named onto the existing ``scope`` domain — the MCP
projection splits a tool name on its first underscore, so ``scope_goal_read``
reaches the model as ``scope(verb="goal_read", …)`` rather than minting a
``goal`` domain of its own:

  - ``scope_goal_set``     — record a goal, or a revision that supersedes one.
  - ``scope_goal_retire``  — append a tombstone retiring a goal.
  - ``scope_goal_read``    — everything in force at a scope, broad to specific,
                             rendered with the comparison instruction attached.
  - ``scope_goal_history`` — the full chain of restatements behind one goal.

There is no delete verb and no edit-in-place. The post log has no mutation path
and goals do not add one.
"""

from awm.scopes import goals


GOAL_MANIFEST_FUNCTIONS = [
    {
        "name": "scope_goal_set",
        "tool": "scope_goal_set",
        "description": (
            "Record what the user is actually after, at the altitude they said "
            "it, in their own words. level ∈ workspace|project|scope decides the "
            "channel it lands on (workspace = the standing frame, scope = the "
            "current campaign). Pass supersedes=<goal id> to revise: that writes "
            "a NEW record and keeps the old one — there is no edit in place — and "
            "the revision stays on the superseded goal's level unless you pass "
            "level explicitly. A revision INHERITS every disposition field it "
            "does not mention, so restating just the objective never silently "
            "drops the stop line; pass an empty string to clear one. A partial "
            "goal is recorded rather than refused; "
            "the response lists which disposition fields are still missing. Pass "
            "all structured params BEFORE the free-text objective, which must be "
            "the LAST argument."
        ),
        # objective is declared LAST for the same reason scope_post's body is:
        # a long free-text value can bleed past its closing tag and swallow any
        # parameter serialized after it. Do not move it up.
        "params": [
            {"name": "author", "type": "string", "required": True,
             "description": "'user:<name>' — a goal is the user's statement."},
            {"name": "level", "type": "string", "required": False,
             "description": "workspace | project | scope. Required unless supersedes is given."},
            {"name": "project", "type": "string", "required": False},
            {"name": "scope", "type": "string", "required": False},
            {"name": "supersedes", "type": "string", "required": False,
             "description": "Goal id this one replaces. The old record survives."},
            {"name": "fallback", "type": "string", "required": False,
             "description": "The fallback ladder — what to do when the ideal is unreachable."},
            {"name": "stop_line", "type": "string", "required": False,
             "description": "What separates incomplete (acceptable, say so) from wrong (stop)."},
            {"name": "noise", "type": "string", "required": False,
             "description": "What does not matter — caveat classes not worth raising."},
            {"name": "objective", "type": "string", "required": True,
             "description": "The terminal objective in the user's own words. Pass "
                            "this argument LAST — emit every other parameter before it."},
        ],
    },
    {
        "name": "scope_goal_retire",
        "tool": "scope_goal_retire",
        "description": (
            "Retire a goal by appending a tombstone. Nothing is deleted — the "
            "goal drops out of the goal_read set but stays readable via "
            "goal_history."
        ),
        "params": [
            {"name": "goal_id", "type": "string", "required": True},
            {"name": "author", "type": "string", "required": True},
            {"name": "reason", "type": "string", "required": False},
        ],
    },
    {
        "name": "scope_goal_read",
        "tool": "scope_goal_read",
        "description": (
            "Every goal in force at a scope — the workspace frame, the project's "
            "goals and the scope's own, unioned and ordered broad to specific. "
            "Superseded and retired records are excluded. Nothing is ranked or "
            "truncated. Returns a rendered block to read directly plus the "
            "structured records. Read this at the start of work on a scope, and "
            "again before proposing a deliverable."
        ),
        "params": [
            {"name": "project", "type": "string", "required": False},
            {"name": "scope", "type": "string", "required": False},
            {"name": "levels", "type": "array", "required": False,
             "description": "Narrow to a subset of workspace|project|scope. Default: all three."},
        ],
    },
    {
        "name": "scope_goal_history",
        "tool": "scope_goal_history",
        "description": (
            "The full chain of restatements a goal belongs to, oldest first — "
            "how the objective drifted, and the tombstone if it was retired."
        ),
        "params": [
            {"name": "goal_id", "type": "string", "required": True},
        ],
    },
]


def _handle_goal_set(args: dict) -> dict:
    goal = goals.set_goal(
        objective=args["objective"],
        author=args["author"],
        level=args.get("level"),
        project=args.get("project"),
        scope=args.get("scope"),
        # Absent means "carry it forward from the superseded record"; only an
        # explicit "" clears a field. Do not collapse these to "".
        fallback=args.get("fallback"),
        stop_line=args.get("stop_line"),
        noise=args.get("noise"),
        supersedes=args.get("supersedes"),
    )
    return {"goal": goal.to_dict()}


def _handle_goal_retire(args: dict) -> dict:
    tombstone = goals.retire_goal(
        args["goal_id"], author=args["author"], reason=args.get("reason") or "",
    )
    return {"tombstone": tombstone.to_dict(), "retired": args["goal_id"]}


def _handle_goal_read(args: dict) -> dict:
    levels = args.get("levels")
    if isinstance(levels, str):  # a harness may hand a comma list through
        levels = [x.strip() for x in levels.split(",") if x.strip()]
    found, rendered = goals.read_rendered(
        args.get("project"), args.get("scope"), levels=levels,
    )
    return {"goals": [g.to_dict() for g in found],
            "total": len(found), "rendered": rendered}


def _handle_goal_history(args: dict) -> dict:
    chain = goals.history(args["goal_id"])
    return {"chain": [g.to_dict() for g in chain], "total": len(chain)}


GOAL_HANDLERS = {
    "scope_goal_set": _handle_goal_set,
    "scope_goal_retire": _handle_goal_retire,
    "scope_goal_read": _handle_goal_read,
    "scope_goal_history": _handle_goal_history,
}
