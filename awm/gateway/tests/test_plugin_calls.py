"""Each plugin's manifest lists every tool and verb its code calls (install.md R116 item 3).

The gateway refuses a call its plugin's manifest does not list, so a missed verb breaks
the plugin at runtime. The record of each plugin's calls is the literal calls read from
its code, plus the calls a variable names, kept in ``CALLS``. Each record is replayed
through the guard the gateway runs, over the real tree. A declared verb that no record
holds fails too, so the record and the manifest stay one list.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

from awm.gateway.hub import discovery

AWM = Path(__file__).resolve().parents[2]
FOLDERS = sorted(p.parent for root in ("services", "plugins") for p in (AWM / root).glob(f"*/{discovery.PLUGIN_MANIFEST}"))

#: gatewayclient calls, by the index of their tool argument.
CALL_FNS = {"call": 0, "call_sync": 0}

#: Calls the scan cannot read, because a variable names the tool or the verb, or a shared
#: library makes them for the plugin. ``harness@1`` names a contract.
CALLS: dict[str, set[tuple[str, str]]] = {
    # trustpolicy.requests.file_request calls operations approval_request.
    "browser": {("operations", "approval_request")},
    "board": {("operations", "intake_post"), ("operations", "intake_message"),  # hub_adapter._operations
              *{("trilium", v) for v in ("note_get", "note_search", "note_create", "note_update", "note_place",
                                         "attr_set", "attr_delete")}},  # vault.gateway_transport
    "channel": {("reflection", "session_of"), ("reflection", "message"), ("reflection", "list_other")},
    # gatewayclient sessions and push
    "operations": {  # GatewayTool("<tool>").call(verb)
        ("reflection", "session_of"), ("reflection", "message"), ("reflection", "start_other"), ("social", "send"),
        *{("2fa", v) for v in ("burst", "approve_all", "approve", "deny", "devices", "pending")},
        *{("trilium", v) for v in ("note_get", "note_create", "note_update", "attr_delete", "attr_set", "note_search",
                                   "note_children", "note_delete")},
    },
    "routine": {("reflection", "start_other"), ("reflection", "session_of"), ("social", "send"), ("journal", "post"),
                ("operations", "task_for_session"), ("operations", "swarm_message_post"), ("routine", "refresh"),
                ("routine", "deploy")},  # Outbound.invoke
    "wake": {("reflection", "start_other"), ("reflection", "message"), ("harness@1", "revive_pinned"), ("reflection", "revive_pinned"),
             ("wake", "standby_take"), ("wake", "standby_release")},  # Gateway.call
    "claude-science": {("scope", "search"), ("scope", "resolve"), ("journal", "fetch"), ("search", "query"), ("search", "get"),
                       ("dvc", "coverage"), ("dvc", "scope_status")},  # mcp_bridge DEFAULT_ALLOWLIST
    "httpsfront": {("trilium", "slice_resolve")},  # proxy.py posts /invoke itself
    "journal": {("reflection", "session_of")},  # gatewayclient.sessions
    "reflection": {
        ("scope", "claim"), ("scope", "release"), ("journal", "post"),  # _scopes_call, alarm
        *{("harness@1", v) for v in ("platform", "status", "resolve", "find", "list", "start", "stop", "resume", "adopt", "send",
                                     "compact", "mode", "set", "interrupt", "remote_control", "link", "pending")},
    },
    "search": {("journal", "export"), ("kb", "query"), ("kb", "library"), ("kb", "get"), ("kb", "hop"),
               ("transcripts", "query"), ("search", "query"), ("search", "get")},  # the archive sources, their hop,
    # and federation. search's tests/test_needs.py replays these against the real manifests.
    "social": {("reflection", "session_of"), ("reflection", "message"), ("reflection", "list_other"),
               *{("harness-cc", v) for v in ("list", "probe", "start", "resume", "adopt")}},  # push, sessions, Cx._call
    "trilium": {("operations", "approval_request")},  # trustpolicy.requests
    "zotero": {("trilium", v) for v in ("note_search", "note_get", "note_upsert", "note_create", "note_update", "attr_set",
                                        "note_place", "attachment_put", "note_delete")}  # vault.call_sync(SERVICE, fn)
              | {("operations", "order_pull"), ("operations", "order_done")},  # orders.py operations_call
}

#: Recorded calls no manifest can list yet, since no plugin serves them, each with why.
UNSERVED: dict[tuple[str, str, str], str] = {
    ("wake", "reflection", "revive_pinned"): "boot.py's fallback; reflection serves no revive_pinned",
}


def _manifest(folder: Path) -> dict:
    return tomllib.loads((folder / discovery.PLUGIN_MANIFEST).read_text())


def _literal_calls(folder: Path) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for py in sorted((folder / "awm").rglob("*.py")):
        for node in ast.walk(ast.parse(py.read_text())):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            at = CALL_FNS.get(name)
            if at is None or len(node.args) < at + 2:
                continue
            tool, verb = node.args[at], node.args[at + 1]
            if all(isinstance(a, ast.Constant) and isinstance(a.value, str) for a in (tool, verb)):
                found.add((tool.value, verb.value))
    return found


def _recorded(folder: Path) -> set[tuple[str, str]]:
    calls = _literal_calls(folder) | CALLS.get(folder.name, set())
    return {c for c in calls if (folder.name, *c) not in UNSERVED}


def _contract_providers() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for folder in FOLDERS:
        for tool in _manifest(folder).get("tool", []):
            for c in tool.get("implements", []):
                out.setdefault(c, set()).add(tool["name"])
    return out


@pytest.fixture()
def tree(monkeypatch):
    monkeypatch.delenv("AWM_SERVICES_DIR", raising=False)
    monkeypatch.delenv("AWM_SERVICE_ROOTS", raising=False)


@pytest.mark.parametrize("folder", FOLDERS, ids=lambda f: f.name)
def test_the_gateway_admits_each_recorded_call(folder, tree):
    providers = _contract_providers()
    refused = []
    for tool, verb in sorted(_recorded(folder)):
        for target in sorted(providers.get(tool, set())) if "@" in tool else [tool]:
            if why := discovery.needs_refusal(folder.name, target, verb):
                refused.append(why)
    assert not refused, "\n".join(refused)


@pytest.mark.parametrize("folder", FOLDERS, ids=lambda f: f.name)
def test_each_declared_verb_is_a_recorded_call(folder):
    needs = _manifest(folder).get("needs", {})
    declared = {(d["name"], v) for d in needs.get("tool", []) for v in d["verbs"]}
    declared |= {(f"{d['name']}@{d.get('major', 1)}", v) for d in needs.get("contract", []) for v in d["verbs"]}
    stale = declared - _recorded(folder)
    assert not stale, f"{folder.name} declares calls its code does not make: {sorted(stale)}"

