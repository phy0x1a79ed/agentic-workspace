"""There is exactly one cert-minting module in the tree, and it is this one.

`mic` used to carry a near-copy, because it predated httpsfront and ran its own
off-host TLS listener. Two copies of a rule is one copy too many: they diverged
once already on the trust-consumer predicate, and `mic` — enabled by default on
every node — would have won the boot race on a node holding only ``ca.pem``, put
a signing key back, and swapped the fleet's root before httpsfront's own guard
ever fired.

The copy is gone and the audio rides the hub instead. The vigilance that watched
the two copies for divergence converts into this: httpsfront is the only service
in awm that binds an off-host port or touches the CA, so a second ``certs.py``
appearing under ``awm/services/`` means that doctrine broke somewhere.

Read as paths, not imports: each dist resolves the ``awm`` namespace to its own
source root, so importing another service's module from here would silently pick
up the deployed copy instead of this tree's — see gateway/scripts/run-tests.sh.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from awm.httpsfront import certs

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def test_httpsfront_is_the_only_service_that_mints_certs():
    here = Path(certs.__file__).resolve()   # .../services/httpsfront/awm/httpsfront/certs.py
    services = here.parents[3]              # .../awm/services
    assert services.name == "services", services

    found = sorted(services.glob("*/awm/*/certs.py"))
    assert found == [here], (
        "httpsfront is the only off-host listener in awm and the only holder of "
        "the CA; everything else rides the gateway behind it. A second copy of "
        f"the cert code appeared: {[str(p) for p in found if p != here]}"
    )
