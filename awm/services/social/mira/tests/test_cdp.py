"""CDPPage.evaluate's retry policy, with the transport stubbed out."""

from __future__ import annotations

import pytest

from mira_api.cdp import CDPError, CDPPage


class _FlakyPage(CDPPage):
    def __init__(self, fails: int):
        super().__init__("http://x", "needle")
        self.fails = fails
        self.sent = 0
        self.timeouts: list = []

    async def _connect(self):
        return None

    async def _command(self, method, params=None, timeout=None):
        self.sent += 1
        self.timeouts.append(timeout)
        if self.sent <= self.fails:
            raise CDPError(f"{method}: timed out")
        return {"result": {"value": "ok"}}


async def test_default_reruns_once_after_a_transport_error():
    page = _FlakyPage(fails=1)
    assert await page.evaluate("1") == "ok"
    assert page.sent == 2


async def test_retry_false_runs_exactly_once():
    page = _FlakyPage(fails=1)
    with pytest.raises(CDPError):
        await page.evaluate("chunk()", retry=False, timeout=180)
    assert page.sent == 1 and page.timeouts == [180]
