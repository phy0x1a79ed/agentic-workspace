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
    from awm.cx.hub_adapter import API_MANIFEST

    return API_MANIFEST


SERVICE_TOML = Path(__file__).resolve().parents[1] / "service.toml"
HIDDEN = {"adopt", "claim", "seed", "remove"}


def test_every_function_projects_into_the_reflection_tool():
    for fn in manifest()["functions"]:
        domain, _, verb = fn["tool"].partition("_")
        assert domain == "reflection", (fn["name"], fn["tool"])
        assert verb.endswith("_other"), fn["tool"]


def test_the_published_tool_is_the_reflection_name_of_the_internal_handle():
    for fn in manifest()["functions"]:
        assert fn["tool"] == f"reflection_{fn['name']}_other", fn


def test_the_service_joins_the_reflection_tool_and_is_on():
    import tomllib

    cfg = tomllib.loads(SERVICE_TOML.read_text())
    assert cfg["tool"] == "reflection"
    assert cfg["tier"] == "on"
    assert cfg["profiles"] == ["cx"]


def test_the_pool_verbs_leave_mcp_and_the_lifecycle_verbs_stay():
    for fn in manifest()["functions"]:
        if fn["name"] in HIDDEN:
            assert fn["surfaces"] == ["cli", "http"], fn["name"]
        else:
            assert "surfaces" not in fn, fn["name"]


def test_no_verb_collides_with_the_calling_sessions_own():
    own = {"compact", "mode", "message_self", "pending", "whoami"}
    for fn in manifest()["functions"]:
        assert fn["tool"].removeprefix("reflection_") not in own


def test_every_declared_function_has_a_handler():
    from awm.cx.hub_adapter import HANDLERS

    assert {fn["name"] for fn in manifest()["functions"]} == set(HANDLERS)


def test_a_claim_answers_faster_than_the_client_would_wait():
    """The default thirty-second ceiling is far too long to sit in front of a
    terminal, and the manifest budget is the only one the gateway enforces."""
    claim = next(f for f in manifest()["functions"] if f["name"] == "claim")
    assert claim["timeout"] <= 15


def test_the_claim_budget_covers_a_move_that_is_merely_slow():
    """Below the move's own timeout the gateway would abort a claim that was
    about to succeed, and the session would be spent for nothing."""
    from awm.cx import claim as claim_mod

    claim = next(f for f in manifest()["functions"] if f["name"] == "claim")
    assert claim["timeout"] > claim_mod.MOVE_TIMEOUT_S


def test_the_shell_half_posts_to_the_internal_handle():
    assert "/svc/cx/fn/claim " in BIN.read_text()


def test_the_shell_half_waits_out_the_service_budget():
    """Cutting the read off early turns a slow warm start into a cold one."""
    claim = next(f for f in manifest()["functions"] if f["name"] == "claim")
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
