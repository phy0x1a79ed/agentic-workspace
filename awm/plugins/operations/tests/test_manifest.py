from awm.operations.api import API_MANIFEST, TOOL


def test_each_function_publishes_its_full_tool_name():
    assert all(f["tool"] == f"{TOOL}_{f['name']}" for f in API_MANIFEST["functions"])
