"""Stage A of the T3 transcript-mining funnel: harvest human-turn *windows*.

Deterministic, no model. Walks every Claude Code ``.jsonl`` transcript under a
projects root and extracts each genuine human turn together with the preceding
assistant turn(s) for context (chased via the ``parentUuid`` chain). The output
window pool is what Stage B's local-model binary filter narrows down.

Intentionally **stdlib-only** so it runs under a bare ``python3`` with no awm
import chain / PYTHONPATH setup — it never touches the service DB.

A "genuine human turn" is a record with ``type == "user"`` whose
``message.content`` is a *string* (list content is a tool-result envelope). We
additionally drop:

- sidechain / meta records (subagent-internal or harness bookkeeping turns),
- the ``<...>``-tagged harness envelopes that arrive as user strings
  (``<local-command-caveat>``, ``<command-name>``, ``<system-reminder>``, and
  any other leading-tag payload),
- ``Caveat:`` / ``[Request interrupted`` system strings,
- pure slash-command turns and too-short strings.

Agent-to-agent dispatch prompts (e.g. ``[from:orchestrator]`` worker objectives)
are *kept* here — they are hard to distinguish structurally and Stage B's
preference/no-preference classifier rejects them cheaply.

Resumable: windows append+flush to ``windows.jsonl`` and each fully-processed
transcript path is recorded in a sibling ``.done`` set, so a crash or wall-clock
kill loses at most the file in flight (whose windows carry unique uuids, so a
re-run only risks a duplicate row, not lost data).

Usage::

    python3 scrape_transcripts.py \
        --projects-root /home/tony/.claude/projects \
        --out /home/tony/agentic_workspace/data/awm/precedence-mining/windows.jsonl
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------

# Leading markers that identify a harness envelope masquerading as a user turn.
_ENVELOPE_PREFIXES = (
    "<local-command-caveat>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<system-reminder>",
    "Caveat:",
    "[Request interrupted",
    # auto-generated compaction preamble injected on session continuation —
    # a summary of prior work, not a genuine human turn
    "This session is being continued from a previous conversation",
)

_MIN_USER_LEN = 12  # a genuine steering turn is never a 3-char "ok"
_MAX_CONTEXT_CHARS = 2000  # cap per assistant context blob
_MAX_CONTEXT_TURNS = 2  # how many preceding assistant turns to chase
_MAX_USER_CHARS = 8000  # cap the stored user_text (long pastes add no signal)


def _is_genuine_user_text(s: str) -> bool:
    """True if ``s`` looks like a real human turn, not a harness envelope."""
    if len(s) < _MIN_USER_LEN:
        return False
    if s.startswith(_ENVELOPE_PREFIXES):
        return False
    # a leading tag we didn't enumerate is still an envelope, not prose
    if s.startswith("<") and ">" in s[:60]:
        return False
    # pure slash command (single token, no spaces/newlines)
    if s.startswith("/") and (" " not in s and "\n" not in s):
        return False
    return True


def _assistant_text(record: dict) -> str:
    """Join the text blocks of an assistant record's message content."""
    msg = record.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            t = block.get("text")
            if isinstance(t, str) and t.strip():
                parts.append(t.strip())
    return "\n".join(parts).strip()


def _context_for(record: dict, by_uuid: dict) -> str:
    """Walk the parentUuid chain collecting up to N preceding assistant turns."""
    blobs = []
    seen = set()
    cur = record.get("parentUuid")
    while cur and cur not in seen and len(blobs) < _MAX_CONTEXT_TURNS:
        seen.add(cur)
        parent = by_uuid.get(cur)
        if parent is None:
            break
        if parent.get("type") == "assistant":
            txt = _assistant_text(parent)
            if txt:
                blobs.append(txt[:_MAX_CONTEXT_CHARS])
        cur = parent.get("parentUuid")
    # oldest-first reads naturally as lead-up to the user turn
    blobs.reverse()
    return "\n---\n".join(blobs)


# --------------------------------------------------------------------------
# Harvest
# --------------------------------------------------------------------------


def _load_records(path: str) -> list:
    records = []
    try:
        fh = open(path, encoding="utf-8")
    except OSError:
        return records
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def harvest_file(path: str) -> list:
    """Return the list of window dicts extracted from one transcript file."""
    records = _load_records(path)
    if not records:
        return []
    by_uuid = {r.get("uuid"): r for r in records if r.get("uuid")}
    windows = []
    for r in records:
        if r.get("type") != "user":
            continue
        if r.get("isSidechain") or r.get("isMeta"):
            continue
        msg = r.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        s = content.strip()
        if not _is_genuine_user_text(s):
            continue
        windows.append({
            "session_id": r.get("sessionId"),
            "uuid": r.get("uuid"),
            "timestamp": r.get("timestamp"),
            "cwd": r.get("cwd"),
            "gitBranch": r.get("gitBranch"),
            "user_text": s[:_MAX_USER_CHARS],
            "context_text": _context_for(r, by_uuid),
        })
    return windows


def _load_done(done_path: str) -> set:
    done = set()
    if os.path.exists(done_path):
        with open(done_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    done.add(line)
    return done


def _load_seen_uuids(out_path: str) -> set:
    """Pre-load uuids already emitted so a resumed run doesn't re-add them.

    The same session file can appear under multiple project dirs (and a resumed
    session repeats turns), so uuids are the dedup key across the whole pool.
    """
    seen = set()
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    u = json.loads(line).get("uuid")
                except json.JSONDecodeError:
                    continue
                if u:
                    seen.add(u)
    return seen


def run(projects_root: str, out_path: str) -> dict:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    done_path = out_path + ".done"
    done = _load_done(done_path)
    seen_uuids = _load_seen_uuids(out_path)

    files = sorted(glob.glob(os.path.join(projects_root, "**", "*.jsonl"), recursive=True))
    todo = [f for f in files if f not in done]

    out_fh = open(out_path, "a", encoding="utf-8")
    done_fh = open(done_path, "a", encoding="utf-8")

    total_windows = 0
    processed = 0
    try:
        for path in todo:
            wins = harvest_file(path)
            written = 0
            for w in wins:
                u = w.get("uuid")
                if u and u in seen_uuids:
                    continue
                if u:
                    seen_uuids.add(u)
                out_fh.write(json.dumps(w, ensure_ascii=False) + "\n")
                written += 1
            out_fh.flush()
            os.fsync(out_fh.fileno())
            done_fh.write(path + "\n")
            done_fh.flush()
            os.fsync(done_fh.fileno())
            total_windows += written
            processed += 1
            if processed % 200 == 0:
                print(f"  … {processed}/{len(todo)} files, {total_windows} windows so far",
                      file=sys.stderr, flush=True)
    finally:
        out_fh.close()
        done_fh.close()

    return {
        "total_transcripts": len(files),
        "already_done": len(done),
        "processed_this_run": processed,
        "windows_this_run": total_windows,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Harvest human-turn windows from Claude Code transcripts.")
    ap.add_argument("--projects-root", default="/home/tony/.claude/projects")
    ap.add_argument("--out",
                    default="/home/tony/agentic_workspace/data/awm/precedence-mining/windows.jsonl")
    args = ap.parse_args(argv)

    stats = run(args.projects_root, args.out)
    print(json.dumps(stats, indent=2))
    # final authoritative window count in the file
    n = 0
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as fh:
            for _ in fh:
                n += 1
    print(f"TOTAL WINDOWS IN {args.out}: {n}")


if __name__ == "__main__":
    main()
