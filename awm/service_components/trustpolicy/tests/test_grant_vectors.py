"""queue's grant vectors (approver/grantlib/test_vectors.json) pass the same way here."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from awm.trustpolicy import grants

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

VECTORS = json.loads((Path(__file__).parent / "grant_vectors.json").read_text())
KEY = base64.urlsafe_b64decode(VECTORS["public_key_b64u"] + "=" * (-len(VECTORS["public_key_b64u"]) % 4))


@pytest.mark.parametrize("case", [c for c in VECTORS["cases"] if c["check"] == "grant"], ids=lambda c: c["name"])
def test_vector(case):
    run = lambda: grants.verify(case["envelope"], KEY, kind=case.get("kind"),  # noqa: E731
                                node=case.get("node"), now=case["now"])
    if case["expect"] == "ok":
        run()
    else:
        with pytest.raises(grants.GrantError):
            run()


def test_key_id_matches():
    assert grants.key_id(KEY) == VECTORS["key_id"]
