"""The sign-in page has to leave itself once the session is set.

It is served in place of whatever the browser asked for, so success means
loading that same URL again. The vault's URLs carry a fragment
(``/trilium/#root/...``), and ``location.replace(location.href)`` on such a URL
is a same-document navigation: nothing reloads and the person stays on the
sign-in screen with the button disabled.
"""

from __future__ import annotations

import pytest

from awm.httpsfront.pages import login_page

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.mark.parametrize("public", [True, False])
def test_success_reloads_rather_than_replacing_with_itself(public):
    page = login_page(public=public)
    assert "location.reload()" in page
    assert "location.href" not in page
