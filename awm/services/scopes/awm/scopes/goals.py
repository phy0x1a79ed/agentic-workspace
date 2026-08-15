"""Goals — what the user is actually after, held on the scope post log.

A goal is a ``scope_posts`` row with ``kind='goal'``. It is not a task and not a
plan: it is the terminal objective at the altitude the user stated it, in the
user's own words, so a later session retrieves it instead of re-eliciting it.

**The level is the channel.** There is no level column; the owner pair says it:

  ``workspace``  → ``('', 'workspace')``
  ``project``    → ``('', 'project:<name>')``
  ``scope``      → ``(<project>, <scope>)``

which is the same non-literal-channel addressing :mod:`awm.scopes.seed` already
maps refs onto. Reading at a scope unions all three and returns them broad to
specific — the general frame first, the current campaign last.

**Editing is supersede, never mutation.** The post log has no UPDATE or DELETE
path anywhere, deliberately, and goals do not add one. A revision is a new goal
naming the record it replaces; a retirement is a tombstone naming the record it
retires. Both are ordinary appends, so the drift history is free and the
append-only invariant the journal depends on is untouched.

The collapse is therefore computed **on read**, over the whole fetched union
rather than per channel — a revision that moves a goal between levels still
retires the old one. Caching it would go stale on the next append.

The read never ranks and never truncates. A stop line rotated out of a top-`k`
is a correctness failure, not a relevance tradeoff, so this path sorts and
returns everything in force.
"""

from __future__ import annotations

from dataclasses import dataclass

from awm.scopes import channel
from awm.scopes.dao import ScopesDAO

GOAL_KIND = "goal"

LEVELS = ("workspace", "project", "scope")
_LEVEL_ORDER = {name: i for i, name in enumerate(LEVELS)}

#: The disposition fields carried in ``meta``, beyond the objective in ``body``.
DISPOSITION_FIELDS = ("fallback", "stop_line", "noise")

#: Appended to every rendered read. The skill that writes a goal washes out of a
#: long session; the goal record does not, so the comparison rides with the data
#: rather than only in the skill body.
COMPARISON = """\
Hold what you are about to do against the goals above — before you propose a
deliverable, and before you end a turn:

- Is this at the altitude that was asked, or one level down because one level
  down can be finished?
- A diagnosis is not a deliverable. If you can name the fix, attempt it.
- A failing check is a work item, not a verdict. Correctly attributing a
  failure is not a result.
- Partial and honestly labelled beats refusal. Say what is missing; ship the
  rest.
- Do not raise a caveat that does not change what happens next."""


class GoalError(Exception):
    """A goal could not be written or read as asked."""


# ---------------------------------------------------------------------------
# Level ⇄ channel
# ---------------------------------------------------------------------------

def owner_for_level(level: str, project: str | None = None,
                    scope: str | None = None) -> tuple[str, str]:
    """Map a level (+ its project/scope) to the channel owner pair."""
    if level == "workspace":
        return ("", "workspace")
    if level == "project":
        if not project:
            raise GoalError("level='project' needs a project")
        return ("", f"project:{project}")
    if level == "scope":
        if not (project and scope):
            raise GoalError("level='scope' needs both project and scope")
        return (project, scope)
    raise GoalError(f"unknown level {level!r}; expected one of {LEVELS}")


def level_for_owner(owner_project: str, owner_scope: str) -> str:
    """Inverse of :func:`owner_for_level` — read the level back off a channel."""
    if owner_project:
        return "scope"
    if owner_scope == "workspace":
        return "workspace"
    if owner_scope.startswith("project:"):
        return "project"
    return "scope"


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class Goal:
    id: str
    level: str
    project: str        # channel owner_project ('' for workspace/project level)
    scope: str          # channel owner_scope
    author: str
    objective: str      # the post body — the user's own words
    fallback: str
    stop_line: str
    noise: str
    supersedes: str     # id of the record this one replaces ('' if original)
    retires: str        # id this tombstone retires ('' if not a tombstone)
    ts: str

    @property
    def is_tombstone(self) -> bool:
        return bool(self.retires)

    @property
    def missing(self) -> list[str]:
        """Disposition fields not yet filled in — a goal may be partial."""
        return [f for f in DISPOSITION_FIELDS if not getattr(self, f)]

    def to_dict(self) -> dict:
        d = {
            "id": self.id, "level": self.level, "project": self.project,
            "scope": self.scope, "author": self.author,
            "objective": self.objective, "fallback": self.fallback,
            "stop_line": self.stop_line, "noise": self.noise,
            "supersedes": self.supersedes, "retires": self.retires,
            "ts": self.ts,
        }
        if not self.is_tombstone:
            d["missing"] = self.missing
        return d


def _post_to_goal(post: channel.ScopePost) -> Goal:
    meta = post.meta if isinstance(post.meta, dict) else {}
    return Goal(
        id=post.id,
        level=level_for_owner(post.project, post.scope),
        project=post.project, scope=post.scope, author=post.author,
        objective=post.body or "",
        fallback=str(meta.get("fallback") or ""),
        stop_line=str(meta.get("stop_line") or ""),
        noise=str(meta.get("noise") or ""),
        supersedes=str(meta.get("supersedes") or ""),
        retires=str(meta.get("retires") or ""),
        ts=post.ts,
    )


def _row_to_goal(row) -> Goal:
    return _post_to_goal(channel._row_to_post(row))


def _fetch_channel(owner_project: str, owner_scope: str) -> list[Goal]:
    """Every goal-kind post on one channel, oldest first. No limit by design —
    see the module docstring on why this path must not truncate."""
    rows = ScopesDAO().query_all(
        "SELECT * FROM scope_posts WHERE owner_project=? AND owner_scope=? "
        "AND kind=? ORDER BY ts ASC, id ASC",
        (owner_project, owner_scope, GOAL_KIND),
    )
    return [_row_to_goal(r) for r in rows]


def get_goal(goal_id: str) -> Goal | None:
    post = channel.get_post(goal_id)
    if post is None or post.kind != GOAL_KIND:
        return None
    return _post_to_goal(post)


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def set_goal(*, objective: str, author: str, level: str | None = None,
             project: str | None = None, scope: str | None = None,
             fallback: str | None = None, stop_line: str | None = None,
             noise: str | None = None, supersedes: str | None = None) -> Goal:
    """Record a goal, optionally as a revision of an existing one.

    A partial goal is recorded rather than refused — the missing disposition
    fields come back on the record so the caller can say what is still open.
    Demanding all four up front would be the same all-or-nothing reflex this
    whole feature exists to correct.

    With ``supersedes`` and no ``level``, the revision lands on the *same
    channel* as the record it replaces, so a restatement can never silently
    change level. Pass ``level`` explicitly to move one.

    A revision **inherits** every disposition field it does not mention. A
    caller restating only the objective would otherwise drop the stop line out
    of the in-force set without saying so — the same silent-loss failure this
    whole feature targets, by a different route. ``None`` means "unspecified,
    carry it forward"; an explicit ``""`` clears the field.
    """
    objective = (objective or "").strip()
    if not objective:
        raise GoalError("a goal needs an objective")

    prior: Goal | None = None
    if supersedes:
        prior = get_goal(supersedes)
        if prior is None:
            raise GoalError(f"no goal {supersedes!r} to supersede")
        if prior.is_tombstone:
            raise GoalError(f"goal {supersedes!r} is a tombstone, not a goal")

    if level:
        owner = owner_for_level(level, project, scope)
    elif prior is not None:
        owner = (prior.project, prior.scope)
    else:
        raise GoalError("a goal needs a level (or a goal to supersede)")

    given = {"fallback": fallback, "stop_line": stop_line, "noise": noise}
    meta = {
        field: (getattr(prior, field) if (value is None and prior is not None)
                else (value or ""))
        for field, value in given.items()
    }
    if supersedes:
        meta["supersedes"] = supersedes

    post = channel.post(owner[0], owner[1], author=author, body=objective,
                        kind=GOAL_KIND, meta=meta)
    return _post_to_goal(post)


def retire_goal(goal_id: str, *, author: str, reason: str = "") -> Goal:
    """Append a tombstone retiring ``goal_id``. Nothing is deleted; the retired
    goal stays readable through :func:`history` and a raw ``scope_fetch``."""
    target = get_goal(goal_id)
    if target is None:
        raise GoalError(f"no goal {goal_id!r} to retire")
    if target.is_tombstone:
        raise GoalError(f"{goal_id!r} is already a tombstone")
    post = channel.post(target.project, target.scope, author=author,
                        body=reason or "", kind=GOAL_KIND,
                        meta={"retires": goal_id})
    return _post_to_goal(post)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def _collapse(goals: list[Goal]) -> list[Goal]:
    """Drop every record a later append superseded or retired, plus the
    tombstones themselves. Computed over the whole union, so a revision that
    moves a goal between levels still retires the record it names."""
    dead = set()
    for g in goals:
        if g.supersedes:
            dead.add(g.supersedes)
        if g.retires:
            dead.add(g.retires)
            dead.add(g.id)
    return [g for g in goals if g.id not in dead and not g.is_tombstone]


def _sort_key(g: Goal) -> tuple:
    return (_LEVEL_ORDER.get(g.level, len(LEVELS)), g.ts, g.id)


def read_goals(project: str | None = None, scope: str | None = None,
               *, levels: list[str] | None = None) -> list[Goal]:
    """Every goal in force at ``(project, scope)``, broad to specific.

    Unions the workspace channel, the project channel and the scope channel —
    whichever the arguments reach — collapses the supersede/retire chains, and
    sorts. Nothing is scored and nothing is dropped for length.
    """
    wanted = list(levels) if levels else list(LEVELS)
    seen: dict[str, Goal] = {}
    for level in wanted:
        if level == "project" and not project:
            continue
        if level == "scope" and not (project and scope):
            continue
        try:
            owner = owner_for_level(level, project, scope)
        except GoalError:
            continue
        for g in _fetch_channel(*owner):
            seen[g.id] = g
    return sorted(_collapse(list(seen.values())), key=_sort_key)


def history(goal_id: str) -> list[Goal]:
    """The full chain of restatements a goal belongs to, oldest first, with any
    tombstone last. Walks backwards along ``supersedes`` and forwards along the
    records that name it, within the goal's own channel."""
    target = get_goal(goal_id)
    if target is None:
        raise GoalError(f"no goal {goal_id!r}")
    everything = {g.id: g for g in _fetch_channel(target.project, target.scope)}
    forward: dict[str, list[Goal]] = {}
    for g in everything.values():
        parent = g.supersedes or g.retires
        if parent:
            forward.setdefault(parent, []).append(g)

    root = target
    guard = set()
    while root.supersedes and root.supersedes in everything:
        if root.id in guard:  # a cycle can only come from a hand-edited meta
            break
        guard.add(root.id)
        root = everything[root.supersedes]

    chain: list[Goal] = []
    stack = [root]
    while stack:
        g = stack.pop(0)
        if any(c.id == g.id for c in chain):
            continue
        chain.append(g)
        stack.extend(sorted(forward.get(g.id, []), key=lambda x: (x.ts, x.id)))
    return chain


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

_LEVEL_HEADING = {
    "workspace": "Workspace — the standing frame",
    "project": "Project",
    "scope": "Scope — the current campaign",
}


def render(goals: list[Goal], project: str | None = None,
           scope: str | None = None) -> str:
    """Render the in-force set for an agent to read, with the comparison
    instruction attached. The instruction ships with the *data* on purpose: the
    skill that wrote the goal is gone from context by the time it matters."""
    where = "/".join(p for p in (project, scope) if p)
    head = f"# Goals in force{f' — {where}' if where else ''}"
    if not goals:
        return (f"{head}\n\nNothing recorded yet. Run `align` with what you are "
                f"actually after and it gets written here.")

    out = [head]
    current_level = None
    for g in goals:
        if g.level != current_level:
            current_level = g.level
            label = _LEVEL_HEADING.get(g.level, g.level)
            if g.level == "project" and project:
                label = f"{label}: {project}"
            out.append(f"\n## {label}")
        out.append(f"\n**{g.objective}**\n")
        if g.fallback:
            out.append(f"- When the ideal is unreachable: {g.fallback}")
        if g.stop_line:
            out.append(f"- Incomplete is acceptable — say so. A stop is: {g.stop_line}")
        if g.noise:
            out.append(f"- Not worth raising: {g.noise}")
        if g.missing:
            out.append(f"- *(not yet stated: {', '.join(g.missing)})*")
        out.append(f"- `id {g.id}` · set {g.ts} by {g.author}")
    out.append("\n---\n")
    out.append(COMPARISON)
    return "\n".join(out)


def read_rendered(project: str | None = None, scope: str | None = None,
                  *, levels: list[str] | None = None) -> tuple[list[Goal], str]:
    goals = read_goals(project, scope, levels=levels)
    return goals, render(goals, project, scope)
