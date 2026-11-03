"""Hop steps, the author filter and `kb hop`, over hop arrays written here as kb.hops writes them."""

import json

import numpy as np
import pytest

from awm.kb import fastlayer, hops, hub_adapter

from .test_fastlayer import _base


def _csr(pairs, n):
    rows = [[] for _ in range(n)]
    for r, c in pairs:
        if c not in rows[r]:
            rows[r].append(c)
    ptr = np.cumsum([0] + [len(r) for r in rows]).astype(np.int64)
    return ptr, np.array([c for r in rows for c in sorted(r)], dtype=np.int32)


def write_hops(root, papers, authored=(), cites=(), prior=None, names=None, dates=None):
    """Hop arrays for ``papers`` (paper ids), ``authored`` (paper, author key) and ``cites`` (citing, cited).
    A ``prior`` writes the arrays a build before 2026-11-03 also held."""
    root.mkdir(parents=True, exist_ok=True)
    ids = sorted(papers)
    row = {p: i for i, p in enumerate(ids)}
    authors = sorted({a for _, a in authored})
    arow = {a: i for i, a in enumerate(authors)}
    pa = [(row[p], arow[a]) for p, a in authored]
    ct = [(row[s], row[d]) for s, d in cites]
    by_surname = {}
    for p, people in (names or {}).items():
        for person in people:
            by_surname.setdefault(hops.fold(person.split(",")[0] if "," in person else person.split()[-1]),
                                  set()).add(row[p])
    sn = sorted(by_surname)
    tri = {}
    for j, s in enumerate(sn):
        for g in hops._grams(s):
            tri.setdefault(g, []).append(j)
    tn = sorted(tri)
    arrays = {
        "paper_id": np.array(ids, dtype=f"S{max(len(i) for i in ids)}"),
        "date": np.array([(dates or {}).get(p, 20200101) for p in ids], dtype=np.int32),
        **dict(zip(("pa_ptr", "pa_idx"), _csr(pa, len(ids)))),
        **dict(zip(("ap_ptr", "ap_idx"), _csr([(a, p) for p, a in pa], len(authors)))),
        **dict(zip(("ct_ptr", "ct_idx"), _csr(ct, len(ids)))),
        **dict(zip(("cb_ptr", "cb_idx"), _csr([(d, s) for s, d in ct], len(ids)))),
        "surname": np.array(sn or [""], dtype=f"S{max([len(s) for s in sn] or [1])}"),
        **dict(zip(("sp_ptr", "sp_idx"), _csr([(sn.index(s), r) for s in sn for r in by_surname[s]], len(sn)))),
        "trigram": np.array(tn or [""], dtype="S3"),
        **dict(zip(("ts_ptr", "ts_idx"), _csr([(tn.index(g), j) for g in tn for j in tri[g]], len(tn)))),
    }
    if prior is not None:
        arrays["prior"] = np.array([prior.get(p, 0.001) for p in ids], dtype=np.float32)
        arrays["trajectory"] = np.full(len(ids), np.nan, dtype=np.float32)
    for name, arr in arrays.items():
        np.save(root / f"{name}.npy", arr)
    (root / "meta.json").write_text(json.dumps({"papers": len(ids)}))
    return hops.Hops(root)


#: A group of three papers by K1 and K2, a stranger S1's paper, and a paper by S1 and K2 from 2018.
GROUP = {"g1": ["K1", "K2"], "g2": ["K1"], "g3": ["K2"], "s1": ["S1"], "old": ["S1", "K2"]}
DATES = {"g1": 20240301, "g2": 20230101, "g3": 20250601, "s1": 20240101, "old": 20180101}


def _group(tmp_path):
    authored = [(p, a) for p, people in GROUP.items() for a in people]
    return write_hops(tmp_path / "hops", list(GROUP), authored=authored, dates=DATES,
                      cites=[("g1", "s1"), ("s1", "old")])


def test_an_authors_hop_finds_the_seeds_coauthors_papers_and_leaves_the_seeds_out(tmp_path):
    h = _group(tmp_path)
    got = [p for p, _ in h.hop(["g1"], "authors")]
    assert got[0] == "g2" and set(got[1:3]) == {"g3", "old"} and "g1" not in got
    assert got[-1] == "s1"  # reached only through old's other author, two round trips out
    assert [p for p, _ in h.hop(["g1"], "authors", after="2024-01-01")] == ["g3", "s1"]
    assert [p for p, _ in h.hop(["g1"], "authors", before="2019-01-01")] == ["old"]


def test_a_wrong_first_seed_leaves_the_next_seeds_papers_in_the_answer(tmp_path):
    h = _group(tmp_path)
    got = dict(h.hop(["s1", "g1"], "authors"))
    assert "g2" in got and "g3" in got


def test_an_earlier_seed_weighs_more_and_equal_walks_go_to_the_newest(tmp_path):
    h = _group(tmp_path)
    first = [p for p, _ in h.hop(["g2", "g3"], "authors")]
    assert first.index("g1") == 0  # both seeds' coauthor
    assert [p for p, _ in h.hop(["g2", "s1"], "authors")][:2] == ["g1", "old"]
    tie = write_hops(tmp_path / "tie", ["a", "x", "y"], authored=[("a", "A"), ("x", "A"), ("y", "A")],
                     dates={"x": 20190101, "y": 20250101})
    assert [p for p, _ in tie.hop(["a"], "authors")] == ["y", "x"]


def test_arrays_from_a_build_with_a_prior_still_hop(tmp_path):
    old = write_hops(tmp_path / "old", ["a", "x"], authored=[("a", "A"), ("x", "A")], prior={"x": 0.5})
    assert [p for p, _ in old.hop(["a"], "authors")] == ["x"]


def test_citation_hops_follow_the_edges_direction(tmp_path):
    h = _group(tmp_path)
    assert [p for p, _ in h.hop(["g1"], "cites")][0] == "s1"
    assert [p for p, _ in h.hop(["s1"], "cited_by")] == ["g1"]
    with pytest.raises(ValueError):
        h.hop(["g1"], "wrote")


def test_an_author_filter_matches_a_misspelt_surname_on_surnames_only(tmp_path):
    h = write_hops(tmp_path / "hops", ["a", "b", "c", "d"],
                   names={"a": ["Curtis Huttenhower"], "b": ["Küsel, Kirsten"], "c": ["Hutton, James"],
                          "d": ["A. Kosel"]})
    assert h.surnames("Huttenhauer") == ["huttenhower"]
    assert h.by_author("Huttenhauer") == {"a"} and h.by_author("K. Kusel") == {"b"}
    assert h.by_author("Curtis") == set() and h.by_author("Curtis Huttenhower") == {"a"}
    assert h.surnames("Küsel") == ["kusel"]  # an exact surname leaves out Kosel, one letter away


@pytest.fixture
def served(tmp_path, monkeypatch):
    papers = {p: (f"10.1/{p}", f"Paper {p} on microbial selection", 2020, f"Abstract of {p}.", "")
              for p in GROUP}
    _base(tmp_path / "papers.db", papers)
    monkeypatch.setattr(hub_adapter, "PAPERS", fastlayer.FastLayer(tmp_path / "papers.db", tmp_path / "d.db"))
    monkeypatch.setattr(hub_adapter, "HOPS", _group(tmp_path))

    async def no_entries(text):
        return None, "no encoder"
    monkeypatch.setattr(hub_adapter, "_entries", no_entries)


async def test_a_query_answers_in_two_sections_and_a_hop_is_its_own_call(served):
    got = await hub_adapter.HANDLERS["query"]({"query": "Paper g1 on"})
    assert got["exact"][0]["id"] == "g1" and set(got) & {"exact", "related", "graph"} == {"exact", "related"}
    hopped = await hub_adapter.HANDLERS["hop"]({"ids": [h["id"] for h in got["exact"][:1]], "limit": 5})
    assert {h["id"] for h in hopped["hits"]} >= {"g2", "g3"} and all(h["match"] == "authors" for h in hopped["hits"])
    # g1 cites s1, which cites old; then their authors' papers over three round trips, s1 and old left out
    cited = await hub_adapter.HANDLERS["hop"]({"ids": ["g1"], "along": "cites"})
    chained = await hub_adapter.HANDLERS["hop"]({"ids": [h["id"] for h in cited["hits"]], "along": "authors"})
    assert [h["id"] for h in cited["hits"]] == ["s1", "old"]
    assert {h["id"] for h in chained["hits"]} == {"g1", "g2", "g3"}
    with pytest.raises(ValueError, match="along must be"):
        await hub_adapter.HANDLERS["hop"]({"ids": ["g1"], "along": "wrote"})


async def test_kb_hop_chains_from_ids(served):
    got = await hub_adapter.HANDLERS["hop"]({"ids": ["g1"], "along": "cited_by"})
    assert got["hits"] == []
    got = await hub_adapter.HANDLERS["hop"]({"ids": "s1", "along": "cited_by"})
    assert [h["id"] for h in got["hits"]] == ["g1"] and got["hits"][0]["passages"][0]["field"] == "abstract"


async def test_the_author_filter_narrows_every_section(served, tmp_path, monkeypatch):
    h = write_hops(tmp_path / "hops2", list(GROUP), names={"g2": ["Ann Lee"], "g3": ["Bo Lee"], "g1": ["X Y"]})
    monkeypatch.setattr(hub_adapter, "HOPS", h)
    got = await hub_adapter.HANDLERS["query"]({"query": "microbial selection", "author": "Lee"})
    assert {x["id"] for x in got["exact"] + got["related"]} == {"g2", "g3"}
    none = await hub_adapter.HANDLERS["query"]({"query": "microbial selection", "author": "Nobodyatall"})
    assert none["exact"] == none["related"] == []
