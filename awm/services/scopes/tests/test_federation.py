"""Federation surface of scopes: every verb declares an effect, and no read carries a federation category."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.scopes]


# ---------------------------------------------------------------------------
# Every verb declares an effect; no scopes verb is open to a foreign caller
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


def test_no_scopes_verb_is_open_to_a_foreign_caller():
    assert {n for n, f in _functions().items() if f.get("category")} == set()
