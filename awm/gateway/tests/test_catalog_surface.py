"""The collapsed MCP surface under the one-service-one-tool rule: tool and tier
keys, hidden tools and verbs, top-level peer-only tools, strict arguments,
ranked describe, and the usage tally behind it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer

from awm.gateway import catalog, peer_catalog, usage
from awm.gateway.hub import discovery
from awm.gateway.hub.registry import ServiceRecord
from awm.gateway.operations import register_service_cli_commands

pytestmark = pytest.mark.usefixtures("domestic_peers")


_NATIVE = {"gateway", "services", "peer", "config"}


def _services_only(catalog_map):
    return {k: v for k, v in catalog_map.items() if k not in _NATIVE}


class _StubRegistry:
    def __init__(self, records):
        self._records = records

    def service_records(self):
        return list(self._records)


class _Channel:
    def __init__(self):
        self.calls = []

    async def call(self, fn, args, as_=None, timeout=None):
        self.calls.append((fn, args))
        return {"ran": fn}


def _folder(root, name, toml=None):
    d = root / name
    d.mkdir(parents=True)
    (d / "run.sh").write_text("#!/bin/bash\n")
    if toml is not None:
        (d / "service.toml").write_text(toml)


def _rec(name, *fns, description=None):
    api = {"functions": [
        {"name": n, "tool": t, "description": f"{t} does it. More text.",
         "params": params, **({"surfaces": s} if s else {})}
        for n, t, params, s in fns]}
    if description:
        api["description"] = description
    return ServiceRecord(name=name, prefix=f"/svc/{name}", kind="service",
                         service_id=f"sid-{name}", api=api)


def _p(*names, required=()):
    return [{"name": n, "type": "string", "required": n in required} for n in names]


@pytest.fixture()
def world(tmp_path, monkeypatch, awm_workspace):
    """``news`` (tool ``news``) and ``rlm-browser`` (tool ``browser``) follow the
    rule; ``legacy`` is allowlisted. ``hidden`` is off, and ``news`` hides one verb."""
    root = tmp_path / "services"
    _folder(root, "news", 'tool = "news"\ntier = "on"\n')
    _folder(root, "rlm-browser", 'tool = "browser"\nprofiles = ["gamebot"]\n')
    _folder(root, "hidden", 'tier = "off"\n')
    _folder(root, "scopes", 'tier = "core"\n')
    monkeypatch.setenv("AWM_SERVICES_DIR", str(root))
    monkeypatch.delenv("AWM_PROFILES", raising=False)
    monkeypatch.delenv("AWM_TWOFA_PEER", raising=False)
    monkeypatch.setattr(peer_catalog, "snapshot", lambda: {})
    usage._reset_for_tests()
    monkeypatch.setattr(usage, "SEED_PATH", tmp_path / "no-seed.txt")
    stub = _StubRegistry([
        _rec("news",
             ("get", "news_get", _p("id", "fmt", required=("id",)), None),
             ("list", "news_list", _p("limit"), None),
             ("purge", "news_purge", _p(), ["cli", "http"])),
        _rec("rlm-browser",
             ("status", "browser_status", _p(), None),
             ("open", "browser_open", _p("url", required=("url",)), None)),
        _rec("hidden", ("ask", "hidden_ask", _p("q"), None)),
        _rec("scopes", ("search", "scope_search", _p("q"), None)),
    ])
    monkeypatch.setattr(catalog, "get_registry", lambda: stub)
    ch = _Channel()
    monkeypatch.setattr(catalog.rpc, "get_control", lambda sid: ch)
    yield ch
    usage._reset_for_tests()


# -- tool and tier keys -------------------------------------------------------


@pytest.mark.parametrize("tier", ["core", "more", "discoverable", "on", "", "weird"])
def test_every_tier_but_off_reads_as_on(tmp_path, tier):
    (tmp_path / "service.toml").write_text(f'tier = "{tier}"\n')
    assert discovery.read_tier(tmp_path) == discovery.TIER_ON


def test_off_reads_as_off_and_is_reread_when_the_file_changes(tmp_path):
    toml = tmp_path / "service.toml"
    toml.write_text('tier = "off"\n')
    assert discovery.read_tier(tmp_path) == discovery.TIER_OFF
    toml.write_text("# gone\n")
    assert discovery.read_tier(tmp_path) == discovery.TIER_ON


def test_a_broken_service_toml_never_switches_a_tool_off(tmp_path):
    (tmp_path / "service.toml").write_text("tier = [not toml")
    assert discovery.read_tier(tmp_path) == discovery.TIER_ON
    assert discovery.read_tool(tmp_path) == tmp_path.name


def test_tool_defaults_to_the_folder_name(world):
    assert discovery.service_tool("news") == "news"
    assert discovery.service_tool("rlm-browser") == "browser"
    assert discovery.service_tool("hidden") == "hidden"
    assert discovery.service_tool("../news") == "../news"


def test_tool_and_tier_do_not_touch_the_profile_gate(world):
    assert discovery.is_enabled("news") is True
    assert discovery.is_enabled("rlm-browser") is False


# -- the projected surface ----------------------------------------------------


def test_the_surface_is_one_top_level_tool_per_tool_name(world):
    names = {t.name for t in catalog.list_domain_tools(peers=True)} - _NATIVE
    assert names == {"news", "browser", "scope"}
    assert "more" not in names and "providersOf" not in names


def test_a_switched_off_tool_is_absent_from_mcp_but_still_runs_flat(world):
    assert "hidden" not in {t.name for t in catalog.list_domain_tools(peers=True)}
    assert "hidden" not in {t.name for t in catalog.list_domain_tools()}
    assert "hidden_ask" in {t.name for t in catalog.list_tools()}


async def test_a_switched_off_tool_still_answers_over_http_flat_names(world):
    out = await catalog.dispatch("hidden_ask", {"q": "x"})
    assert json.loads(out) == {"ran": "ask"}
    with pytest.raises(ValueError, match="Unknown"):
        await catalog.dispatch("hidden", {"verb": "ask"})


async def test_a_hidden_verb_leaves_mcp_and_keeps_cli_and_http(world):
    verbs = [v["verb"] for v in catalog._domain_catalog()["news"]]
    assert verbs == ["get", "list"]
    assert "news_purge" in {t.name for t in catalog.list_tools()}
    assert json.loads(await catalog.dispatch("news_purge", {})) == {"ran": "purge"}
    with pytest.raises(ValueError, match="Unknown verb"):
        await catalog.dispatch("news", {"verb": "purge"})


async def test_a_renamed_tool_dispatches_by_its_new_name(world):
    out = await catalog.dispatch("browser", {"verb": "open", "args": {"url": "u"}})
    assert json.loads(out) == {"ran": "open"}
    assert world.calls == [("open", {"url": "u"})]


def test_a_tool_description_lists_each_verb_with_its_required_arguments(world):
    tool = {t.name: t for t in catalog.list_domain_tools()}["news"]
    assert "- get(id): news_get does it." in tool.description
    assert "- list(): news_list does it." in tool.description
    assert "purge" not in tool.description
    assert tool.description.count("describe") == 1


def test_a_peer_only_tool_is_top_level_on_a_node_that_does_not_run_it(
        world, monkeypatch):
    snap = {"mira": {"domains": {"social": ["search"]}, "reachable": True,
                     "error": None,
                     "descriptions": {"social": "Chat accounts. Verbs:\n- search()."}}}
    monkeypatch.setattr(peer_catalog, "snapshot", lambda: snap)
    tools = {t.name: t for t in catalog.list_domain_tools(peers=True)}
    assert tools["social"].description.startswith("Chat accounts.")
    assert tools["social"].inputSchema["properties"]["peer"]["enum"] == ["mira"]


# -- strict arguments ---------------------------------------------------------


async def test_an_unknown_argument_returns_the_verbs_schema(world):
    with pytest.raises(catalog.ArgumentError) as exc:
        await catalog.dispatch("news", {"verb": "get", "args": {"id": "1", "bogus": 2}})
    payload = exc.value.payload
    assert payload["unknown"] == ["bogus"]
    assert payload["schema"]["required"] == ["id"]
    assert set(payload["schema"]["properties"]) == {"id", "fmt"}
    assert world.calls == []


async def test_an_unknown_argument_is_refused_on_the_flat_door_too(world):
    with pytest.raises(catalog.ArgumentError):
        await catalog.dispatch("news_get", {"nope": 1})


async def test_declared_and_underscore_arguments_pass(world):
    await catalog.dispatch("news", {"verb": "get", "args": {
        "id": "1", "fmt": "x", "_caller_pid": 7}})
    assert world.calls[0][1]["_caller_pid"] == 7


async def test_a_verb_with_no_params_schema_passes_anything_through(
        world, monkeypatch):
    rec = ServiceRecord(name="news", prefix="/svc/news", kind="service",
                        service_id="sid-news", api={"functions": [
                            {"name": "get", "tool": "news_get"}]})
    monkeypatch.setattr(catalog, "get_registry", lambda: _StubRegistry([rec]))
    await catalog.dispatch("news", {"verb": "get", "args": {"whatever": 1}})
    assert world.calls == [("get", {"whatever": 1})]


async def test_a_verb_argument_beside_the_envelope_is_an_error_not_dropped(world):
    with pytest.raises(catalog.ArgumentError) as exc:
        await catalog.dispatch("news", {"verb": "get", "id": "1"})
    assert exc.value.payload["unknown"] == ["id"]
    assert "inside args" in exc.value.payload["error"]


def test_http_answers_a_bad_argument_with_400_and_the_schema(world):
    from fastapi.testclient import TestClient

    from awm.gateway.server import app

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/invoke", json={"name": "news", "args": {
            "verb": "get", "args": {"zzz": 1}}})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["error_class"] == "UnknownArgument"
    assert detail["schema"]["required"] == ["id"]


# -- describe -----------------------------------------------------------------


async def test_describe_lists_every_visible_verb_and_takes_a_verbs_list(world):
    out = json.loads(await catalog.dispatch("news", {"verb": "describe"}))
    assert [v["verb"] for v in out["verbs"]] == ["get", "list"]
    out = json.loads(await catalog.dispatch(
        "news", {"verb": "describe", "args": {"verbs": ["list"]}}))
    assert [v["verb"] for v in out["verbs"]] == ["list"]
    with pytest.raises(ValueError, match="Unknown verb"):
        await catalog.dispatch(
            "news", {"verb": "describe", "args": {"verbs": ["list", "nope"]}})


def test_the_overview_ranks_by_use_and_shows_every_tool(world, tmp_path):
    for _ in range(5):
        usage.record("news", "list")
    usage.record("browser", "open")
    out = catalog.describe_overview(catalog=_services_only(catalog._domain_catalog()))
    rows = [(r["tool"], r["verb"]) for r in out["verbs"]]
    assert rows[0] == ("news", "list") and rows[1] == ("browser", "open")
    assert {r["tool"] for r in out["verbs"]} == {"news", "browser", "scope"}
    mine = _services_only(catalog._domain_catalog())
    get = next(r for r in catalog.describe_overview(limit=10, catalog=mine)["verbs"]
               if r["verb"] == "get")
    assert get["required"] == ["id"] and get["summary"] == "news_get does it."
    assert {t["tool"] for t in out["tools"]} - _NATIVE == {"news", "browser", "scope"}


def test_the_overview_cuts_at_the_limit_but_keeps_one_verb_per_tool(world):
    mine = _services_only(catalog._domain_catalog())
    out = catalog.describe_overview(limit=1, catalog=mine)
    assert {r["tool"] for r in out["verbs"]} == {"news", "browser", "scope"}
    assert out["omitted"] == 2
    full = catalog.describe_overview(limit=40, catalog=mine)
    assert len(full["verbs"]) == 5 and full["omitted"] == 0


# -- the usage tally ----------------------------------------------------------


async def test_dispatch_is_tallied_per_tool_and_verb(world):
    await catalog.dispatch("news", {"verb": "list"})
    await catalog.dispatch("news_list", {})
    await catalog.dispatch("news", {"verb": "describe"})
    assert usage.totals() == {("news", "list"): 2}


def test_the_first_run_seeds_the_tally_and_skips_describe(tmp_path, awm_workspace,
                                                         monkeypatch):
    seed = tmp_path / "seed.txt"
    seed.write_text("3 scope post\n1 scope describe\nbad line\n2 scope post\n")
    usage._reset_for_tests()
    monkeypatch.setattr(usage, "SEED_PATH", seed)
    assert usage.totals() == {("scope", "post"): 5}
    seed.write_text("100 scope post\n")
    usage._reset_for_tests()
    assert usage.totals() == {("scope", "post"): 5}, "seeded only once"
    usage._reset_for_tests()


def test_the_tally_persists_and_forgets_after_sixty_days(tmp_path, awm_workspace,
                                                         monkeypatch):
    import datetime as dt

    usage._reset_for_tests()
    monkeypatch.setattr(usage, "SEED_PATH", tmp_path / "none.txt")
    usage.record("a", "b")
    usage.flush()
    path = usage.storage_path()
    data = json.loads(path.read_text())
    old = (dt.date.today() - dt.timedelta(days=usage.WINDOW_DAYS + 1)).isoformat()
    data["days"][old] = {"a b": 50}
    path.write_text(json.dumps(data))
    usage._reset_for_tests()
    assert usage.totals() == {("a", "b"): 1}
    usage._reset_for_tests()


def test_renamed_pairs_fold_into_their_new_name(world):
    usage.record("scope", "post")
    usage.record("journal", "post")
    usage.set_renames({("scope", "post"): ("journal", "post")})
    assert usage.totals() == {("journal", "post"): 2}


def test_the_shipped_seed_parses():
    seed = usage.read_seed(Path(usage.__file__).with_name("usage_seed.txt"))
    assert seed["tether drain"] == 376 and not any(k.endswith(" describe") for k in seed)


# -- the CLI ------------------------------------------------------------------


def test_the_cli_groups_by_tool_for_a_renamed_and_an_unrenamed_service(world):
    tools = [t.model_dump(by_alias=True) for t in catalog.list_tools()]
    app = typer.Typer()
    groups = register_service_cli_commands(
        app, tools, api_func=lambda *a, **k: None, exclude_names=set())
    commands = {g: {c.name for c in typer_app.registered_commands}
                for g, typer_app in groups.items()}
    assert {"status", "open"} <= commands["browser"]
    assert {"get", "list", "purge"} <= commands["news"]
    assert "ask" in commands["hidden"]
    assert "search" in commands["scope"]
