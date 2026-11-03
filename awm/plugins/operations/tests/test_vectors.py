import json
from pathlib import Path

import pytest

from verify_grant import GrantInvalid, load_public_key, verify_grant, verify_order

VECTORS = json.loads((Path(__file__).parents[1] / "grantlib" / "test_vectors.json").read_text())


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["name"])
def test_vector(case):
    pub = load_public_key(VECTORS["public_key_b64u"])
    if case["check"] == "grant":
        run = lambda: verify_grant(case["envelope"], pub, kind=case.get("kind"),  # noqa: E731
                                   node=case.get("node"), now=case["now"])
    else:
        run = lambda: verify_order(case["envelope"], pub, node=case["node"], now=case["now"])  # noqa: E731
    if case["expect"] == "ok":
        run()
    else:
        with pytest.raises(GrantInvalid, match=case["expect"]):
            run()
