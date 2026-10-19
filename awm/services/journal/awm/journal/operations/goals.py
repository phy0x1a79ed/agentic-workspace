"""Goal operation definitions for the gateway manifest.

Goals ride the scope post log as ``kind='goal'`` — see :mod:`awm.journal.goals`
for the model. Four verbs, published as ``journal_goal_<verb>`` and reached as
``journal(verb="goal_<verb>", …)``:

  - ``goal_set``     — record a goal, or a revision that supersedes one.
  - ``goal_retire``  — append a tombstone retiring a goal.
  - ``goal_read``    — everything in force at a scope, broad to specific,
                       rendered with the comparison instruction attached.
  - ``goal_history`` — the full chain of restatements behind one goal.

There is no delete verb and no edit-in-place. The post log has no mutation path
and goals do not add one.
"""

from awm.journal import channel, goals, private


GOAL_MANIFEST_FUNCTIONS = [
    {
        "name": "goal_set",
        "tool": "journal_goal_set",
        "effect": "write",
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
        # objective is declared LAST for the same reason journal_post's body is:
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
        "name": "goal_retire",
        "tool": "journal_goal_retire",
        "effect": "write",
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
        "name": "goal_read",
        "tool": "journal_goal_read",
        "effect": "read",
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
        "name": "goal_history",
        "tool": "journal_goal_history",
        "effect": "read",
        "description": (
            "The full chain of restatements a goal belongs to, oldest first — "
            "how the objective drifted, and the tombstone if it was retired."
        ),
        "params": [
            {"name": "goal_id", "type": "string", "required": True},
        ],
    },
]


def _refuse_hidden(goal_id: str | None, as_: str | None, verb: str) -> None:
    """Answer a peer naming a goal on a private channel as for a goal that does not exist."""
    if not goal_id:
        return
    target = goals.get_goal(goal_id)
    if target is not None and private.hides(as_, target.project, target.scope):
        raise goals.GoalError(f"no goal {goal_id!r}{verb}")


def _handle_goal_set(args: dict, as_: str | None = None) -> dict:
    _refuse_hidden(args.get("supersedes"), as_, " to supersede")
    if args.get("level") and private.hides(
            as_, *goals.owner_for_level(args["level"], args.get("project"), args.get("scope"))):
        raise ValueError(f"no scope {args.get('project')}/{args.get('scope')}")
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
        origin=channel.edge_origin(as_),
    )
    return {"goal": goal.to_dict()}


def _handle_goal_retire(args: dict, as_: str | None = None) -> dict:
    _refuse_hidden(args["goal_id"], as_, " to retire")
    tombstone = goals.retire_goal(
        args["goal_id"], author=args["author"], reason=args.get("reason") or "",
        origin=channel.edge_origin(as_),
    )
    return {"tombstone": tombstone.to_dict(), "retired": args["goal_id"]}


def _refuse_foreign(as_: str | None) -> None:
    """Goals are not journals: the verbs carry no category, so the foreign gate never lets a foreign caller reach them. This refusal backs the gate."""
    if channel.is_foreign(as_):
        raise PermissionError("goals are not readable by foreign peers")


def _handle_goal_read(args: dict, as_: str | None = None) -> dict:
    _refuse_foreign(as_)
    levels = args.get("levels")
    if isinstance(levels, str):  # a harness may hand a comma list through
        levels = [x.strip() for x in levels.split(",") if x.strip()]
    found = [g for g in goals.read_goals(args.get("project"), args.get("scope"), levels=levels)
             if not private.hides(as_, g.project, g.scope)]
    rendered = goals.render(found, args.get("project"), args.get("scope"))
    return {"goals": [g.to_dict() for g in found],
            "total": len(found), "rendered": rendered}


def _handle_goal_history(args: dict, as_: str | None = None) -> dict:
    _refuse_foreign(as_)
    _refuse_hidden(args["goal_id"], as_, "")
    chain = goals.history(args["goal_id"])
    return {"chain": [g.to_dict() for g in chain], "total": len(chain)}


GOAL_HANDLERS = {
    "goal_set": _handle_goal_set,
    "goal_retire": _handle_goal_retire,
    "goal_read": _handle_goal_read,
    "goal_history": _handle_goal_history,
}
