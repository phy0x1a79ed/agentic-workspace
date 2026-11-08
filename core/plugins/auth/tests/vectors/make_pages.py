"""Write pages.json: the HTML the Python edge renders for synthetic inputs, byte for byte.

Run from anywhere with `mamba run -n awm python -I make_pages.py`; it reads the Python from
this worktree, not from an installed copy.
"""

import json
import pathlib
import sys

WS = pathlib.Path(__file__).resolve().parents[5]
sys.path[:0] = [str(WS / "awm/services/httpsfront"), str(WS / "awm/service_components/tlsfront"),
                str(WS / "awm/service_components/config"), str(WS / "awm/service_components/grantlib"),
                str(WS / "awm/service_components/persistence")]

from awm.httpsfront import pages  # noqa: E402

assert pages.__file__.startswith(str(WS)), pages.__file__

SNAPSHOTS = [
    {"plugins": [
        {"name": "claude-science", "pages": [{"name": "science", "dir": "/x/pages/science/dist", "entry": "index.html"}]},
        {"name": "journal", "pages": []},
        {"name": "trilium", "pages": [{"name": "trilium"}, {"name": ""}, {}, None]},
        {"name": "a b/c?d", "pages": [{"name": "p&q<r>\"s'"}, {"name": "café ☃"}]},
        {"name": "", "pages": [{"name": "orphan"}]},
        {"pages": [{"name": "nameless"}]},
        None, {}, 0,
        {"name": 7, "pages": [{"name": 1.5}, {"name": True}, {"name": [1, "x"]}, {"name": {"k": None}}, {"name": 0}]},
        {"name": "x~y_z.w-v", "pages": [{"name": "~ok"}]},
    ]},
    {"plugins": []}, {"plugins": None}, {}, [], "plugins", None,
    {"plugins": "ab"}, {"plugins": ""}, {"plugins": {"k": 1}}, {"plugins": {"": 1}}, {"plugins": 3},
    {"plugins": [{"name": "p", "pages": "x"}]}, {"plugins": [{"name": "p", "pages": 5}]},
    {"plugins": [{"name": "p", "pages": [[1]]}]}, {"plugins": [{"name": "p", "pages": [[]]}]},
    {"plugins": [[1, 2]]}, {"plugins": [{"name": "p", "pages": {"": 1}}]},
    {"plugins": [{"name": "p", "pages": [{"name": "q"}]}, "bad"]},
]


def entries_rows():
    for snap in SNAPSHOTS:
        try:
            got = pages.page_entries(snap)
        except Exception:  # noqa: BLE001 - the landing route degraded to an empty index
            got = []
        yield {"snapshot": snap, "entries": got}


ENTRIES = pages.page_entries(SNAPSHOTS[0])

LANDINGS = [
    {"entries": [], "tags_by_page": {}, "tag_counts": [], "selected_tags": [], "peer_name": "awm", "display_names": []},
    {"entries": ENTRIES,
     "tags_by_page": {"science": ["lab", "physics"], "trilium": ["notes"], "p&q<r>\"s'": ["a'b", "<x>"]},
     "tag_counts": [["<x>", 1], ["a'b", 1], ["lab", 1], ["notes", 1], ["physics", 2], ["unused", 0]],
     "selected_tags": ["physics", "gone", "a'b"],
     "peer_name": "node.example.test",
     "display_names": [["science", "Science Lab"], ["trilium", "Zz été \U0001F600 \x7f"], ["orphan", "x"]]},
    {"entries": ENTRIES[:2], "tags_by_page": {}, "tag_counts": [["only", 3]], "selected_tags": ["only"],
     "peer_name": "<b>&'\"", "display_names": [["science", ""]]},
    {"entries": [{"name": "b", "href": "/p/x/b/"}, {"name": "a", "href": "/p/x/a/"}, {"name": "c", "href": "/p/x/c/"}],
     "tags_by_page": {"a": [], "b": ["t"]}, "tag_counts": [["t", 1]], "selected_tags": [],
     "peer_name": "mesh", "display_names": [["c", "a"], ["a", "c"]]},
]


def landing_rows():
    for case in LANDINGS:
        html = pages.landing_page(case["entries"], case["tags_by_page"], dict(case["tag_counts"]),
                                  case["selected_tags"], case["peer_name"],
                                  display_names=dict(case["display_names"]))
        yield {**case, "html": html}


REASONS = ["not listening yet", "", "<script>alert('x')</script> & \"more\" é"]

out = {
    "page_entries": list(entries_rows()),
    "landing": list(landing_rows()),
    "vault_unavailable": [{"reason": r, "html": pages.vault_unavailable_page(r)} for r in REASONS],
}
dest = pathlib.Path(__file__).with_name("pages.json")
dest.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
print(f"{dest}: {len(out['page_entries'])} snapshots, {len(out['landing'])} landings, {len(out['vault_unavailable'])} vault pages")
