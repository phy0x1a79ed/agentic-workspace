"""What the manifest projects onto the MCP/CLI surface, and the shell half.

Worth a test rather than a comment because the failure is silent and only
visible after a deploy: the gateway folds a projected tool name by splitting on
its **first** underscore, so a name carrying an extra underscore becomes a
domain and a verb that both read as nonsense.
"""

from __future__ import annotations

from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin" / "cx"


def manifest():
    from awm.cch.hub_adapter import API_MANIFEST

    return API_MANIFEST


SERVICE_TOML = Path(__file__).resolve().parents[1] / "service.toml"
PLUGIN_TOML = Path(__file__).resolve().parents[1] / "awm-plugin.toml"
CONTRACT_VERBS = {"platform", "resolve", "find", "status", "list", "send",
                  "interrupt", "start", "stop", "resume", "set", "compact"}


def _claim():
    return next(f for f in manifest()["functions"] if f["name"] == "pool_claim")


def test_every_function_projects_into_the_hidden_harness_tool():
    for fn in manifest()["functions"]:
        assert fn["tool"] == f"harness-cc_{fn['name']}", fn


def test_the_service_is_hidden_and_implements_the_harness_contract():
    import tomllib

    cfg = tomllib.loads(SERVICE_TOML.read_text())
    assert (cfg["tool"], cfg["tier"]) == ("harness-cc", "off")
    plugin = tomllib.loads(PLUGIN_TOML.read_text())
    [tool] = plugin["tool"]
    assert tool["name"] == "harness-cc" and tool["tier"] == "off"
    assert tool["implements"] == ["8ca45e2d-96cb-497f-8e00-e4cb25ba0fd3@1"]


def test_it_serves_every_contract_verb_and_lists_its_optional_ones():
    from awm.cch import hub_adapter

    names = {fn["name"] for fn in manifest()["functions"]}
    assert CONTRACT_VERBS <= names
    assert set(hub_adapter.OPTIONAL_VERBS) == names - CONTRACT_VERBS


def test_every_declared_function_has_a_handler():
    from awm.cch.hub_adapter import HANDLERS

    assert {fn["name"] for fn in manifest()["functions"]} == set(HANDLERS)


def test_a_handler_that_takes_the_principal_still_gets_it():
    import inspect

    from awm.cch.hub_adapter import HANDLERS

    for name in ("list", "start", "stop", "resume", "adopt"):
        assert len(inspect.signature(HANDLERS[name]).parameters) == 2, name


def test_a_claim_answers_faster_than_the_client_would_wait():
    """The default thirty-second ceiling is far too long to sit in front of a
    terminal, and the manifest budget is the only one the gateway enforces."""
    claim = _claim()
    assert claim["timeout"] <= 15


def test_the_claim_budget_covers_a_move_that_is_merely_slow():
    """Below the move's own timeout the gateway would abort a claim that was
    about to succeed, and the session would be spent for nothing."""
    from awm.cch import claim as claim_mod

    claim = _claim()
    assert claim["timeout"] > claim_mod.MOVE_TIMEOUT_S


def test_the_shell_half_posts_to_the_internal_handle():
    assert "/svc/cch/fn/pool_claim " in BIN.read_text()


def test_the_shell_half_waits_out_the_service_budget():
    """Cutting the read off early turns a slow warm start into a cold one."""
    claim = _claim()
    src = BIN.read_text()
    wait = int(src.split("CX_WAIT=${CX_WAIT:-")[1].split("}")[0])
    assert wait >= claim["timeout"]


def test_the_shell_half_hands_the_terminal_over_on_every_path():
    """A branch that does not `exec` leaves this script in the process tree for
    the whole session, which is the one thing it exists not to do."""
    src = [ln.strip() for ln in BIN.read_text().splitlines()]
    launches = [ln for ln in src if '"$CX_CLAUDE"' in ln and not ln.startswith("#")]
    assert launches, "the script never launches Claude Code"
    for ln in launches:
        assert "exec " in ln, ln


def test_the_protocol_component_still_has_what_the_claim_reaches_for():
    """A refactor of the shared component fails here rather than in a terminal."""
    from awm import claudedaemon

    lane = claudedaemon.DaemonLane(sock="/x", auth="t", session_id="s", repl_pid=1)
    for field in ("sock", "auth", "session_id", "repl_pid", "dec_modes"):
        assert hasattr(lane, field)
    for name in ("open_lane", "connect", "DaemonError"):
        assert hasattr(claudedaemon, name)
