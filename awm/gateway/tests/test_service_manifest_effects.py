"""Every service manifest verb declares a valid effect and a valid grant category.

The services run in their own processes with their own dependencies, so this
reads each ``hub_adapter.py`` statically instead of importing it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from awm.config import EFFECTS, GRANT_CATEGORIES

pytestmark = [pytest.mark.unit]

SERVICES = Path(__file__).resolve().parents[2] / "services"

# Services whose declarations another owner maintains; they are still validated.
_OTHER_OWNERS = {"cx", "board", "httpsfront", "scopes"}


def _literal(node: ast.AST | None):
    try:
        return ast.literal_eval(node) if node is not None else None
    except ValueError:
        return None


def _function_entries(tree: ast.AST) -> list[dict]:
    """Manifest function entries: dict literals with a ``name`` and no ``type``,
    ``Operation(...)`` calls, and calls to a module-level ``_fn(name, description, effect)``."""
    entries: list[dict] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = {k.value: v for k, v in zip(node.keys, node.values)
                    if isinstance(k, ast.Constant)}
            name = _literal(keys.get("name"))
            if (isinstance(name, str) and "type" not in keys
                    and ("params" in keys or "description" in keys or len(keys) == 1)):
                entries.append({"name": name,
                                "effect": _literal(keys.get("effect")),
                                "category": _literal(keys.get("category")),
                                "line": node.lineno})
        elif isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Operation":
            kw = {k.arg: k.value for k in node.keywords}
            entries.append({"name": _literal(kw.get("name")),
                            "effect": _literal(kw.get("effect")),
                            "category": _literal(kw.get("category")),
                            "line": node.lineno})
        elif isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_fn" and node.args:
            kw = {k.arg: k.value for k in node.keywords}
            effect = _literal(node.args[2]) if len(node.args) > 2 else _literal(kw.get("effect"))
            entries.append({"name": _literal(node.args[0]), "effect": effect or "write",
                            "category": None, "line": node.lineno})
    return entries


def _manifests() -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}
    for path in (sorted(SERVICES.glob("*/awm/*/hub_adapter.py"))
                 + sorted(SERVICES.glob("*/awm/*/service.py"))
                 + sorted(SERVICES.glob("*/awm/*/operations.py"))
                 + sorted(SERVICES.glob("scopes/awm/scopes/operations/*.py"))):
        entries = _function_entries(ast.parse(path.read_text()))
        if entries:
            found.setdefault(path.parts[len(SERVICES.parts)], []).extend(
                {**e, "file": str(path.relative_to(SERVICES))} for e in entries)
    return found


MANIFESTS = _manifests()


def test_manifests_found():
    assert len(MANIFESTS) >= 30
    assert sum(len(v) for v in MANIFESTS.values()) >= 300


def test_declared_effects_are_valid():
    bad = [(e["file"], e["name"], e["effect"]) for es in MANIFESTS.values()
           for e in es if e["effect"] is not None and e["effect"] not in EFFECTS]
    assert not bad, bad


def test_declared_categories_are_grantable():
    bad = [(e["file"], e["name"], e["category"]) for es in MANIFESTS.values()
           for e in es if e["category"] is not None and e["category"] not in GRANT_CATEGORIES]
    assert not bad, bad


def test_only_read_verbs_carry_a_category():
    bad = [(e["file"], e["name"], e["effect"]) for es in MANIFESTS.values()
           for e in es if e["category"] is not None and e["effect"] != "read"]
    assert not bad, bad


def test_every_verb_declares_an_effect():
    missing = [(e["file"], e["name"]) for svc, es in MANIFESTS.items()
               if svc not in _OTHER_OWNERS for e in es if e["effect"] is None]
    assert not missing, missing


def test_kb_reads_carry_the_kb_category():
    kb = {e["name"]: e for e in MANIFESTS["kb"]}
    assert kb["recall"]["category"] == "kb" and kb["recall"]["effect"] == "read"
    assert kb["status"]["category"] == "kb"
    assert kb["start"]["category"] is None


def test_no_category_outside_kb_and_scopes():
    """Any other read verb stays unreachable by a grant until a category is added."""
    leaked = [(svc, e["name"]) for svc, es in MANIFESTS.items()
              if svc not in ("kb", "scopes") for e in es if e["category"] is not None]
    assert not leaked, leaked


def test_operation_files_are_scanned():
    files = {e["file"] for es in MANIFESTS.values() for e in es}
    assert any(f.startswith("scopes/awm/scopes/operations/") for f in files)


def test_relabelled_verbs_are_write():
    by = {(svc, e["name"]): e["effect"] for svc, es in MANIFESTS.items() for e in es}
    assert by[("stt", "transcribe")] == "write"
    assert by[("rlm-browser", "commands")] == "write"
    assert by[("rlm-browser", "observe")] == "write"


def test_gateway_native_reads():
    from awm.gateway.gateway_ops import GATEWAY_OPERATIONS
    reads = {op.name for op in GATEWAY_OPERATIONS if op.effect == "read"}
    assert reads == {"awm_status", "gateway_list", "services_list", "peer_list",
                     "peer_resolve", "peer_providers", "config_contracts"}
    assert all(op.category is None for op in GATEWAY_OPERATIONS)
