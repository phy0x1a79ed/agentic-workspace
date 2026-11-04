"""A call the gateway did not stamp is untrusted (fleet.md R103 item 2)."""
from __future__ import annotations

import pytest

from awm.journal import bounds

pytestmark = [pytest.mark.journal, pytest.mark.unstamped]


def test_an_unstamped_call_is_untrusted_and_needs_its_own_scope():
    with pytest.raises(bounds.Refused, match="untrusted session"):
        bounds.of({}, None)
    bound = bounds.of({"_caller_scope": "awm/dev"}, None)
    assert bound.write_scope is not None


def test_a_stamped_trusted_call_is_unbounded():
    assert bounds.of({"_caller_trust": "trusted"}, None).write_scope is None
