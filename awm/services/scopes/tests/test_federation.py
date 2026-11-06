"""Federation surface of scopes: no verb carries a category."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.scopes]


# ---------------------------------------------------------------------------
# No verb carries a category
# ---------------------------------------------------------------------------

def _functions():
    from awm.scopes.hub_adapter import API_MANIFEST
    return {f["name"]: f for f in API_MANIFEST["functions"]}


def test_no_scopes_verb_carries_a_category():
    assert {n for n, f in _functions().items() if "category" in f} == set()
