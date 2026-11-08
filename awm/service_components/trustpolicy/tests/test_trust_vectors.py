"""trustpolicy still gives the answers awm-trust checks against (trust_vectors.json)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

import awm.trustpolicy as tp

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

VECTORS = json.loads((Path(__file__).resolve().parents[4]
                      / "core/crates/awm-trust/tests/trust_vectors.json").read_text())
PUB = base64.urlsafe_b64decode(VECTORS["public_key_b64u"] + "=")


def test_constants():
    assert list(tp.LEVELS) == VECTORS["levels"]
    assert sorted(tp.STEER_VERBS) == VECTORS["steer_verbs"]
    assert tp.FIRST_LOCAL_MESSAGE_HINT == VECTORS["first_local_message_hint"]


@pytest.mark.parametrize("case", VECTORS["starts"])
def test_start(case):
    grant = case["grant"]
    run = lambda: tp.level_for_start(case["starter"], case["requested"],  # noqa: E731
                                     session_id=case["session_id"], grant=grant,
                                     grant_key=PUB if grant else None, node=case["node"],
                                     now=case["now"])
    if "level" in case:
        assert run() == case["level"]
    else:
        with pytest.raises(tp.TrustError) as exc:
            run()
        assert str(exc.value) == case["error"]


@pytest.mark.parametrize("case", VECTORS["steers"])
def test_steer(case):
    assert list(tp.may_steer(case["actor"], case["verb"], target_level=case["target"])) == [
        case["ok"], case["why"]]


@pytest.mark.parametrize("case", VECTORS["bounds"])
def test_bound(case):
    edges = VECTORS["edges"]["edges"]
    b = tp.journal_bound(case["level"], node=case["node"], project=case["project"],
                         scope=case["scope"], reachable=lambda n: sorted(edges.get(n, {})))
    assert (sorted(b.read_nodes) if b.read_nodes is not None else None) == case["read_nodes"]
    assert (b.read_scope, b.write_scope) == (case["read_scope"], case["write_scope"])
    assert [b.may_read(n, p, s) for n, p, s, _ in case["reads"]] == [ok for *_, ok in case["reads"]]
    assert [b.may_write(p, s) for p, s, _ in case["writes"]] == [ok for *_, ok in case["writes"]]
