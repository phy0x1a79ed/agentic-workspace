"""Tests for the gateway-dist Pydantic envelope models (split out of the
monolith's ``unit/test_models.py``; feature models moved to their owning
dists)."""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.unit, pytest.mark.smoke]

from awm.gateway.models import StatusResponse


class TestStatusResponse:
    def test_defaults(self):
        r = StatusResponse(workspace_root="/tmp")
        assert r.status == "ok"
        assert r.active_scopes == 0
