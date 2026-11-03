"""The scope brief verbs, ``journal(verb="brief_<verb>")`` (scopes.md R143)."""

from awm.journal import briefs, channel, private

#: scopes.md R143 fixes this text. Change it there first.
WRITE_GUIDE = (
    "The scope brief tells an agent what this scope is for within its project. It "
    "informs. It does not instruct: write no steps, commands or rules. Write at most "
    "two sentences, such as \"for <the work this scope holds>\". Name the scope's role: "
    "what its branch is for, the part of the project it works on, or whom it serves. "
    "When the work lives in a narrower part of the repo than the whole, give that path "
    "relative to the worktree. Do not describe the project, the goal, the plan or what "
    "done looks like. The repo's docs, AGENTS.md, the goal and the journal hold those. "
    "This is not a log. Change the brief only when the scope's role changes. A brief "
    "over 400 characters is refused."
)

_WHERE = [
    {"name": "project", "type": "string", "required": True},
    {"name": "scope", "type": "string", "required": True},
]
_AUTHOR = {"name": "author", "type": "string", "required": False,
           "description": "Who writes it. Defaults to agent:<project>/<scope>."}

BRIEF_MANIFEST_FUNCTIONS = [
    {
        "name": "brief_read",
        "tool": "journal_brief_read",
        "effect": "read",
        "description": ("The scope's brief: what the scope is for. The harness hands it to "
                        "a session when the session enters the scope, so read it only to "
                        "look again. Returns brief null when the scope has none."),
        "params": list(_WHERE),
    },
    {
        "name": "brief_set",
        "tool": "journal_brief_set",
        "effect": "write",
        "description": WRITE_GUIDE + " brief_set replaces the whole brief.",
        "params": [*_WHERE, _AUTHOR,
                   {"name": "text", "type": "string", "required": True,
                    "description": "The whole brief. Pass this argument last."}],
    },
    {
        "name": "brief_edit",
        "tool": "journal_brief_edit",
        "effect": "write",
        "description": WRITE_GUIDE + " brief_edit replaces one passage, old, with new.",
        "params": [*_WHERE, _AUTHOR,
                   {"name": "old", "type": "string", "required": True,
                    "description": "The passage to replace. It must occur exactly once."},
                   {"name": "new", "type": "string", "required": True,
                    "description": "Its replacement. Pass this argument last."}],
    },
]


def _where(args: dict, as_: str | None) -> tuple[str, str]:
    project, scope = args["project"], args["scope"]
    if channel.is_foreign(as_) or private.hides(as_, project, scope):
        raise ValueError(f"no scope {project}/{scope}")
    return project, scope


def _author(args: dict, project: str, scope: str) -> str:
    return args.get("author") or f"agent:{project}/{scope}"


def _handle_brief_read(args: dict, as_: str | None = None) -> dict:
    brief = briefs.read(*_where(args, as_))
    return {"brief": brief.to_dict() if brief else None}


def _handle_brief_set(args: dict, as_: str | None = None) -> dict:
    project, scope = _where(args, as_)
    brief = briefs.set_brief(project, scope, args["text"], author=_author(args, project, scope),
                             origin=channel.edge_origin(as_))
    return {"brief": brief.to_dict()}


def _handle_brief_edit(args: dict, as_: str | None = None) -> dict:
    project, scope = _where(args, as_)
    brief = briefs.edit_brief(project, scope, args["old"], args["new"],
                              author=_author(args, project, scope),
                              origin=channel.edge_origin(as_))
    return {"brief": brief.to_dict()}


BRIEF_HANDLERS = {
    "brief_read": _handle_brief_read,
    "brief_set": _handle_brief_set,
    "brief_edit": _handle_brief_edit,
}
