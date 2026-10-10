"""Session modes and the verbs each may call.

`cx start` declares a mode for a session it creates. The gateway looks the
calling session's mode up and refuses any call the mode's policy does not admit
(`refusal`). A mode that is not listed in `MODES`, such as the default `worker`,
is unrestricted.

A policy is one of two shapes. An `Allow` policy names the only verbs a session
may call, and suits a session with a fixed job (the representative, the
secretary). A `Deny` policy names what a session may not call and admits the
rest, and suits a session that does open-ended work (the delegate). Both name a
domain and a verb, because that is the unit a caller addresses.

A policy is a guardrail on the agent-facing doors: it cannot cover a built-in
tool the session itself holds, which is why a restricted session also launches
with an explicit tool list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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


def _pairs(domain: str, *verbs: str) -> frozenset[Pair]:
    return frozenset((domain, verb) for verb in verbs)


@dataclass(frozen=True)
class Allow:
    """A session in this mode may call exactly these (domain, verb) pairs."""

    pairs: frozenset[Pair]

    def domains(self) -> frozenset[str]:
        return frozenset(d for d, _ in self.pairs)

    def admits(self, domain: str, verb: str) -> bool:
        return (domain, verb) in self.pairs

    def uses(self, domain: str) -> bool:
        return domain in self.domains()


@dataclass(frozen=True)
class Deny:
    """A session in this mode may call anything except what is named here.

    `domains` are denied whole, `pairs` singly, and `keep` exempts a pair from
    a whole-domain denial.
    """

    domains: frozenset[str] = frozenset()
    pairs: frozenset[Pair] = frozenset()
    keep: frozenset[Pair] = field(default_factory=frozenset)

    def admits(self, domain: str, verb: str) -> bool:
        if (domain, verb) in self.pairs:
            return False
        return domain not in self.domains or (domain, verb) in self.keep

    def uses(self, domain: str) -> bool:
        return domain not in self.domains or any(d == domain for d, _ in self.keep)


Policy = Allow | Deny


#: A restricted session can always shed its own context. `compact` acts only on
#: the calling session (the gateway stamps the pid), and `reflection mode`,
#: which changes the session's permission mode, is deliberately absent.
_REFLECTION = _pairs("reflection", "compact", "whoami")

#: Pure reads of the scope channel.
_SCOPE_READS = (_pairs("scope", "fetch", "search", "goal_read", "goal_history", "resolve")
                | _pairs("project", "search"))

#: Domains a delegate may not touch at all: remote access, credentials, the
#: network, node administration and anything that runs code elsewhere.
_DELEGATE_DENIED_DOMAINS = frozenset({
    "cx", "ssh", "auth", "peer", "config", "vpn", "2fa", "tether", "social",
    "httpsfront", "gateway", "services", "dsh", "compute", "dev", "agent",
    "orch", "workspace",
})

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
    # The worker a restricted session starts for a card. It works freely in the
    # awm domains that cannot reach outside the node, and may finish its card
    # on the board, but it cannot start sessions, reach other hosts, touch
    # credentials or administer the node.
    DELEGATE: Deny(
        domains=_DELEGATE_DENIED_DOMAINS,
        keep=_pairs("cx", "list"),
        pairs=(_pairs("board", "party_add", "party_revoke", "party_list")
               | _pairs("reflection", "send", "mode")
               | _pairs("scope", "delete", "data_gc")),
    ),
    # A session whose mode could not be established gets the least that keeps
    # it alive: it can compact itself and nothing else.
    UNKNOWN: Allow(_REFLECTION),
}


def is_restricted(mode: str | None) -> bool:
    """Whether calls from a session in this mode are gated at all."""
    return mode is not None and mode in MODES


def refusal(mode: str | None, domain: str | None, verb: Any,
            peer: Any = None) -> str | None:
    """Why a session in `mode` may not call `domain`'s `verb`, or None if it may.

    `describe` is allowed for a domain the mode can use at all, so a restricted
    session can read the schemas of the verbs it holds. A restricted mode may
    not name a `peer`: the verb would then run on another node, past this
    node's gate.
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
    if policy.admits(domain, verb):
        return None
    return f"{label} may not call {domain}.{verb}"
