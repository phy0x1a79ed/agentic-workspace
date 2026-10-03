"""The fleet-wide collapsed surface: one tool per domain, ``peer`` chooses the node.

Covers ``peer_catalog``'s provider/default resolution and the ``peers=1``
projection + dispatch routing in ``catalog``. All synchronous against a stub
registry and a hand-built peer snapshot — no hub, no ssh, no peer edge.

The behaviour under test replaced merging each peer's whole catalog into the
surface under ``<domain>@<peer>`` names, which multiplied the tool count by the
peer count for no new capability. So the assertions that matter most are the ones
about *absence*: no ``@`` names, and one entry per domain however many peers.
"""

from __future__ import annotations

import pytest

from awm.gateway import catalog, peer_catalog
from awm.gateway.hub.registry import ServiceRecord


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _StubRegistry:
    def __init__(self, records):
        self._records = records

    def service_records(self):
        return list(self._records)


@pytest.fixture()
def local_registry(monkeypatch):
    """This node runs `scope` (also on both peers) and `social` (peers too)."""
    rec = ServiceRecord(
        name="scopes", prefix="/svc/scopes", kind="service", service_id="sid-scopes",
        api={"functions": [
            {"name": "search", "tool": "scope_search",
             "description": "Search scopes.", "params": []},
            {"name": "create", "tool": "scope_create",
             "description": "Create a scope.", "params": []},
        ]},
    )
    stub = _StubRegistry([rec])
    monkeypatch.setattr(catalog, "get_registry", lambda: stub)
    return stub


@pytest.fixture()
def snap():
    """mira reachable with four domains; altair asleep but last known to have two.

    ``2fa`` lives only on mira, ``orch`` only on mira, ``hpcllm`` on both — the
    three shapes that exercise every resolution rule.
    """
    return {
        "altair": {"domains": {"scope": ["search"], "hpcllm": ["ask"]},
                   "reachable": False, "error": "ssh: timed out"},
        "mira": {"domains": {"scope": ["search", "create"], "2fa": ["approve"],
                             "orch": ["run"], "hpcllm": ["ask"]},
                 "reachable": True, "error": None},
    }


@pytest.fixture()
def no_homes(monkeypatch):
    """No declared singletons, whatever the ambient environment says.

    The seeded table reads ``AWM_TWOFA_PEER``, which is genuinely set on a
    borrowing node — so a test that did not clear it would pass or fail depending
    on the shell it ran in.
    """
    monkeypatch.delenv("AWM_TWOFA_PEER", raising=False)
    for key in list(__import__("os").environ):
        if key.startswith(peer_catalog._HOME_ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def twofa_on_mira(no_homes, monkeypatch):
    monkeypatch.setenv("AWM_TWOFA_PEER", "mira")


def _local(catalog_map=None):
    return catalog._local_domain_verbs(catalog._domain_catalog())


# ---------------------------------------------------------------------------
# Rule 1 — a declared singleton has exactly ONE provider
# ---------------------------------------------------------------------------


def test_singleton_has_one_provider_which_is_the_default(
        local_registry, snap, twofa_on_mira):
    res = peer_catalog.resolve("2fa", _local(), snap)
    assert res["default"] == "mira"
    assert res["reason"] == peer_catalog.REASON_SINGLETON
    assert [p["peer"] for p in res["providers"]] == ["mira"]


def test_singleton_refuses_any_other_peer(local_registry, snap, twofa_on_mira):
    """Including ``local``. Two nodes both acting as a valid provider for one
    external resource is the concrete failure this rule exists to stop — two
    listeners spending a single Duo attempt budget."""
    for requested in ("local", "altair", "cosmos"):
        with pytest.raises(ValueError, match="singleton"):
            peer_catalog.choose_target("2fa", requested, _local(), snap)


def test_singleton_default_needs_no_peer_argument(
        local_registry, snap, twofa_on_mira):
    assert peer_catalog.choose_target("2fa", None, _local(), snap) == "mira"


def test_declared_home_via_generic_env(local_registry, snap, no_homes, monkeypatch):
    """``AWM_DOMAIN_HOME_<domain>`` declares a singleton without a code change."""
    monkeypatch.setenv("AWM_DOMAIN_HOME_scope", "mira")
    res = peer_catalog.resolve("scope", _local(), snap)
    assert res["default"] == "mira"
    assert [p["peer"] for p in res["providers"]] == ["mira"]


def test_social_and_ssh_are_not_domain_singletons(local_registry, snap, monkeypatch):
    """Only ``AWM_TWOFA_PEER`` means "the whole domain lives elsewhere".

    ``social`` runs per-node (only individual accounts are singular) and only the
    ssh *slot-arbiter role* is fleet-global, so re-homing either domain would cost
    this node its own accounts / its own ssh. Guards against someone
    "generalising" the seeded table to every ``AWM_*_PEER`` selector.
    """
    monkeypatch.setenv("AWM_SOCIAL_PEER", "mira")
    monkeypatch.setenv("AWM_SSH_SLOT_PEER", "mira")
    assert "social" not in peer_catalog.declared_homes()
    assert "ssh" not in peer_catalog.declared_homes()


# ---------------------------------------------------------------------------
# Rules 2-4 — local default, sole peer, ambiguous
# ---------------------------------------------------------------------------


def test_local_domain_defaults_local_with_peers_as_overrides(
        local_registry, snap, no_homes):
    res = peer_catalog.resolve("scope", _local(), snap)
    assert res["default"] == peer_catalog.LOCAL
    assert res["reason"] == peer_catalog.REASON_LOCAL
    assert [p["peer"] for p in res["providers"]] == ["local", "altair", "mira"]
    assert peer_catalog.choose_target("scope", None, _local(), snap) == "local"
    assert peer_catalog.choose_target("scope", "mira", _local(), snap) == "mira"


def test_sole_peer_domain_is_callable_with_no_peer_argument(
        local_registry, snap, no_homes):
    """``orch`` exists only on mira. It must still be callable by its plain name
    with no ``peer`` — that is what keeps a peer-only domain reachable now that
    the ``orch@mira`` twin is gone."""
    res = peer_catalog.resolve("orch", _local(), snap)
    assert res["reason"] == peer_catalog.REASON_SOLE_PEER
    assert res["default"] == "mira"
    assert peer_catalog.choose_target("orch", None, _local(), snap) == "mira"


def test_ambiguous_domain_refuses_without_peer_and_names_options(
        local_registry, snap, no_homes):
    """Two peers, no local, no declared home → no default. Guessing would bind
    the call to whichever node happened to sort first."""
    res = peer_catalog.resolve("hpcllm", _local(), snap)
    assert res["reason"] == peer_catalog.REASON_AMBIGUOUS
    assert res["default"] is None
    with pytest.raises(ValueError) as exc:
        peer_catalog.choose_target("hpcllm", None, _local(), snap)
    assert "altair" in str(exc.value) and "mira" in str(exc.value)
    assert peer_catalog.choose_target("hpcllm", "altair", _local(), snap) == "altair"


def test_unknown_tool_is_unknown(local_registry, snap, no_homes):
    res = peer_catalog.resolve("nosuchthing", _local(), snap)
    assert res["reason"] == peer_catalog.REASON_UNKNOWN
    with pytest.raises(ValueError, match="Unknown tool"):
        peer_catalog.choose_target("nosuchthing", None, _local(), snap)


def test_unreachable_peer_is_reported_not_dropped(local_registry, snap, no_homes):
    """"altair does not have this" and "altair is asleep" are different answers;
    conflating them makes the discovery tool useless."""
    res = peer_catalog.resolve("scope", _local(), snap)
    altair = next(p for p in res["providers"] if p["peer"] == "altair")
    assert altair["reachable"] is False
    assert "timed out" in altair["error"]
    # ...and still selectable: reachability is a 2-minute-old snapshot bit, so the
    # call itself is the authority on whether the peer answers.
    assert peer_catalog.choose_target("scope", "altair", _local(), snap) == "altair"


def test_a_peer_never_overrides_a_local_verb_set(local_registry, snap, no_homes):
    """The advertised verbs come from the DEFAULT provider, not a merge — mira's
    ``scope`` may carry verbs this node's release does not."""
    res = peer_catalog.resolve("scope", _local(), snap)
    assert catalog._advertised_verbs(res) == ["search", "create"]


# ---------------------------------------------------------------------------
# The projection
# ---------------------------------------------------------------------------


def test_fleet_view_has_one_tool_per_domain_and_no_at_names(
        local_registry, snap, twofa_on_mira, monkeypatch):
    monkeypatch.setattr(peer_catalog, "snapshot", lambda: snap)
    tools = catalog.list_domain_tools(peers=True)
    names = [t.name for t in tools]
    assert len(names) == len(set(names)), "a domain must appear exactly once"
    assert not any("@" in n for n in names), "peer-suffixed twins are gone"
    # peer-only domains are present, so nothing reachable stopped being nameable
    assert {"scope", "2fa", "orch", "hpcllm"} <= set(names)
    assert catalog._PROVIDERS_TOOL in names


def test_fleet_view_is_not_multiplied_by_peer_count(
        local_registry, snap, twofa_on_mira, monkeypatch):
    """The whole point: adding a peer that runs the same domains adds no tools."""
    monkeypatch.setattr(peer_catalog, "snapshot", lambda: snap)
    before = len(catalog.list_domain_tools(peers=True))
    widened = dict(snap)
    widened["cosmos"] = {"domains": {"scope": ["search"], "hpcllm": ["ask"]},
                         "reachable": True, "error": None}
    monkeypatch.setattr(peer_catalog, "snapshot", lambda: widened)
    assert len(catalog.list_domain_tools(peers=True)) == before


def test_default_view_stays_local_only(local_registry, snap, monkeypatch):
    """A peer reads *this* view when it fetches our catalog. If it carried our
    peers, the fleet would advertise transitive nodes nobody can dial."""
    monkeypatch.setattr(peer_catalog, "snapshot", lambda: snap)
    names = {t.name for t in catalog.list_domain_tools()}
    assert "orch" not in names and "hpcllm" not in names
    assert catalog._PROVIDERS_TOOL not in names
    assert "scope" in names


def test_fleet_descriptions_say_where_a_call_lands(
        local_registry, snap, twofa_on_mira, monkeypatch):
    monkeypatch.setattr(peer_catalog, "snapshot", lambda: snap)
    tools = {t.name: t for t in catalog.list_domain_tools(peers=True)}
    assert "singleton owned by 'mira'" in tools["2fa"].description
    assert "this node by default" in tools["scope"].description
    assert "only provider" in tools["orch"].description
    assert "no default" in tools["hpcllm"].description


def test_every_fleet_tool_carries_the_peer_key(
        local_registry, snap, twofa_on_mira, monkeypatch):
    monkeypatch.setattr(peer_catalog, "snapshot", lambda: snap)
    for tool in catalog.list_domain_tools(peers=True):
        if tool.name == catalog._PROVIDERS_TOOL:
            continue
        assert "peer" in tool.inputSchema["properties"], tool.name


# ---------------------------------------------------------------------------
# Dispatch routing
# ---------------------------------------------------------------------------


class _FakeChannel:
    def __init__(self):
        self.calls = []

    async def call(self, fn, args, as_=None, timeout=None):
        self.calls.append((fn, args, as_))
        return {"ok": fn}


async def test_local_default_dispatches_locally(
        local_registry, snap, no_homes, monkeypatch):
    monkeypatch.setattr(peer_catalog, "snapshot", lambda: snap)
    ch = _FakeChannel()
    monkeypatch.setattr(catalog.rpc, "get_control", lambda sid: ch)
    await catalog.dispatch("scope", {"verb": "search", "args": {}})
    assert ch.calls and ch.calls[0][0] == "search"


async def test_explicit_peer_raises_a_redirect_not_a_relay(
        local_registry, snap, no_homes, monkeypatch):
    """The gateway hands back an address; it never carries the call. That is the
    invariant that keeps peer bytes off every gateway."""
    monkeypatch.setattr(peer_catalog, "snapshot", lambda: snap)
    monkeypatch.setattr(catalog.rpc, "get_control", lambda sid: _FakeChannel())
    monkeypatch.setattr(
        "awm.gateway.peers.resolve",
        lambda name: {"name": name, "edge_url": "https://mira:12100",
                      "ssh_alias": "miraz"})
    with pytest.raises(peer_catalog.PeerRedirect) as exc:
        await catalog.dispatch("scope", {"verb": "search", "args": {},
                                         "peer": "mira"})
    payload = exc.value.payload()
    assert payload["peer"] == "mira"
    assert payload["edge_url"] == "https://mira:12100"
    assert payload["ssh_alias"] == "miraz"


async def test_singleton_redirects_with_no_peer_argument(
        local_registry, snap, twofa_on_mira, monkeypatch):
    monkeypatch.setattr(peer_catalog, "snapshot", lambda: snap)
    monkeypatch.setattr("awm.gateway.peers.resolve", lambda name: {"name": name})
    with pytest.raises(peer_catalog.PeerRedirect) as exc:
        await catalog.dispatch("2fa", {"verb": "approve", "args": {}})
    assert exc.value.peer == "mira"


async def test_peer_only_domain_is_dispatchable_by_plain_name(
        local_registry, snap, no_homes, monkeypatch):
    """Before this, a name absent from the local catalog fell through to the flat
    branch and raised "Unknown tool" — the reason ``orch@mira`` had to exist."""
    monkeypatch.setattr(peer_catalog, "snapshot", lambda: snap)
    monkeypatch.setattr("awm.gateway.peers.resolve", lambda name: {"name": name})
    with pytest.raises(peer_catalog.PeerRedirect) as exc:
        await catalog.dispatch("orch", {"verb": "run", "args": {}})
    assert exc.value.peer == "mira"


async def test_describe_is_routed_not_answered_locally(
        local_registry, snap, no_homes, monkeypatch):
    """``describe`` used to short-circuit before any routing, which would have
    described verbs the target peer does not have."""
    monkeypatch.setattr(peer_catalog, "snapshot", lambda: snap)
    monkeypatch.setattr("awm.gateway.peers.resolve", lambda name: {"name": name})
    with pytest.raises(peer_catalog.PeerRedirect):
        await catalog.dispatch("scope", {"verb": "describe", "peer": "mira"})
    # ...but a local describe still answers from the catalog with no round trip
    out = await catalog.dispatch("scope", {"verb": "describe"})
    assert "search" in out


async def test_bad_peer_argument_fails_loudly_never_falls_back_to_local(
        local_registry, snap, no_homes, monkeypatch):
    monkeypatch.setattr(peer_catalog, "snapshot", lambda: snap)
    ch = _FakeChannel()
    monkeypatch.setattr(catalog.rpc, "get_control", lambda sid: ch)
    with pytest.raises(ValueError, match="does not provide"):
        await catalog.dispatch("scope", {"verb": "search", "args": {},
                                         "peer": "nosuchnode"})
    assert not ch.calls, "a misdirected call must not run here instead"


async def test_non_string_peer_is_rejected(local_registry, snap, no_homes,
                                           monkeypatch):
    monkeypatch.setattr(peer_catalog, "snapshot", lambda: snap)
    with pytest.raises(ValueError, match="node name"):
        await catalog.dispatch("scope", {"verb": "search", "peer": 7})


async def test_flat_dispatch_is_untouched_by_routing(
        local_registry, snap, no_homes, monkeypatch):
    """The CLI's by-name ``/invoke`` posts carry no ``verb``, so they never enter
    the domain branch and can never be redirected."""
    monkeypatch.setattr(peer_catalog, "snapshot", lambda: snap)
    ch = _FakeChannel()
    monkeypatch.setattr(catalog.rpc, "get_control", lambda sid: ch)
    await catalog.dispatch("scope_search", {})
    assert ch.calls and ch.calls[0][0] == "search"


# ---------------------------------------------------------------------------
# providersOf
# ---------------------------------------------------------------------------


async def test_providers_of_one_tool(local_registry, snap, twofa_on_mira,
                                     monkeypatch):
    monkeypatch.setattr(peer_catalog, "snapshot", lambda: snap)
    out = await catalog.dispatch(catalog._PROVIDERS_TOOL, {"tool": "2fa"})
    assert '"default": "mira"' in out
    assert '"reason": "declared-singleton"' in out


async def test_providers_of_whole_fleet(local_registry, snap, twofa_on_mira,
                                        monkeypatch):
    monkeypatch.setattr(peer_catalog, "snapshot", lambda: snap)
    out = await catalog.dispatch(catalog._PROVIDERS_TOOL, {})
    for domain in ("scope", "2fa", "orch", "hpcllm"):
        assert f'"tool": "{domain}"' in out


async def test_providers_of_does_not_sweep_unless_asked(
        local_registry, snap, no_homes, monkeypatch):
    """A sweep costs an ssh per peer; the default read must never pay it."""
    swept = []

    async def _sweep():
        swept.append(True)
        return snap

    monkeypatch.setattr(peer_catalog, "snapshot", lambda: snap)
    monkeypatch.setattr(peer_catalog, "sweep", _sweep)
    await catalog.dispatch(catalog._PROVIDERS_TOOL, {"tool": "scope"})
    assert not swept
    await catalog.dispatch(catalog._PROVIDERS_TOOL,
                           {"tool": "scope", "refresh": True})
    assert swept


def test_providers_of_is_not_folded_into_a_domain(local_registry):
    """A bare single-token tool name becomes its own junk single-verb domain, so
    the reserved tool must stay out of the fold."""
    assert catalog._PROVIDERS_TOOL in peer_catalog.RESERVED_TOOLS
    assert catalog._PROVIDERS_TOOL not in catalog._domain_catalog()


# ---------------------------------------------------------------------------
# The sweep's own parsing
# ---------------------------------------------------------------------------


def test_verbs_read_from_the_enum_when_present():
    tool = {"name": "scope", "description": "irrelevant",
            "inputSchema": {"properties": {"verb": {"enum": ["a", "b"]}}}}
    assert peer_catalog._verbs_from_tool(tool) == ["a", "b"]


def test_verbs_fall_back_to_the_description_for_an_older_peer():
    """A peer tracking an older release predates the enum. The fleet updates node
    by node, so that window is real and its domains must not read as verbless."""
    tool = {"name": "scope",
            "description": "Generic 'scope' domain tool. Verbs: search, create. "
                           "Call with {verb, args}.",
            "inputSchema": {"properties": {"verb": {"type": "string"}}}}
    assert peer_catalog._verbs_from_tool(tool) == ["search", "create"]


def test_no_peers_means_an_empty_snapshot_and_everything_local(
        local_registry, no_homes, monkeypatch):
    monkeypatch.setattr("awm.gateway.peers.list_all", lambda: [])
    monkeypatch.setattr(peer_catalog, "snapshot", lambda: {})
    assert peer_catalog.choose_target("scope", None, _local(), {}) == "local"
    names = {t.name for t in catalog.list_domain_tools(peers=True)}
    assert "scope" in names and "orch" not in names


async def test_sweep_keeps_last_known_domains_for_a_failed_peer(monkeypatch):
    """One failed sweep must not make a peer's tools vanish and reappear."""
    monkeypatch.setattr(
        "awm.gateway.peers.list_all",
        lambda: [{"name": "mira", "edge_url": "https://mira:12100",
                  "ssh_alias": "miraz"}])

    async def _ok(entry):
        return {"scope": ["search"]}

    monkeypatch.setattr(peer_catalog, "_fetch_peer", _ok)
    first = await peer_catalog.sweep()
    assert first["mira"]["reachable"] is True

    async def _boom(entry):
        raise RuntimeError("asleep")

    monkeypatch.setattr(peer_catalog, "_fetch_peer", _boom)
    second = await peer_catalog.sweep()
    assert second["mira"]["reachable"] is False
    assert second["mira"]["domains"] == {"scope": ["search"]}
    assert "asleep" in second["mira"]["error"]
