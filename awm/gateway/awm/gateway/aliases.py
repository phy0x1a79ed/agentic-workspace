"""Old tool and verb names that still reach their renamed successors.

A peer on the previous release, an old hook script and any caller of the HTTP
``/invoke`` door name verbs as they were published before the one-service-one-tool
surface. Each row maps an old ``(tool, verb)`` to the verb that replaced it, with
arguments the new verb needs and the old one implied, and old argument names the
new verb spells differently or no longer takes. MCP lists only the new names.

A row fires only when the old name is no longer registered and its target is, so
a live verb is never shadowed and a target that is absent (its branch not yet
merged, or a verb removed with no successor) leaves the call to fail as an
unknown tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

Pair = tuple[str, str]


@dataclass(frozen=True)
class Alias:
    """``old_tool``/``old_verb`` is the pair a domain call named; ``old_flat`` the
    flat name when it is not ``<old_tool>_<old_verb>``. ``rename`` maps an old
    argument name to its new one, or to ``None`` to drop it. ``inject`` is merged
    over the call's arguments after that."""

    old_tool: str
    old_verb: str
    new_tool: str
    new_verb: str
    inject: Mapping[str, Any] = field(default_factory=dict)
    old_flat: str | None = None
    rename: Mapping[str, str | None] = field(default_factory=dict)

    @property
    def flat_old(self) -> str:
        return self.old_flat or f"{self.old_tool}_{self.old_verb}"

    @property
    def flat_new(self) -> str:
        return f"{self.new_tool}_{self.new_verb}"

    def rewrite(self, args: Mapping[str, Any]) -> dict:
        out: dict = {}
        for key, value in args.items():
            if key in self.rename:
                if self.rename[key] is not None:
                    out[self.rename[key]] = value
            else:
                out[key] = value
        out.update(self.inject)
        return out


def _rows() -> list[Alias]:
    rows: list[Alias] = []

    def add(old_tool: str, old_verb: str, new_tool: str, new_verb: str,
            inject: Mapping[str, Any] | None = None, old_flat: str | None = None,
            rename: Mapping[str, str | None] | None = None) -> None:
        rows.append(Alias(old_tool, old_verb, new_tool, new_verb, dict(inject or {}),
                          old_flat, dict(rename or {})))

    add("reflection", "send", "reflection", "message_self")
    for verb in ("start", "list", "stop", "claim", "seed", "remove"):
        add("cx", verb, "reflection", f"{verb}_other")

    for verb in ("post", "claim", "complete", "fail", "get", "list"):
        add("board", verb, "operations", f"board_{verb}")
    for verb in ("add", "revoke", "list"):
        add("board", f"party_{verb}", "operations", f"party_{verb}")

    add("reporting", "status", "operations", "status")
    add("reporting", "claim_task", "operations", "task_claim")
    for verb in ("plan", "plan_approved", "todos", "prompt"):
        add("reporting", f"hook_{verb}", "operations", f"hook_{verb}")
    add("reporting", "ops_post", "operations", "ops_post")
    add("reporting", "board_ensure", "operations", "board_ensure")
    add("reporting", "request_human_user", "operations", "request_human", {"kind": "blocked"})
    add("reporting", "request_human_review", "operations", "request_human", {"kind": "review"})

    for verb in ("post", "fetch", "subscribe", "unsubscribe",
                 "goal_set", "goal_retire", "goal_read", "goal_history"):
        add("scope", verb, "journal", verb)
    add("scope", "data_mount", "dvc", "mount")
    add("scope", "data_gc", "dvc", "gc")
    add("scope", "data_status", "dvc", "scope_status")

    add("scope", "gather", "scope", "git_gather")
    add("scope", "scatter", "scope", "git_scatter")
    add("scope", "refresh", "journal", "render")
    add("project", "search", "scope", "search", rename={"active_only": None})
    add("project", "create", "scope", "create", {"new_project": True},
        rename={"name": "project"})
    add("ref", "resolve", "scope", "ref_resolve")

    for verb in ("status", "restart"):
        add("gateway", verb, "awm", verb, old_flat=f"awm_{verb}")
    add("gateway", "mcp-sync", "awm", "mcp_sync", old_flat="awm_mcp_sync")
    add("gateway", "list", "awm", "registry_list")
    add("gateway", "deregister", "awm", "registry_deregister")
    for verb in ("list", "start", "stop", "restart", "enable", "disable", "reap"):
        add("services", verb, "awm", f"services_{verb}")
    for verb in ("join", "add", "set", "grant", "revoke", "list", "resolve", "forget"):
        add("peer", verb, "operations", f"peer_{verb}")
    add("peer", "providers", "awm", "providers")
    add("config", "contracts", "awm", "config_contracts")
    add("config", "set", "awm", "config_set")

    for verb in ("password", "peer_credential", "verify", "edge_material", "node_key",
                 "sign_peer_token", "rotate", "user_add", "user_passwd", "user_disable",
                 "user_list", "penpot_record", "penpot_session", "penpot_rotate",
                 "penpot_list"):
        add("auth", verb, "awm", verb)
    add("auth", "status", "awm", "auth_status")

    for verb in ("acquire", "release", "reset", "status", "tab_open", "tab_close",
                 "tab_list", "tab_activate", "observe", "screenshot", "cdp", "commands"):
        add("rlm", f"browser_{verb}", "browser", verb)
    for verb in ("acquire", "release", "reset", "status", "join", "rejoin", "leave",
                 "seats", "observe", "screenshot", "observe_events", "recipes",
                 "technologies", "world_new", "world_save", "world_load", "pause",
                 "exec_lua", "walk", "wait", "throw", "order", "blueprint_stamp",
                 "blueprint_capture", "scan", "load", "move", "stop", "teleport",
                 "respawn", "mine", "craft", "build", "insert", "take", "research"):
        add("rlm", f"factorio_{verb}", "factorio", verb)
    add("penpot", "plugins_status", "penpot-plugins", "status")
    return rows


ALIASES: tuple[Alias, ...] = tuple(_rows())

_BY_FLAT: dict[str, Alias] = {a.flat_old: a for a in ALIASES}
_BY_PAIR: dict[Pair, Alias] = {(a.old_tool, a.old_verb): a for a in ALIASES}


def usage_renames() -> dict[Pair, Pair]:
    """``{(old_tool, old_verb): (new_tool, new_verb)}`` for ``usage.set_renames``."""
    return {(a.old_tool, a.old_verb): (a.new_tool, a.new_verb) for a in ALIASES}


def renamed_pair(pair: Pair) -> Pair:
    alias = _BY_PAIR.get(pair)
    return (alias.new_tool, alias.new_verb) if alias else pair


def resolve(name: str, args: dict, *, flat_names: Callable[[], set[str]],
            visible: Callable[[str, str], bool]) -> tuple[str, dict]:
    """The call a request for ``name`` becomes. ``flat_names`` yields every flat
    name the catalog can run now, and ``visible(tool, verb)`` says whether a verb
    answers to a domain call. A call that matches no row, or whose target is
    absent, comes back unchanged."""
    if not isinstance(name, str) or not isinstance(args, dict):
        return name, args
    verb = args.get("verb")
    if isinstance(verb, str):
        alias = _BY_PAIR.get((name, verb))
        if alias is not None and not visible(name, verb):
            known = flat_names()
            if alias.flat_new in known:
                inner = args.get("args")
                inner = alias.rewrite(inner if isinstance(inner, dict) else {})
                if visible(alias.new_tool, alias.new_verb):
                    return alias.new_tool, {**{k: v for k, v in args.items()
                                               if k not in ("verb", "args")},
                                            "verb": alias.new_verb, "args": inner}
                return alias.flat_new, inner
        return name, args
    alias = _BY_FLAT.get(name)
    if alias is None:
        return name, args
    known = flat_names()
    if name in known or alias.flat_new not in known:
        return name, args
    return alias.flat_new, alias.rewrite(args)
