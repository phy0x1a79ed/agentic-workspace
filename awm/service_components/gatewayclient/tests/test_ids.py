import json
from pathlib import Path

import pytest

from awm.gatewayclient.ids import BadName, BadSessionId, Name, SessionId

GOLDEN = json.loads((Path(__file__).parents[4] / "core/crates/awm-proto/ids_golden.json").read_text())


@pytest.mark.parametrize("row", GOLDEN["ids"], ids=lambda r: r["canonical"])
def test_id_forms(row):
    sid = SessionId.parse(row["canonical"])
    assert sid.canonical() == row["canonical"]
    assert sid.harness_native() == row["harness_native"]
    assert sid.job_dir() == row["job_dir"]
    assert SessionId.from_harness_native(sid.platform, row["harness_native"]) == sid


@pytest.mark.parametrize("bad", GOLDEN["bad_ids"])
def test_parse_takes_only_the_canonical_form(bad):
    with pytest.raises(BadSessionId):
        SessionId.parse(bad)


@pytest.mark.parametrize("row", GOLDEN["names"], ids=lambda r: r["name"])
def test_name_forms(row):
    name = Name.new(row["name"])
    assert name.rc() == row["rc"]
    assert all(name.matches(m) for m in row["matches"])
    assert not any(name.matches(m) for m in row["misses"])


@pytest.mark.parametrize("bad", GOLDEN["bad_names"])
def test_bad_names(bad):
    with pytest.raises(BadName):
        Name.new(bad)
