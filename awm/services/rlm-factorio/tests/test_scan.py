import json
import math
import re

import pytest

from awm.rlm_factorio import scan


def world(n=900, seed=7):
    """Entities scattered over a few chunks, positions unique."""
    ents, x = [], seed
    for i in range(n):
        x = (x * 1103515245 + 12345) % 2**31
        ents.append({"name": "tree" if i % 3 else "rock",
                     "position": {"x": (x % 2000) / 10 - 100, "y": ((x >> 11) % 2000) / 10 - 100}})
    return ents


def fake_engine(ents, cost_per_entity=0.01):
    """Emulate one PAGE_LUA command over ``ents`` with the same semantics."""
    calls = []

    def run(code):
        blob = re.search(r"json_to_table\('(.*)'\)", code).group(1)
        P = json.loads(blob.replace("\\'", "'").replace("\\\\", "\\"))
        calls.append(P)
        cx1, cy1 = math.floor(P["x1"] / 32), math.floor(P["y1"] / 32)
        cx2, cy2 = math.floor((P["x2"] - 1e-9) / 32), math.floor((P["y2"] - 1e-9) / 32)
        w = cx2 - cx1 + 1
        n = w * (cy2 - cy1 + 1)
        out, count, work, ci, skip, nxt = [], 0, 0, P["ci"], P["skip"], None
        names = P.get("name")
        while ci < n:
            cx, cy = cx1 + ci % w, cy1 + ci // w
            k, done = 0, False
            inside = [e for e in ents if math.floor(e["position"]["x"] / 32) == cx
                      and math.floor(e["position"]["y"] / 32) == cy
                      and P["x1"] <= e["position"]["x"] < P["x2"]
                      and P["y1"] <= e["position"]["y"] < P["y2"]
                      and (not names or e["name"] in names)]
            work += len(inside) + 1
            for e in inside:
                k += 1
                if k > skip:
                    count += 1
                    if not P["count_only"]:
                        out.append(e)
                    if count >= P["limit"]:
                        nxt, done = [ci, k], True
                        break
            if done:
                break
            ci, skip = ci + 1, 0
            if work >= P["work_cap"] and ci < n:
                nxt = [ci, 0]
                break
        return json.dumps({"rows": out, "count": count, "next": nxt}), work * cost_per_entity
    return run, calls


AREA = {"x1": -100, "y1": -100, "x2": 100, "y2": 100}


def follow(sc, run, args):
    seen, cursor = [], None
    while True:
        a = dict(args, cursor=cursor) if cursor else dict(args)
        r = sc.scan("k", a, run)
        seen += r["entities"]
        cursor = r["next"]
        if cursor is None:
            assert r["complete"]
            return seen


def test_cursors_cover_each_entity_exactly_once():
    ents = world()
    run, _ = fake_engine(ents)
    seen = follow(scan.Scanner(), run, {**AREA, "limit": 37})
    key = lambda e: (e["name"], e["position"]["x"], e["position"]["y"])
    assert sorted(map(key, seen)) == sorted(map(key, ents))


def test_count_only_matches_unpaged_count():
    ents = world()
    run, calls = fake_engine(ents)
    sc = scan.Scanner()
    sc.caps["k"] = 60
    r = sc.scan("k", {**AREA, "count_only": True}, run)
    assert r["count"] == len(ents) and r["complete"] and "entities" not in r
    assert len(calls) > 1


def test_default_limit_is_200():
    run, _ = fake_engine(world())
    r = scan.Scanner().scan("k", dict(AREA), run)
    assert len(r["entities"]) == 200 and r["next"]


def test_expensive_pages_shrink_the_work_cap():
    run, _ = fake_engine(world(), cost_per_entity=1.0)
    sc = scan.Scanner()
    sc.scan("k", {**AREA, "count_only": True}, run)
    assert sc.caps["k"] < scan.CAP_START


def test_cursor_rejects_other_filters():
    run, _ = fake_engine(world())
    r = scan.Scanner().scan("k", {**AREA, "limit": 5, "name": ["tree"]}, run)
    with pytest.raises(scan.ScanError, match="other filters"):
        scan.Scanner().scan("k", {"cursor": r["next"], "name": ["rock"]}, run)


def test_radius_centres_on_the_seat():
    q = scan.resolve_area({"radius": 10}, lambda: {"x": 5, "y": -5})
    assert (q["x1"], q["y1"], q["x2"], q["y2"]) == (-5, -15, 15, 5)


def test_area_required_and_bounded():
    with pytest.raises(scan.ScanError, match="needs an area"):
        scan.resolve_area({}, None)
    with pytest.raises(scan.ScanError, match="split it"):
        scan.resolve_area({"x1": 0, "y1": 0, "x2": 9000, "y2": 9000}, None)


def test_unknown_field_refused():
    run, _ = fake_engine(world())
    with pytest.raises(scan.ScanError, match="unknown fields"):
        scan.Scanner().scan("k", {**AREA, "fields": ["owner"]}, run)


def test_page_code_escapes_quotes():
    code = scan.page_code({"surface": "nauvis", "x1": 0, "y1": 0, "x2": 1, "y2": 1,
                           "ci": 0, "skip": 0, "name": ["it's"]},
                          fields=["name"], limit=1, count_only=False, work_cap=1)
    assert "it\\'s" in code and "@PARAMS@" not in code
