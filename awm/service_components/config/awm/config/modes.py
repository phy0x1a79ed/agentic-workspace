"""Session modes and the verbs each may call.

`cx start` declares a mode for a session it creates. The gateway looks the
calling session's mode up and refuses any call the mode's policy does not admit
(`refusal`). A mode that is not listed in `MODES`, such as the default `worker`,
is unrestricted.

A policy is one of two shapes, and both admit only what they name or declare:

- `Allow` names the exact (domain, verb) pairs a session may call. It suits a
  session with a fixed job (the representative, the secretary).
- `ByEffect` admits verbs by the effect they declare (`read`, `queue`, `write`
  or `secret`), minus domains and verbs named out, plus a few named exceptions.
  It suits a session that does open-ended work (the delegate) and has no tool
  that runs code: a verb with no declared effect counts as `write`, so a new
  verb is closed until its author declares it a read.

A policy is a guardrail on the agent-facing doors: it cannot cover a built-in
tool the session itself holds, which is why a restricted session also launches
with an explicit tool list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

REPRESENTATIVE = "representative"
SECRETARY = "secretary"

#: The mode cx gives a session that a restricted session started. Only cx
#: assigns it, and it is never a mode a caller may ask for.
DELEGATE = "delegate"

#: What `mode_of` answers when it cannot establish a mode. The most restricted
#: mode, so an unreadable record never reads as "unrestricted".
UNKNOWN = "unknown"

#: Modes a session may not request for a session it starts. The front door
#: service, which carries no session, starts the first two; cx assigns the third.
RESERVED_MODES = frozenset({REPRESENTATIVE, SECRETARY, DELEGATE})

#: Names only the front door service may give a session.
RESERVED_NAMES = frozenset({REPRESENTATIVE, SECRETARY})

Pair = tuple[str, str]
CallArgs = dict[str, Any]


def _pairs(domain: str, *verbs: str) -> frozenset[Pair]:
    return frozenset((domain, verb) for verb in verbs)


@dataclass(frozen=True)
class Allow:
    """A session in this mode may call exactly these (domain, verb) pairs."""

    pairs: frozenset[Pair]

    def uses(self, domain: str) -> bool:
        return any(d == domain for d, _ in self.pairs)

    def admits(self, domain: str, verb: str, effect: str | None,
               call_args: CallArgs | None) -> bool:
        return (domain, verb) in self.pairs


@dataclass(frozen=True)
class ByEffect:
    """A session in this mode may call verbs that declare one of `effects`.

    `pairs` are admitted whatever they declare, and `when` admits a pair only
    for calls whose own arguments pass its test. `denied_domains` and
    `denied_pairs` are refused whatever they declare, and win over everything.
    """

    effects: frozenset[str]
    denied_domains: frozenset[str] = frozenset()
    denied_pairs: frozenset[Pair] = frozenset()
    pairs: frozenset[Pair] = frozenset()
    when: dict[Pair, Callable[[CallArgs], bool]] = field(default_factory=dict)

    def uses(self, domain: str) -> bool:
        return domain not in self.denied_domains

    def admits(self, domain: str, verb: str, effect: str | None,
               call_args: CallArgs | None) -> bool:
        if domain in self.denied_domains or (domain, verb) in self.denied_pairs:
            return False
        if (domain, verb) in self.pairs:
            return True
        test = self.when.get((domain, verb))
        if test is not None:
            return isinstance(call_args, dict) and test(call_args)
        return effect in self.effects


Policy = Allow | ByEffect


def is_reply(call_args: CallArgs) -> bool:
    """Whether a `board post` is a message card answering another card."""
    reply_to = call_args.get("reply_to")
    return call_args.get("kind") == "message" and isinstance(reply_to, str) and bool(reply_to)


#: A restricted session can always shed its own context. `compact` acts only on
#: the calling session (the gateway stamps the pid), and `reflection mode`,
#: which changes the session's permission mode, is deliberately absent.
_REFLECTION = _pairs("reflection", "compact", "whoami")

#: Pure reads of the scope channel.
_SCOPE_READS = (_pairs("scope", "fetch", "search", "goal_read", "goal_history", "resolve")
                | _pairs("project", "search"))

MODES: dict[str, Policy] = {
    # Triage only: read cards, start or list workers, record the hand-off, and
    # fail a card it refuses. It never does a card's work and never claims,
    # completes or posts a card: the front door claims, a delegate finishes.
    REPRESENTATIVE: Allow(
        _pairs("board", "list", "get", "fail")
        | _pairs("cx", "start", "list")
        | _pairs("door", "status", "list", "get", "assign")
        | _REFLECTION
        | _SCOPE_READS
    ),
    # Tony's assistant over Remote Control: starts, names, lists and stops
    # agents. It never touches the board except to read it.
    SECRETARY: Allow(
        _pairs("cx", "start", "list", "stop")
        | _pairs("board", "list", "get")
        | _pairs("door", "status", "list", "get")
        | _REFLECTION
        | _SCOPE_READS
    ),
    # The worker a restricted session starts for a card. It reads what the
    # node declares readable and finishes its card on the board, and it posts a
    # message card only as a reply. It starts nothing, writes no scope, queue or
    # setting, and has no browser: `rlm` can drive a loopback browser to the
    # gateway's HTTP door, past this gate.
    DELEGATE: ByEffect(
        effects=frozenset({"read"}),
        denied_domains=frozenset({"rlm", "door"}),
        denied_pairs=_pairs("board", "party_list"),
        pairs=_pairs("board", "get", "list", "complete", "fail") | _REFLECTION,
        when={("board", "post"): is_reply},
    ),
    # A session whose mode could not be established gets the least that keeps
    # it alive: it can compact itself and nothing else.
    UNKNOWN: Allow(_REFLECTION),
}


def is_restricted(mode: str | None) -> bool:
    """Whether calls from a session in this mode are gated at all."""
    return mode is not None and mode in MODES


def refusal(mode: str | None, domain: str | None, verb: Any, peer: Any = None, *,
            effect: str | None = None, call_args: CallArgs | None = None) -> str | None:
    """Why a session in `mode` may not call `domain`'s `verb`, or None if it may.

    `effect` is the effect the verb declares (None when the caller could not
    resolve the call to a declared verb), and `call_args` the verb's own
    arguments, for a rule that depends on them. `describe` is allowed for a
    domain the mode can use at all, so a restricted session can read the schemas
    of the verbs it holds. A restricted mode may not name a `peer`: the verb
    would then run on another node, past this node's gate.
    """
    if not is_restricted(mode):
        return None
    policy = MODES[mode]
    label = f"mode {mode!r}"
    if peer is not None:
        return f"{label} may not run a verb on a peer"
    if not isinstance(domain, str) or not domain or not isinstance(verb, str) or not verb:
        return f"{label} may call only its allowed verbs; this call names no domain and verb"
    if verb == "describe":
        if policy.uses(domain):
            return None
        return f"{label} may not use the {domain!r} domain"
    if policy.admits(domain, verb, effect, call_args):
        return None
    return f"{label} may not call {domain}.{verb}"
