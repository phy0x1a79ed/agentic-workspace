"""Federation surface of scopes: every verb declares an effect, and none carries a category."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.scopes]


# ---------------------------------------------------------------------------
# Every verb declares an effect, and none carries a category
# ---------------------------------------------------------------------------

def _functions():
    from awm.scopes.hub_adapter import API_MANIFEST
    return {f["name"]: f for f in API_MANIFEST["functions"]}


def test_every_scopes_verb_declares_an_effect():
    from awm.config import EFFECTS
    missing = [n for n, f in _functions().items() if f.get("effect") not in EFFECTS]
    assert missing == []


READS = {"scope_search", "resolveScope", "resolveRef"}


def test_only_the_known_reads_are_reads():
    reads = {n for n, f in _functions().items() if f["effect"] == "read"}
    assert reads == READS


def test_no_scopes_verb_carries_a_category():
    assert {n for n, f in _functions().items() if "category" in f} == set()
