"""Test bootstrap for the agents dist.

The per-dist test runner (``awm/gateway/scripts/run-tests.sh``) puts only this
dist's source root + the shared components (config / persistence /
gatewayclient) on ``PYTHONPATH``. The agents service now imports
``awm.agentcore`` (the harness layer), which is a leaf component living under
``awm/service_components/agentcore`` — NOT in that component set. Add its source
root here so ``import awm.agentcore`` resolves under the agents dist's
interpreter.

This is import-path-only (no namespace shadowing risk): agentcore ships under
the same PEP 420 ``awm`` namespace and provides only the ``awm.agentcore``
subpackage, which the agents source root does not also provide.
"""

from __future__ import annotations

import sys
from pathlib import Path

_AGENTCORE_SRC = (
    Path(__file__).resolve().parents[4]
    / "service_components" / "agentcore"
)
if _AGENTCORE_SRC.is_dir():
    p = str(_AGENTCORE_SRC)
    if p not in sys.path:
        sys.path.insert(0, p)
