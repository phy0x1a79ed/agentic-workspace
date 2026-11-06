"""The request parser: short forms, text normalisation, node split, relayed calls and every rejection."""

from __future__ import annotations

import json

import pytest

from awm.search.query import Lit, Op, QueryError, parse

from .conftest import HERE

pytestmark = [pytest.mark.search]


def bad(query, match, **extra):
    with pytest.raises(QueryError, match=match):
        parse({"query": query, **extra})


# -- short forms ---------------------------------------------------------------


def test_string_query_is_text_over_this_nodes_journals():
    req = parse({"query": "trigram budget"})
    assert [(s.kind, s.node, s.filter) for s in req.sources] == [("journals", "", {})]
    assert req.text.about == "trigram budget" and req.text.under_or
    assert req.text.contains == Lit("trigram budget") and req.text.raw == "trigram budget"
    assert (req.max, req.relayed, req.remote, req.elsewhere) == (10, False, {}, [])


def test_text_without_source_searches_journals():
    req = parse({"query": {"text": {"$contains": "HNSW"}}})
    assert [s.name for s in req.sources] == ["journals"]
    assert req.text.about is None and req.text.contains == Lit("HNSW")


def test_sources_without_text_have_no_text():
    req = parse({"query": {"journals": {"project": "awm", "tag": "debrief"}}})
    assert req.text is None
    assert req.sources[0].filter == {"project": "awm", "tag": "debrief"}


def test_json_string_query_is_an_object():
    req = parse({"query": '{"text": "x", "journals": {"scope": "svc-*"}}'})
    assert req.sources[0].filter == {"scope": "svc-*"} and req.text.about == "x"


# -- text normalisation --------------------------------------------------------


def text(value):
    return parse({"query": {"text": value}}).text


def test_lone_about():
    t = text({"$about": "latency"})
    assert (t.about, t.under_or, t.contains) == ("latency", False, None)


def test_or_with_only_about_is_a_lone_about():
    t = text({"$or": [{"$about": "latency"}]})
    assert (t.about, t.under_or, t.contains) == ("latency", False, None)


def test_or_with_about_and_contains_is_under_or():
    t = text({"$or": [{"$about": "a"}, {"$contains": "p95"}]})
    assert (t.about, t.under_or, t.contains) == ("a", True, Lit("p95"))


def test_and_with_about_and_several_contains():
    t = text({"$and": [{"$about": "a"}, {"$contains": "p95"}, {"$or": [{"$contains": "x"}, {"$contains": "y"}]}]})
    assert t.about == "a" and not t.under_or
    assert t.contains == Op("and", (Lit("p95"), Op("or", (Lit("x"), Lit("y")))))


def test_and_with_about_and_one_contains_collapses_to_it():
    t = text({"$and": [{"$contains": "p95"}, {"$about": "a"}]})
    assert t.contains == Lit("p95") and not t.under_or


def test_no_about_keeps_the_whole_tree():
    t = text({"$and": [{"$contains": "a"}, {"$contains": "b"}]})
    assert (t.about, t.under_or) == (None, False)
    assert t.contains == Op("and", (Lit("a"), Lit("b")))
    t = text({"$nor": [{"$contains": "a"}, {"$contains": "b"}]})
    assert t.contains == Op("nor", (Lit("a"), Lit("b")))


def test_nor_nested_under_and_survives():
    t = text({"$and": [{"$about": "a"}, {"$nor": [{"$contains": "z"}]}]})
    assert t.contains == Op("nor", (Lit("z"),))


def test_raw_is_the_text_as_given():
    value = {"$or": [{"$about": "a"}, {"$contains": "b"}]}
    assert text(value).raw == value


# -- sources and nodes ---------------------------------------------------------


def test_node_split_here_and_remote():
    req = parse({"query": {"journals": {"project": "awm"}, "kb@altair": {"author": "Smith"},
                           "transcripts@vega": {}}})
    assert [s.name for s in req.sources] == ["journals"]
    assert set(req.remote) == {"altair", "vega"}
    assert req.remote["altair"][0].kind == "kb" and req.remote["altair"][0].node == "altair"


def test_explicit_this_node_is_local():
    req = parse({"query": {f"journals@{HERE}": {"scope": "x"}}})
    assert req.sources[0].name == f"journals@{HERE}" and req.sources[0].node == "" and not req.remote


def test_a_source_named_twice_after_normalisation_is_refused():
    bad({"journals": {}, f"journals@{HERE}": {}}, "named twice")
    bad({"kb@altair": {}, "kb@Altair": {}}, "named twice")


def test_same_kind_on_different_nodes_is_fine():
    req = parse({"query": {"journals": {}, "journals@altair": {}}})
    assert len(req.sources) == 1 and len(req.remote["altair"]) == 1


def test_forwarded_uses_bare_kinds_and_raw_text():
    req = parse({"query": {"text": {"$contains": "x"}, "journals": {"project": "awm"},
                           "journals@altair": {"ts": {"$gte": "2026-10-01"}}, "kb@altair": {"author": "A"}},
                 "max": 20})
    assert req.forwarded("altair") == {
        "query": {"text": {"$contains": "x"}, "journals": {"ts": {"$gte": "2026-10-01T00:00:00Z"}},
                  "kb": {"author": "A"}},
        "max": 20}


def test_forwarded_omits_absent_text():
    req = parse({"query": {"kb@altair": {}}})
    assert req.forwarded("altair") == {"query": {"kb": {}}, "max": 10}


# -- relayed calls -------------------------------------------------------------


def test_relayed_call_drops_remote_sources_and_records_them(monkeypatch):
    from awm.config import privacy
    monkeypatch.setattr(privacy, "marks", lambda: [{"mark": 1}])
    req = parse({"query": {"journals": {}, "kb@altair": {}, "journals@vega": {}}}, as_="peer:altair")
    assert req.relayed and req.remote == {} and req.elsewhere == ["kb@altair", "journals@vega"]
    assert [s.name for s in req.sources] == ["journals"]
    assert req.hidden == [{"mark": 1}]


def test_local_call_sees_nothing_hidden():
    req = parse({"query": "x"}, as_="session")
    assert not req.relayed and req.hidden == []


def test_unreadable_marks_hide_everything(monkeypatch):
    from awm.config import privacy

    def boom():
        raise privacy.MarksUnreadable("no")
    monkeypatch.setattr(privacy, "marks", boom)
    assert parse({"query": "x"}, as_="peer").hidden is None


# -- max and arguments ---------------------------------------------------------


@pytest.mark.parametrize("value, expected", [(1, 1), (100, 100), ("20", 20), (None, 10)])
def test_max_accepted(value, expected):
    args = {"query": "x"} if value is None else {"query": "x", "max": value}
    assert parse(args).max == expected


@pytest.mark.parametrize("value", [0, 101, -1, True, 2.5, "ten", "", [5]])
def test_max_refused(value):
    with pytest.raises(QueryError, match=r"^max: .*allowed: 1-100"):
        parse({"query": "x", "max": value})


def test_unknown_argument_is_named():
    with pytest.raises(QueryError, match=r"^sources: unknown argument; allowed: query, max"):
        parse({"query": "x", "sources": ["journals"]})


def test_caller_keys_are_ignored():
    assert parse({"query": "x", "_caller_scope": "awm/redux"}).max == 10


def test_query_is_required():
    with pytest.raises(QueryError, match="^query: missing"):
        parse({})


# -- ts ------------------------------------------------------------------------


def test_ts_is_normalised_everywhere():
    f = {"ts": {"$gte": "2026-03-01", "$lt": "2026-03-01T12:00:00+02:00", "$in": ["2026-03-02 05:00"]}}
    req = parse({"query": {"journals": {"$or": [f, {"$and": [{"ts": "2026-04-01"}]}]}}})
    first, second = req.sources[0].filter["$or"]
    assert first["ts"] == {"$gte": "2026-03-01T00:00:00Z", "$lt": "2026-03-01T10:00:00Z",
                           "$in": ["2026-03-02T05:00:00Z"]}
    assert second["$and"][0]["ts"] == "2026-04-01T00:00:00Z"


@pytest.mark.parametrize("filt", [
    {"ts": "yesterday"}, {"ts": {"$gt": "soon"}}, {"ts": {"$in": ["2026-01-01", "nope"]}},
    {"$or": [{"ts": "x"}]}, {"$nor": [{"$and": [{"ts": {"$lte": "13/01/2026"}}]}]}, {"ts": 5}])
def test_bad_ts_refused(filt):
    bad({"journals": filt}, "ts|ISO 8601|scalar")


def test_ts_error_names_its_path():
    bad({"journals": {"$or": [{"ts": {"$in": ["2026-01-01", "nope"]}}]}},
        r"query\.journals\.\$or\[0\]\.ts\.\$in\[1\]: 'nope' is not an ISO 8601")


# -- rejections ----------------------------------------------------------------


def test_bare_list_value():
    bad({"journals": {"tag": ["a", "b"]}}, r"query\.journals\.tag: a bare list.*\$in")


def test_unknown_operator():
    bad({"journals": {"tag": {"$regex": "x"}}}, r"query\.journals\.tag\.\$regex: unknown operator; allowed: \$eq")


def test_operator_at_field_position():
    bad({"journals": {"$gt": 3}}, r"query\.journals\.\$gt: operator not allowed at a field position; allowed: \$and.*project")
    bad({"journals": {"$or": [{"$in": [1]}]}}, r"\$or\[0\]\.\$in: operator not allowed")


def test_field_the_kind_lacks_lists_its_fields():
    bad({"transcripts": {"tag": "x"}}, r"query\.transcripts\.tag: transcripts has no field 'tag'; allowed: ts, project, scope")
    bad({"kb": {"project": "x"}}, r"allowed: ts, author, tag, cites, cited_by")
    bad({"journals": {"$and": [{"author": "x"}]}}, r"\$and\[0\]\.author")


@pytest.mark.parametrize("key", ["$about", "$contains"])
def test_text_operators_inside_a_source_filter(key):
    bad({"journals": {key: "x"}}, rf"query\.journals\.\{key}: .*belongs in text")
    bad({"journals": {"$or": [{key: "x"}]}}, rf"\$or\[0\]\.\{key}")


def test_second_about():
    bad({"text": {"$or": [{"$about": "a"}, {"$about": "b"}]}}, r"query\.text\.\$or: more than one \$about")
    bad({"text": {"$and": [{"$about": "a"}, {"$about": "b"}, {"$contains": "c"}]}}, "more than one")


def test_about_nested_too_deep():
    bad({"text": {"$and": [{"$or": [{"$about": "a"}, {"$contains": "b"}]}]}}, r"query\.text\.\$and\[0\]\.\$or\[0\]: \$about sits only")
    bad({"text": {"$nor": [{"$about": "a"}]}}, r"query\.text\.\$nor\[0\]: \$about sits only")
    bad({"text": {"$and": [{"$nor": [{"$about": "a"}]}]}}, r"query\.text\.\$and\[0\]\.\$nor\[0\]: \$about sits only")


def test_text_object_with_several_keys():
    bad({"text": {"$about": "a", "$contains": "b"}}, r"query\.text: .*exactly one key")
    bad({"text": {"$and": [{"$contains": "a", "$about": "b"}]}}, r"query\.text\.\$and\[0\]: .*exactly one key")
    bad({"text": {}}, "exactly one key")


def test_text_unknown_operator():
    bad({"text": {"$regex": "a"}}, r"query\.text: .*exactly one key|not allowed in text")
    bad({"text": {"$and": [{"$regex": "a"}]}}, r"\$and\[0\]\.\$regex: not allowed in text")


def test_contains_value_must_be_a_nonempty_string():
    bad({"text": {"$contains": ""}}, "a letter or digit")
    bad({"text": {"$contains": 5}}, "a letter or digit")
    bad({"text": {"$contains": "--"}}, "a letter or digit")
    bad({"text": {"$about": ""}}, "non-empty string")
    bad({"text": {"$and": [{"$about": 5}]}}, "non-empty string")
    bad({"text": ""}, "text is empty")


def test_empty_logical_lists():
    bad({"journals": {"$and": []}}, r"query\.journals\.\$and: needs a non-empty list")
    bad({"journals": {"$or": {"project": "x"}}}, "non-empty list")
    bad({"text": {"$or": []}}, r"query\.text\.\$or: needs a non-empty list")
    bad({"text": {"$and": [{"$nor": []}]}}, "non-empty list")


def test_empty_operator_object():
    bad({"journals": {"project": {}}}, r"query\.journals\.project: empty operator object")


def test_empty_in_list():
    bad({"journals": {"tag": {"$in": []}}}, r"\$in: needs a non-empty list")
    bad({"journals": {"tag": {"$all": "a"}}}, r"\$all: needs a non-empty list")


@pytest.mark.parametrize("value", [True, None, 1.5 + 0j, {"a": 1}])
def test_non_scalar_values(value):
    if isinstance(value, dict):
        bad({"journals": {"project": {"$eq": value}}}, r"project\.\$eq: dict is not a scalar")
    else:
        bad({"journals": {"project": value}}, "not a scalar")
    bad({"journals": {"project": {"$in": [value]}}}, r"\$in\[0\]")


def test_source_named_text_or_unknown_kind():
    bad({"text@altair": {}}, r"query\.text@altair: not a source; allowed: awm, journals, kb, transcripts")
    bad({"notes": {}}, r"query\.notes: not a source; allowed: awm, journals, kb, transcripts")
    bad({"precedence@altair": {}}, "not a source")


@pytest.mark.parametrize("key", ["journals@", "journals@a b", "journals@@x", "kb@x/y"])
def test_malformed_node(key):
    bad({key: {}}, "malformed node after @")


def test_source_filter_must_be_an_object():
    bad({"journals": "awm"}, r"query\.journals: a source filter is an object")
    bad({"journals": {"$and": ["x"]}}, r"\$and\[0\]: a source filter is an object")


@pytest.mark.parametrize("query", [{}, [], 5, None, "", "   "])
def test_empty_or_odd_query(query):
    bad(query, "^query: ")


def test_query_json_string_that_does_not_parse():
    bad('{"text": ', "query: not valid JSON")


def test_duplicate_keys_are_found_only_in_json_text():
    text_form = '{"journals": {"project": "a", "project": "b"}}'
    bad(text_form, r"query\.journals\.project: duplicate key")
    bad('{"journals": {}, "journals": {}}', r"query\.journals: duplicate key")
    bad('{"journals": {"$or": [{"tag": "a", "tag": "b"}]}}', r"\$or\[0\]\.tag: duplicate key")
    bad('{"text": {"$or": [{"$about": "a", "$about": "b"}]}}', "duplicate key")
    # an object has already lost its duplicates by the time it arrives
    assert parse({"query": json.loads(text_form)}).sources[0].filter == {"project": "b"}


# -- the plan's examples -------------------------------------------------------

EXAMPLES = [
    {"query": "trigram budget"},
    {"query": {"journals": {"project": "awm", "scope": "redux-dev", "tag": "debrief"}}},
    {"query": {"text": "trigram budget", "journals": {"project": "awm", "scope": "svc-*"}}},
    {"query": {"text": {"$and": [{"$about": "search latency at 10x scale"},
                                 {"$or": [{"$contains": "p95"}, {"$contains": "timeout"}]}]},
               "journals": {"project": "awm"}, "journals@altair": {"ts": {"$gte": "2026-10-01"}}}},
    {"query": {"text": {"$contains": "HNSW"}, "journals": {"replies_to": "<post id>"}}, "max": 20},
]


@pytest.mark.parametrize("args", EXAMPLES)
def test_plan_examples_parse(args):
    req = parse(args)
    assert req.sources and req.max in (10, 20)


def test_last_examples_shape():
    req = parse(EXAMPLES[3])
    assert req.text.about == "search latency at 10x scale" and not req.text.under_or
    assert req.text.contains == Op("or", (Lit("p95"), Lit("timeout")))
    assert req.remote["altair"][0].filter == {"ts": {"$gte": "2026-10-01T00:00:00Z"}}
