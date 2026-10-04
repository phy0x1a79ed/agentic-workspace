"""The mechanical half of the edge-only invariant.

Ugly, and correct. The vault runs with its own authentication off, so the whole
design rests on no awm code ever binding that child to anything but loopback.
The per-person TLS front that used to do exactly that is deleted; this is what
notices if somebody re-derives it, which is a much likelier way for it to come
back than a deliberate decision.
"""

from __future__ import annotations

import pathlib

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "awm" / "trilium"

#: Each of these would mean a listener this service owns, which is the thing
#: that must not exist. `proxy.serve` is how the retired front bound one.
FORBIDDEN = ["0.0.0.0", "proxy.serve", "TRILIUM_FRONTS", "FRONT_PORT"]


def _code(path: pathlib.Path) -> str:
    """The file with its comments and docstrings stripped, roughly.

    Prose *about* the retired listener is exactly what we want in these files —
    the reason it is gone is worth writing down. Only an occurrence in code is
    a defect, so the naive grep would fail on its own explanation.
    """
    out, in_doc = [], False
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.count('"""') == 1:
            in_doc = not in_doc
            continue
        if in_doc or stripped.startswith("#") or stripped.startswith('"""'):
            continue
        out.append(line.split("  #", 1)[0])
    return "\n".join(out)


@pytest.mark.parametrize("needle", FORBIDDEN)
def test_the_service_binds_no_public_listener(needle):
    offenders = [p.name for p in PACKAGE.glob("*.py") if needle in _code(p)]
    assert not offenders, (
        f"{needle!r} is back in {offenders}. The vault's child runs with "
        f"Trilium's own authentication disabled, which is only safe while the "
        f"awm edge is the only route to it — see server.child_env.")
