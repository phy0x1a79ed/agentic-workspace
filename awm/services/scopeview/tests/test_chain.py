"""The chain contract: what check reports, when a node goes stale, and reading an old ref through the DVC cache."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from awm.scopeview import chain, layout, trace
from awm.scopeview.hub_adapter import API_MANIFEST, HANDLERS
from awm.scopeview.tree import Tree

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def sh(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def pin_dir(root: Path, cache: Path, name: str, files: dict[str, bytes]):
    """Write data/<name>/ and its pin, and fill the cache, as `dvc add` would."""
    listing = []
    for rel, data in sorted(files.items()):
        md5 = hashlib.md5(data).hexdigest()
        obj = cache / "files" / "md5" / md5[:2] / md5[2:]
        obj.parent.mkdir(parents=True, exist_ok=True)
        obj.write_bytes(data)
        (root / "data" / name / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / "data" / name / rel).write_bytes(data)
        listing.append({"md5": md5, "relpath": rel})
    body = json.dumps(listing).encode()
    dmd5 = hashlib.md5(body).hexdigest() + ".dir"
    obj = cache / "files" / "md5" / dmd5[:2] / dmd5[2:]
    obj.parent.mkdir(parents=True, exist_ok=True)
    obj.write_bytes(body)
    (root / "data" / f"{name}.dvc").write_text(f"outs:\n- md5: {dmd5}\n  path: {name}\n")


@pytest.fixture
def scope(tmp_path):
    root, cache = tmp_path / "scope", tmp_path / "cache"
    root.mkdir()
    sh(root, "init", "-q", "-b", "study")
    sh(root, "config", "user.email", "t@example.org")
    sh(root, "config", "user.name", "t")
    (root / ".dvc").mkdir()
    (root / ".dvc" / "config.local").write_text(f"[cache]\n    dir = {cache}\n")
    (root / ".gitignore").write_text("/data/ds\n/data/chain\n.dvc/config.local\n")
    pin_dir(root, cache, "ds", {
        "samples.tsv": b"sample_id\nS1\n",
        "tables/mags.tsv": b"bin\tphylum\tcompleteness\nS1.a.001\tBacteroidota\t90\nS1.a.002\tChloroflexota\t60\nS1.a.003\tBacteroidota\t95\n",
        "binning/checkm2_a/S1.tsv": b"bin\tc\n",
        "assembly/stats_a/S1.json": b'{"n50": 1200}',
        "_logs/gaps.tsv": b"product\tstem\treason\ncheckm2_a\tS2\tnot_run\n",
    })
    sh(root, "add", "-A")
    sh(root, "commit", "-qm", "data")
    return root, cache


def test_a_path_from_table_to_prose_checks_clean(scope):
    root, _ = scope
    q = chain.new(root, "question", ["table:ds/tables/mags.tsv"], "How complete are the MAGs?")
    i = chain.new(root, "interpretation", [q["id"]], "Mostly complete")
    chain.new(root, "point", [i["id"]], "Most MAGs pass 90%")
    g = chain.build(Tree(root), gate=True)
    assert g["ok"], g["problems"]
    kinds = {n["id"]: n.get("kind") for n in g["nodes"] if n["stage"] == "table"}
    assert kinds["table:ds/tables/mags.tsv"] == "table"
    assert kinds["table:ds/binning/checkm2_a"] == "product"
    product = next(n for n in g["nodes"] if n["id"] == "table:ds/binning/checkm2_a")
    assert product["gaps"] == [{"product": "checkm2_a", "stem": "S2", "reason": "not_run"}]


def test_a_nodes_provenance_lays_out_its_ancestors_and_children(scope):
    root, _ = scope
    q = chain.new(root, "question", ["table:ds/tables/mags.tsv"], "Q")
    i = chain.new(root, "interpretation", [q["id"]], "I")
    p = chain.new(root, "point", [i["id"]], "P")
    chain.new(root, "question", ["table:ds/samples.tsv"], "unrelated")
    geo = layout.provenance(chain.build(Tree(root)), i["id"])
    kinds = {n["id"]: n["kind"] for n in geo["nodes"]}
    assert kinds == {"table:ds/tables/mags.tsv": "given", q["id"]: "data", i["id"]: "data", p["id"]: "data"}
    assert len(geo["edges"]) == 3 and all(e["d"] for e in geo["edges"])


def test_a_non_tabular_file_previews_as_text(scope):
    root, _ = scope
    pv = chain.node(Tree(root), "table:ds/assembly/stats_a")["preview"]
    assert pv["text"] == '{\n  "n50": 1200\n}'


def write_body(root, made, body, **meta_extra):
    f = root / made["path"]
    meta, _ = chain.split(f.read_text())
    meta.update(meta_extra)
    f.write_text(chain.join(meta, body))


def test_a_sentence_traces_through_its_citations_to_table_rows(scope):
    root, cache = scope
    t = "table:ds/tables/mags.tsv"
    q = chain.new(root, "question", [t], "Which phyla?")
    f = chain.new(root, "figure", [q["id"], t], "MAGs by phylum")
    write_body(root, f, "", rows={"table": t, "key": "bin"})
    i = chain.new(root, "interpretation", [f["id"]], "Bacteroidota lead")
    write_body(root, i, "- Bacteroidota hold two MAGs [[%s?bin=S1.a.001,S1.a.003]].\n- One is small.\n" % f["id"])
    p = chain.new(root, "point", [i["id"]], "P")
    write_body(root, p, "Bacteroidota dominate [[%s#1]]. Chloroflexota is rare [[%s?phylum=Chloroflexota]].\n" % (i["id"], t))
    g = chain.build(Tree(root))
    assert not [x for x in g["problems"] if x["code"] == "bad_cite"], g["problems"]

    out = trace.trace(Tree(root), g, ["%s#1" % i["id"]])
    assert out["steps"][0]["text"].startswith("Bacteroidota hold two")
    assert [r["bin"] for r in out["rows"][0]["rows"]] == ["S1.a.001", "S1.a.003"] and out["rows"][0]["via"] == f["id"]
    marks = {"data": [{"customdata": [["S1.a.001"], ["S1.a.002"]]}], "layout": {}}
    pin_dir(root, cache, "chain", {f"{f['id']}.plotly.json": json.dumps(marks).encode()})
    only = trace.trace(Tree(root), chain.build(Tree(root)), ["%s?phylum=Bacteroidota" % f["id"]])["rows"][0]
    assert [r["bin"] for r in only["rows"]] == ["S1.a.001"], "a figure selection covers only its own marks"
    assert {t, q["id"], f["id"], i["id"]} <= set(out["lit"])
    rows = trace.trace(Tree(root), g, ["%s?phylum=Chloroflexota" % t])["rows"][0]
    assert rows["total"] == 1 and rows["rows"][0]["completeness"] == 60
    rows = trace.trace(Tree(root), g, ["%s?phylum=Bacteroidota&completeness>=92" % t])["rows"][0]
    assert [r["bin"] for r in rows["rows"]] == ["S1.a.003"]
    assert trace.trace(Tree(root), g, ["%s?phylum=null" % t])["rows"][0]["total"] == 0
    assert trace.trace(Tree(root), g, ["%s?phylum!=null" % t])["rows"][0]["total"] == 3


def test_check_reports_each_bad_citation(scope):
    root, _ = scope
    t = "table:ds/tables/mags.tsv"
    q = chain.new(root, "question", [t], "Q")
    f = chain.new(root, "figure", [q["id"], t], "F")
    other = chain.new(root, "question", ["table:ds/samples.tsv"], "other")
    i = chain.new(root, "interpretation", [f["id"]], "I")
    write_body(root, i, "[[nope]] [[%s]] [[%s#3]] [[%s?bin=x]] [[%s?colour=red]] [[%s?a=b]] [[%s?completeness~9]]\n"
               % (other["id"], q["id"], f["id"], t, q["id"], t))
    msgs = [x["message"] for x in chain.build(Tree(root))["problems"] if x["code"] == "bad_cite"]
    assert len(msgs) == 7, msgs


def test_check_reports_each_broken_rule(scope):
    root, _ = scope
    q = chain.new(root, "question", ["table:ds/tables/mags.tsv"], "Q")
    (root / "chain" / "points").mkdir(parents=True)
    (root / "chain" / "points" / "p-x.md").write_text("---\nid: p-x\nstage: point\nfrom: [nope, table:ds/missing.tsv]\n---\n")
    (root / "chain" / "points" / "p-dup.md").write_text(f"---\nid: {q['id']}\nstage: point\nfrom: []\n---\n")
    (root / "chain" / "questions" / "q-late.md").write_text("---\nid: q-late\nstage: question\nfrom: [p-x]\n---\n")
    (root / "chain" / "prose").mkdir()
    (root / "chain" / "prose" / "bad.md").write_text("no front matter\n")
    codes = {p["code"] for p in chain.build(Tree(root))["problems"]}
    assert {"unresolved_from", "duplicate_id", "stage_order", "bad_front_matter"} <= codes


def test_a_cycle_is_reported(scope):
    root, _ = scope
    d = root / "chain" / "points"
    d.mkdir(parents=True)
    (d / "a.md").write_text("---\nid: a\nstage: point\nfrom: [b]\n---\n")
    (d / "b.md").write_text("---\nid: b\nstage: point\nfrom: [a]\n---\n")
    assert "cycle" in {p["code"] for p in chain.build(Tree(root))["problems"]}


def test_an_edited_input_stales_the_child_and_flags_downstream_until_accept(scope):
    root, _ = scope
    q = chain.new(root, "question", ["table:ds/tables/mags.tsv"], "Q")
    i = chain.new(root, "interpretation", [q["id"]], "I")
    p = chain.new(root, "point", [i["id"]], "P")
    qf = root / q["path"]
    qf.write_text(qf.read_text() + "A sharper question.\n")
    g = {n["id"]: n for n in chain.build(Tree(root), gate=True)["nodes"]}
    assert g[i["id"]]["stale"] and g[i["id"]]["stale_inputs"] == [q["id"]]
    assert not g[p["id"]]["stale"] and g[p["id"]]["upstream_stale"]
    assert not chain.build(Tree(root), gate=True)["ok"]
    assert chain.build(Tree(root))["ok"]

    with pytest.raises(ValueError):
        chain.accept(root, i["id"], " ")
    chain.accept(root, i["id"], "re-read against the sharper question; still holds")
    g = {n["id"]: n for n in chain.build(Tree(root), gate=True)["nodes"]}
    assert not g[i["id"]]["stale"] and not g[p["id"]]["upstream_stale"]
    assert g[i["id"]]["review"]["note"].startswith("re-read")


def test_accepting_review_metadata_does_not_ripple_downstream(scope):
    root, _ = scope
    q = chain.new(root, "question", ["table:ds/tables/mags.tsv"], "Q")
    i = chain.new(root, "interpretation", [q["id"]], "I")
    chain.accept(root, q["id"], "checked")
    assert not {n["id"]: n for n in chain.build(Tree(root))["nodes"]}[i["id"]]["stale"]


def test_stale_ok_passes_the_gate(scope):
    root, _ = scope
    q = chain.new(root, "question", ["table:ds/tables/mags.tsv"], "Q")
    i = chain.new(root, "interpretation", [q["id"]], "I")
    (root / q["path"]).write_text((root / q["path"]).read_text() + "edit\n")
    f = root / i["path"]
    meta, body = chain.split(f.read_text())
    meta["stale_ok"] = "wording only"
    f.write_text(chain.join(meta, body))
    assert chain.build(Tree(root), gate=True)["ok"]


def test_a_figure_needs_its_artifact_pinned(scope):
    root, cache = scope
    q = chain.new(root, "question", ["table:ds/tables/mags.tsv"], "Q")
    f = chain.new(root, "figure", [q["id"], "table:ds/tables/mags.tsv"], "Completeness")
    assert f["artifact"] == f"data/chain/{f['id']}.plotly.json"
    (root / f["made_by"]).write_text("print('fig')\n")
    assert "artifact_missing" in {p["code"] for p in chain.build(Tree(root))["problems"]}
    pin_dir(root, cache, "chain", {f"{f['id']}.plotly.json": b'{"data": [], "layout": {}}'})
    g = chain.build(Tree(root))
    assert g["ok"], g["problems"]
    assert chain.node(Tree(root), f["id"])["figure"] == {"data": [], "layout": {}}


def test_an_old_ref_reads_text_from_git_and_data_from_the_cache(scope):
    root, cache = scope
    q = chain.new(root, "question", ["table:ds/tables/mags.tsv"], "Q")
    (root / q["path"]).write_text((root / q["path"]).read_text() + "first wording\n")
    sh(root, "add", "-A")
    sh(root, "commit", "-qm", "v1")
    v1 = Tree(root).commit
    (root / q["path"]).write_text((root / q["path"]).read_text().replace("first", "second"))
    pin_dir(root, cache, "ds", {"samples.tsv": b"sample_id\nS1\n", "tables/mags.tsv": b"bin\tcompleteness\nS1.a.001\t50\n"})
    sh(root, "add", "-A")
    sh(root, "commit", "-qm", "v2")

    old = chain.node(Tree(root, v1), q["id"])
    assert "first wording" in old["body"]
    assert chain.node(Tree(root, v1), "table:ds/tables/mags.tsv")["preview"]["head"][0]["completeness"] == "90"
    assert chain.node(Tree(root), "table:ds/tables/mags.tsv")["preview"]["head"][0]["completeness"] == "50"
    assert not {n["id"]: n for n in chain.build(Tree(root, v1))["nodes"]}[q["id"]]["stale"]
    assert {n["id"]: n for n in chain.build(Tree(root))["nodes"]}[q["id"]]["stale"]
    assert [c["subject"] for c in chain.history(Tree(root), None)] == ["v2", "v1", "data"]


def test_every_function_projects_into_the_scopeview_domain():
    assert {fn["name"] for fn in API_MANIFEST["functions"]} == set(HANDLERS)
    for fn in API_MANIFEST["functions"]:
        assert fn["tool"] == f"scopeview_{fn['name']}"
