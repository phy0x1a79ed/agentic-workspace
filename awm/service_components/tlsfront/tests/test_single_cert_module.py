"""There is exactly one cert-minting module in the tree, and it is this one.

`mic` used to carry a near-copy, because it predated the shared front and ran
its own off-host TLS listener. Two copies of a rule is one copy too many: they
diverged once already on the trust-consumer predicate, and `mic` — enabled by
default on every node — would have won the boot race on a node holding only
``ca.pem``, put a signing key back, and swapped the fleet's root before the
front's own guard ever fired.

Every off-host listener in awm (httpsfront, dsh, claude-science) mints through
this library, so a ``certs.py`` appearing under ``awm/services/`` or beside this
one in ``awm/service_components/`` means that doctrine broke somewhere.

Read as paths, not imports: each dist resolves the ``awm`` namespace to its own
source root, so importing another dist's module from here would silently pick up
the deployed copy instead of this tree's — see the run-tests script.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from awm.tlsfront import certs

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def test_tlsfront_is_the_only_module_that_mints_certs():
    here = Path(certs.__file__).resolve()   # .../service_components/tlsfront/awm/tlsfront/certs.py
    components = here.parents[3]            # .../awm/service_components
    assert components.name == "service_components", components
    awm_root = components.parent

    found = sorted([*(awm_root / "services").glob("*/awm/*/certs.py"),
                    *components.glob("*/awm/*/certs.py")])
    assert found == [here], (
        "awm.tlsfront is the only holder of the cert code; every off-host "
        f"listener mints through it. A second copy appeared: "
        f"{[str(p) for p in found if p != here]}"
    )
