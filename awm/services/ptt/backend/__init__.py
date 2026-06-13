"""PTT backend — the supervised process for the ``ptt`` service.

Discovered + spawned by ``awm packages sync`` from
``packages/services/ptt/start.sh``, which execs the control-WS adapter
(``python -m backend.hub_adapter``). The hub injects ``AWM_HUB_URL``,
``AWM_HUB_TOKEN``, ``AWM_SERVICE_NAME`` and (after restart) ``AWM_SERVICE_ID``.
Browser-side, calls go to ``/svc/ptt/{fn,session,emit}/*`` and the hub
relays them across the control + bridge WSes.
"""

from backend.app import build_app

__all__ = ["build_app"]
