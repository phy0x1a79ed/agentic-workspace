"""PTT V2 service — standalone FastAPI process forwarded by the hub.

The hub (`awm.exposed:app` on :7820) registers this service against the
``/ptt`` path prefix; matched requests are forwarded here. See
``awm/demos/echo_svc.py`` for the canonical starter shape.

Run::

    python -m awm.services.ptt --port 7853

Register with the hub once both are up::

    awm hub register --name ptt --prefix /ptt --url http://127.0.0.1:7853
"""

from awm.services.ptt.app import build_app

__all__ = ["build_app"]
