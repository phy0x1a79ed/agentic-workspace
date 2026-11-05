"""compile.py: filters and text trees against a real database."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from awm.search import compile as c
from awm.search import store


@dataclass(frozen=True)
class Lit:
    text: str


@dataclass(frozen=True)
class Op:
    op: str
    items: tuple


def doc(i, *, title="", text="", project="", scope="", ts="2026-03-01T00:00:00Z", tags=(), reply_to=(),
        source="journals"):
    return store.clean(source, {"id": i, "title": title, "text": text or title or "x", "project": project,
                                "scope": scope, "ts": ts, "tags": list(tags), "reply_to": list(reply_to)})


DOCS = [
    doc("a", title="Alpha note", project="awm", scope="redux", ts="2026-03-01", tags=["Debrief", "x"]),
    doc("b", title="Beta", text="the trigram budget", project="awm", scope="redux/sub", ts="2026-03-05",
        tags=["x"], reply_to=["a"]),
    doc("c", title="Gamma", project="awm", scope="svc-kb", ts="2026-03-09", reply_to=["a", "b"]),
    doc("d", title="Delta", project="other", scope="svc-a/deep", ts="2026-03-12", tags=["debrief"]),
    doc("e", title="Channel post", project="", scope="project:awm", ts="2026-03-15"),
    doc("f", title="No date", project="awm", scope="s[1]?", ts=""),
    doc("g", title="Größe der Dinge", project="awm", scope="m", ts="2026-03-20"),
    doc("p", title="Precedence-like", project="awm", scope="redux", source="other"),
]


@pytest.fixture()
def search_db(tmp_path, monkeypatch):
    services_dir = tmp_path / "services"
    services_dir.mkdir()
    import awm.persistence.databases as pdbs
    monkeypatch.setattr(pdbs, "SERVICES_DIR", services_dir)
    monkeypatch.setattr(store, "_initialized", False)
    store.init()
    return services_dir


@pytest.fixture()
def conn(search_db):
    conn = store.connect()
    store.put_documents(conn, DOCS)
    yield conn
    conn.close()


def ids(conn, f=None, contains=None, negate=False, hidden=()):
    sql, params = c.allowed("journals", f or {}, contains, negate, hidden)
    return sorted(r[0].split(":", 1)[1] for r in conn.execute(sql, params))


def test_empty_filter_is_everything_in_the_source(conn):
    assert c.filter_sql("journals", {}) == ("1", [])
    assert ids(conn) == list("abcdefg")


def test_project_matches_channel_row(conn):
    assert ids(conn, {"project": "awm"}) == list("abcefg")
    assert ids(conn, {"project": "other"}) == ["d"]


def test_scope_covers_nested(conn):
    assert ids(conn, {"scope": "redux"}) == ["a", "b"]
    assert ids(conn, {"scope": "redux/sub"}) == ["b"]
    assert ids(conn, {"scope": "red"}) == []


def test_scope_glob_trailing_star_covers_nested_without_extra_clause(conn):
    sql, params = c.filter_sql("journals", {"scope": "svc-*"})
    assert sql == "scope GLOB ?" and params == ["svc-*"]
    assert ids(conn, {"scope": "svc-*"}) == ["c", "d"]


def test_scope_glob_inner_star_still_covers_nested(conn):
    sql, params = c.filter_sql("journals", {"scope": "*-a"})
    assert sql.count("GLOB") == 2 and params == ["*-a", "*-a/*"]
    assert ids(conn, {"scope": "*-a"}) == ["d"]


def test_glob_escapes_brackets_and_question_marks(conn):
    assert c.glob_pattern("a[1]?*") == "a[[]1]" "[?]*"
    assert ids(conn, {"scope": "s[1]?*"}) == ["f"]
    assert ids(conn, {"scope": "s?*"}) == []


def test_project_glob_matches_channel(conn):
    assert ids(conn, {"project": "a*"}) == list("abcefg")


def test_ts_comparisons_on_normalised_strings(conn):
    assert ids(conn, {"ts": {"$gte": "2026-03-05T00:00:00Z", "$lt": "2026-03-12T00:00:00Z"}}) == ["b", "c"]
    assert ids(conn, {"ts": "2026-03-09T00:00:00Z"}) == ["c"]
    assert "f" not in ids(conn, {"ts": {"$lt": "2026-03-01T00:00:00Z"}})
    assert "f" not in ids(conn, {"ts": {"$lte": "2026-12-01T00:00:00Z"}})


def test_ne_and_nin_on_single_valued(conn):
    assert ids(conn, {"project": {"$ne": "awm"}}) == ["d"]
    assert ids(conn, {"project": {"$nin": ["awm", "other"]}}) == []
    assert ids(conn, {"scope": {"$ne": "redux"}}) == list("cdefg")
    assert ids(conn, {"project": {"$in": ["other", "awm"]}}) == list("abcdefg")


def test_tags_any_all_and_negations(conn):
    assert ids(conn, {"tag": "debrief"}) == ["a", "d"]
    assert ids(conn, {"tag": "DEBRIEF"}) == ["a", "d"]
    assert ids(conn, {"tag": {"$in": ["x", "debrief"]}}) == ["a", "b", "d"]
    assert ids(conn, {"tag": {"$all": ["x", "debrief"]}}) == ["a"]
    assert ids(conn, {"tag": {"$ne": "x"}}) == list("cdefg")
    assert ids(conn, {"tag": {"$nin": ["x", "debrief"]}}) == list("cefg")
    assert ids(conn, {"tag": "deb*"}) == ["a", "d"]


def test_multi_valued_comparison_is_any_value(conn):
    assert ids(conn, {"tag": {"$gt": "debrief"}}) == ["a", "b"]
    assert ids(conn, {"tag": {"$lt": "x"}}) == ["a", "d"]


def test_replies_to(conn):
    assert ids(conn, {"replies_to": "a"}) == ["b", "c"]
    assert ids(conn, {"replies_to": {"$all": ["a", "b"]}}) == ["c"]
    assert ids(conn, {"replies_to": {"$ne": "a"}}) == list("adefg")


def test_logic_composes(conn):
    f = {"$or": [{"project": "other"}, {"scope": "redux/sub"}], "tag": {"$ne": "zzz"}}
    assert ids(conn, f) == ["b", "d"]
    assert ids(conn, {"$nor": [{"project": "awm"}]}) == ["d"]
    assert ids(conn, {"$and": [{"tag": "x"}, {"scope": "redux"}]}) == ["a", "b"]
    assert ids(conn, {"$or": []}) == []
    assert ids(conn, {"$and": []}) == list("abcdefg")


def test_unknown_field_is_refused():
    with pytest.raises(ValueError):
        c.filter_sql("journals", {"nope": 1})


def test_injection_text_stays_a_parameter(conn):
    nasty = "x' OR 1=1 --"
    sql, params = c.allowed("journals", {"scope": nasty, "tag": nasty}, Lit(nasty), False, ())
    assert nasty not in sql and params.count(nasty) >= 1
    assert ids(conn, {"scope": nasty}) == []


def test_contains_trigram_and_short_literals(conn):
    assert ids(conn, contains=Lit("TRIGRAM")) == ["b"]
    assert ids(conn, contains=Lit("budget")) == ["b"]
    assert ids(conn, contains=Lit("be")) == ["b"]  # short: instr(lower())
    assert ids(conn, contains=Lit("a note")) == ["a"]
    assert ids(conn, contains=Lit('say "hi"')) == []


def test_contains_phrase_quoting():
    sql, params = c.contains_sql(Lit('a "b" c'))
    assert params == ['"a ""b"" c"'] and "MATCH ?" in sql
    assert c.contains_sql(Lit("ab"))[0].startswith("instr(")


def test_contains_logic_and_negation(conn):
    both = Op("and", (Lit("trigram"), Lit("budget")))
    assert ids(conn, contains=both) == ["b"]
    assert ids(conn, contains=Op("or", (Lit("alpha"), Lit("gamma")))) == ["a", "c"]
    assert ids(conn, contains=Op("nor", (Lit("alpha"), Lit("gamma")))) == list("bdefg")
    assert ids(conn, contains=Lit("alpha"), negate=True) == list("bcdefg")
    assert ids(conn, contains=None, negate=True) == []
    assert c.where("journals", {}, None, True, ())[0].endswith("AND 0")


def test_non_ascii_literals_fold_case(conn):
    # Trigram folds non-ASCII case here (SQLite 3.54); ss is not a fold of the sharp s.
    assert ids(conn, contains=Lit("GRÖßE")) == ["g"]
    assert ids(conn, contains=Lit("größe")) == ["g"]
    assert ids(conn, contains=Lit("GRÖSSE")) == []
    assert ids(conn, contains=Lit("DIN")) == ["g"]


def test_veil_hides_marked_project(conn):
    hidden = [{"project": "awm", "scope": "redux"}]
    assert ids(conn, hidden=hidden) == list("cdefg")
    assert ids(conn, hidden=[{"project": "awm", "scope": None}]) == ["d"]
    assert ids(conn, hidden=None) == []


def test_newest_orders_limits_and_uses_one_statement(conn):
    got = c.newest(conn, "journals", {"project": "awm"}, None, (), 3)
    assert [d["doc_id"] for d in got] == ["g", "e", "c"]
    assert set(got[0]) == {"doc_id", "title", "ts"}
    got = c.newest(conn, "journals", {}, Lit("beta"), (), 5)
    assert [d["doc_id"] for d in got] == ["b"]


def test_newest_plan_uses_an_index(conn):
    sql, params = c.where("journals", {}, None, False, ())
    plan = " ".join(r[3] for r in conn.execute(
        f"EXPLAIN QUERY PLAN SELECT doc_id, title, ts FROM documents WHERE {sql} ORDER BY ts DESC LIMIT ?",
        [*params, 5]))
    assert "USING INDEX" in plan or "USING COVERING INDEX" in plan
    assert "TEMP B-TREE" not in plan, plan
