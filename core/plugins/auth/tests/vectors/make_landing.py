"""Write landing.json: httpsfront.db's schema, and what the Python store answers to a script
of calls, so the Rust store can replay the script and compare.

Run from anywhere with `mamba run -n awm python -I make_landing.py`; it reads the Python from
this worktree, not from an installed copy.
"""

import json
import pathlib
import sqlite3
import sys
import tempfile

WS = pathlib.Path(__file__).resolve().parents[5]
sys.path[:0] = [str(WS / "awm/services/httpsfront"), str(WS / "awm/service_components/tlsfront"),
                str(WS / "awm/service_components/config"), str(WS / "awm/service_components/grantlib"),
                str(WS / "awm/service_components/persistence")]

from awm.persistence import databases  # noqa: E402
from awm.httpsfront import store  # noqa: E402

assert store.__file__.startswith(str(WS)), store.__file__

SCRIPT = [
    ["tags_for_page", "science"],
    ["add_tag", "science", "physics"],
    ["add_tag", "science", "  lab   work \t"],
    ["add_tag", "science", "physics"],
    ["add_tag", "math", "physics"],
    ["add_tag", "math", "   "],
    ["add_tag", "", "x"],
    ["add_tag", "café", "été　hiver"],
    ["add_tag", "trilium", "notes"],
    ["tags_for_page", "science"],
    ["tags_by_page", ["science", "math", "absent", "café"]],
    ["tags_by_page", []],
    ["all_tag_counts"],
    ["select_tag", "physics"],
    ["select_tag", "  notes "],
    ["select_tag", ""],
    ["select_tag", "ghost"],
    ["selected_tags"],
    ["remove_tag", "science", "physics"],
    ["selected_tags"],
    ["remove_tag", "math", " physics"],
    ["selected_tags"],
    ["all_tag_counts"],
    ["deselect_tag", "notes  "],
    ["selected_tags"],
    ["select_tag", "notes"],
    ["display_name", "science"],
    ["display_names", ["science"]],
    ["set_display_name", "science", "  Science   Lab "],
    ["set_display_name", "trilium", "Notes"],
    ["set_display_name", "math", "   "],
    ["set_display_name", "", "x"],
    ["display_name", "science"],
    ["display_names", ["trilium", "science", "other", "math"]],
    ["display_names", []],
    ["set_display_name", "science", "Second"],
    ["display_name", "science"],
    ["clear_display_name", "science"],
    ["display_name", "science"],
    ["clear_display_name", "never"],
    ["tags_for_page", "math"],
    ["all_tag_counts"],
    ["selected_tags"],
]

with tempfile.TemporaryDirectory() as tmp:
    databases.SERVICES_DIR = pathlib.Path(tmp)
    store._initialized = False
    dao = store.LandingDAO()
    results = []
    for call in SCRIPT:
        got = getattr(dao, call[0])(*call[1:])
        results.append(got)
    path = databases.service_db_path(store.SERVICE)
    db = sqlite3.connect(path)
    schema = [list(r) for r in db.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name")]
    version = db.execute("SELECT version FROM schema_version").fetchall()
    tables = {t: [list(r) for r in db.execute(f"SELECT * FROM {t} ORDER BY 1, 2" if t != "selected_tags" else f"SELECT * FROM {t} ORDER BY 1")]
              for t in ("page_tags", "selected_tags", "page_names")}
    db.close()

out = {
    "schema_version": store.SCHEMA_VERSION,
    "schema_sql": store.SCHEMA_SQL,
    "sqlite_master": schema,
    "version_rows": version,
    "script": [{"call": c, "result": r} for c, r in zip(SCRIPT, results)],
    "tables": tables,
}
dest = pathlib.Path(__file__).with_name("landing.json")
dest.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
print(f"{dest}: {len(SCRIPT)} calls")
