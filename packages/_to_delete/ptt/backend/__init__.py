"""PTT backend — the supervised process for the @awm/ptt stripe.

Run via the hub::

    awm stripe register --package packages/ptt

The hub allocates a port from ``AWM_STRIPE_PORT_POOL``, spawns
``python -m backend --port <port>`` with cwd ``packages/ptt/``, polls
``/healthz`` until ready, then proxies ``/ptt/_api/*`` here.
"""

from backend.app import build_app

__all__ = ["build_app"]
