"""Write grammar.jsonl: search's Python answers to the per-source grammar and the SQL it compiles.

It imports the Python search, which the kb port deleted. Run it from a checkout of a commit that
still holds awm/services/search (the parent of the deletion), in the awm env, from the root:
    mamba run -n awm python -I core/crates/awm-index/tests/fixtures/capture.py
Every input is synthetic.
"""

import json
import sys
from pathlib import Path

from awm.search import lance_query as lq
from awm.search import query as q
from awm.search.registry import Field

FIELDS = {
    "title": Field("title", "content"),
    "text": Field("text", "content"),
    "ts": Field("ts", "datetime"),
    "score": Field("score", "numeric"),
    "project": Field("project", "categorical"),
    "scope": Field("scope", "categorical", "/"),
    "tag": Field("tag", "categorical"),
}
KIND = "posts"
PATH = "query.posts"

TEXTS = [
    "lantern repair",
    "",
    {"$about": "how to fix a lamp"},
    {"$about": ""},
    {"$contains": "brass lantern"},
    {"$contains": "--"},
    {"$and": [{"$about": "lamps"}, {"$contains": "wick"}]},
    {"$or": [{"$about": "lamps"}, {"$contains": "wick"}]},
    {"$or": [{"$about": "a"}, {"$about": "b"}]},
    {"$and": [{"$contains": "a1"}, {"$nor": [{"$contains": "b2"}, {"$contains": "c3"}]}]},
    {"$or": [{"$contains": "x"}, {"$and": [{"$contains": "y"}, {"$contains": "z"}]}]},
    {"$nor": [{"$contains": "only"}]},
    {"$and": []},
    {"$contains": "a", "$about": "b"},
    {"$sort": "ts"},
    {"$and": [{"$contains": "a"}, {"$about": 3}]},
    {"$or": [{"$contains": "a"}, {"$or": [{"$about": "nested"}]}]},
    ["not", "an", "object"],
    7,
]

FILTERS = [
    {},
    {"project": "alpha"},
    {"project": ["alpha", "beta"]},
    {"scope": "main"},
    {"scope": "main/*"},
    {"scope": "ma*n"},
    {"tag": {"$ne": "draft"}},
    {"tag": {"$in": ["a", "b"]}},
    {"tag": {"$nin": ["a", "b"]}},
    {"tag": {"$all": ["a", "b"]}},
    {"tag": {"$in": []}},
    {"tag": {"$gt": "a"}},
    {"ts": {"$gte": "2026-01-01"}},
    {"ts": {"$lt": "2026-03-01T10:00:00+02:00", "$gt": "2025-12-31T23:59:59.5Z"}},
    {"ts": "2026-02-30"},
    {"ts": {"$eq": "yesterday"}},
    {"score": 3},
    {"score": {"$gte": 2.5, "$lt": 10}},
    {"score": {"$ne": 1}},
    {"score": "high"},
    {"score": True},
    {"$or": [{"project": "a"}, {"tag": "b"}]},
    {"$and": [{"project": "a"}, {"$nor": [{"tag": "x"}, {"scope": "s/t"}]}]},
    {"$or": []},
    {"$sort": "-ts"},
    {"title": "x"},
    {"nope": "x"},
    {"$where": "1"},
    {"$contains": "x"},
    {"project": {"$regex": "a"}},
    {"project": {"$in": "a"}},
    {"project": None},
    {"project": ["a", ["b"]]},
    {"project": {"$eq": "it's"}},
    {"tag": "a.b+c*"},
    "not a dict",
]

SORTS = ["ts", "-ts", "+title", "-nope", "ts desc", 5]
MAXES = [1, 10, 100, 0, 101, "7", " 8 ", "x", 3.5, True, None, "-1"]

VEILS = [
    None,
    [],
    [{"project": "beta", "scope": None}],
    [{"project": "alpha", "scope": "main/beds"}, {"project": "Beta", "scope": None}],
    [{"project": "it's", "scope": "a.b"}],
]


def node(n):
    if isinstance(n, q.Lit):
        return n.text
    return {n.op: [node(x) for x in n.items]}


def run(fn):
    try:
        return {"ok": fn()}
    except q.QueryError as e:
        return {"err": str(e)}
    except lq.Unserved as e:
        return {"unserved": str(e)}


def text(v):
    t = q._text(v)
    return {"about": t.about, "under_or": t.under_or, "contains": None if t.contains is None else node(t.contains)}


def filt(v):
    f = q._filter(v, FIELDS, KIND, PATH)
    return {"filter": f, "sql": run(lambda: lq.filter_sql(FIELDS, f))}


def main():
    out = []
    for v in TEXTS:
        out.append({"case": "text", "input": v, **run(lambda: text(v))})
    for v in FILTERS:
        out.append({"case": "filter", "input": v, **run(lambda: filt(v))})
    for v in SORTS:
        out.append({"case": "sort", "input": v, **run(lambda: list(q._sort(v, FIELDS, KIND, PATH + ".$sort")))})
    for v in MAXES:
        out.append({"case": "max", "input": v, **run(lambda: q._max(v))})
    for v in VEILS:
        out.append({"case": "veil", "input": v, "ok": lq.veil_sql(FIELDS, v)})
    dest = Path(__file__).with_name("grammar.jsonl")
    dest.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out))
    print(f"{len(out)} cases -> {dest}", file=sys.stderr)


main()
