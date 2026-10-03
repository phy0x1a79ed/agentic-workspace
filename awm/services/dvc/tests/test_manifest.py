"""The manifest and the handler table have to agree.

Cheap, and it catches the failure that is otherwise only visible at runtime as a
verb the gateway advertises and the service cannot answer.
"""

from __future__ import annotations

from awm.dvc import hub_adapter as ha

FUNCS = {f["name"]: f for f in ha.API_MANIFEST["functions"]}


def test_every_declared_function_has_a_handler():
    assert set(FUNCS) == set(ha.HANDLERS)


def test_every_function_has_a_tool_name_and_a_description():
    for name, fn in FUNCS.items():
        assert fn["tool"] == f"dvc_{name}", name
        assert len(fn.get("description", "")) > 40, name


def test_the_write_verbs_are_kept_off_the_mcp_surface():
    """An agent reads backup health; a human changes when backups run."""
    assert FUNCS["run"]["surfaces"] == ["cli", "http"]
    assert FUNCS["schedule"]["surfaces"] == ["cli", "http"]


def test_the_read_verbs_are_on_every_surface():
    # An agent asking "is the backup healthy?" needs exactly these two.
    for name in ("jobs", "runs"):
        assert "surfaces" not in FUNCS[name], name


def test_the_job_status_emitter_is_declared():
    topics = {e["topic"] for e in ha.API_MANIFEST["emitters"]}

    assert topics == {"job.status"}


def test_every_verb_that_moves_bytes_declares_a_timeout():
    for name in ("sync", "run", "pull", "push", "task"):
        assert FUNCS[name]["timeout"] >= 300, name


def test_the_run_verb_names_the_jobs_that_actually_exist():
    from awm.dvc import jobs

    described = FUNCS["run"]["params"][0]["description"]
    for name in jobs.JOBS:
        assert name in described, name
