"""Stage B of the T3 transcript-mining funnel: local-model binary filter.

The one high-volume pass. Runs a coarse yes/no classifier over every Stage-A
window — "does this human turn express a genuine user PREFERENCE / CORRECTION /
STEERING decision that would generalize as a reusable precedent?" — so the
expensive Claude judgment in Stage C only ever sees the survivors.

**Backend-agnostic.** Talks plain OpenAI-compatible ``/v1/chat/completions`` over
stdlib ``urllib`` (no deps, runs anywhere). The same code path serves a
fir-hosted llama.cpp ``llama-server`` (the intended backend) or any other
OpenAI-shaped endpoint (local ollama, etc.) — only ``--api-url``/``--model``
change, so a fir blocker degrades to a config swap, not a rewrite.

**Coarse, recall-favoring.** Stage B must not drop real decisions; precision is
Stage C's job. The prompt is tuned to say *yes* on anything that plausibly
carries a durable preference and *no* only on clear non-signal (pure task
requests, one-off factual instructions, greetings, meta-chatter).

**Resumable.** Verdicts append+flush to ``verdicts.jsonl`` keyed by window uuid;
a re-run skips already-classified windows, so a crash/wall-clock kill loses
nothing. ``--sample N`` classifies only the first N (timing + adversarial
quality check before trusting the full sweep). ``decision-points`` mode then
projects the yes-verdicts into ``decision_points.jsonl`` for Stage C.

Usage::

    # timing / quality sample
    python3 filter_windows.py classify --sample 40 \
        --api-url http://localhost:8100/v1 --model qwen2.5-14b-instruct

    # full sweep (concurrent; llama.cpp --cont-batching handles it)
    python3 filter_windows.py classify --concurrency 8 \
        --api-url http://localhost:8100/v1 --model qwen2.5-14b-instruct

    # project survivors
    python3 filter_windows.py decision-points
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

DEFAULT_IN = "/home/tony/agentic_workspace/data/awm/precedence-mining/windows.jsonl"
DEFAULT_PREFILTERED = "/home/tony/agentic_workspace/data/awm/precedence-mining/prefiltered.jsonl"
DEFAULT_VERDICTS = "/home/tony/agentic_workspace/data/awm/precedence-mining/verdicts.jsonl"
DEFAULT_POINTS = "/home/tony/agentic_workspace/data/awm/precedence-mining/decision_points.jsonl"

# --- Stage B0: cheap lexical prefilter -------------------------------------
# A deterministic, recall-favoring net that narrows the harvested windows to
# those whose human turn plausibly carries a durable preference — BEFORE any
# model sees them. On this corpus it cut 6043 -> ~1034 (17%), small enough that
# the binary classify pass runs on cheap Claude Haiku agents instead of a
# fir-hosted GPU model. Spot-checking the dropped set showed only ~3-5% recall
# leakage (soft preferences with no cue word), acceptable for a seed pass.
#
# NOISE = agent-dispatch / autopilot-resume prompts and status/shell pastes that
# are not user turns in spirit (they contain cue words but no genuine steering).
_NOISE = [
    r"^(resume|continue) (the )?(autonomous|asv|onedrive)",
    r"re-?read (the )?(sop|skill)", r"\.awm/data/plans", r"\bautopilot\b",
    r"^answer the user'?s question", r"\bScheduleWakeup\b", r"STANDING ORDERS",
    r"^\s*\d\d:\d\d:\d\d", r"LISTEN 0", r"users:\(\(", r"^\s*\$ ",
    r"sbatch|squeue|sacct|scancel", r"\[from:supervisor\]", r"<teammate-message",
]
# CUES = preference / correction / standing-rule language.
_CUES = [
    r"\bprefer", r"\balways\b", r"\bnever\b", r"\bfrom now on\b", r"\bgoing forward\b",
    r"\binstead\b", r"\brather than\b", r"\bdon'?t\b", r"\bdo not\b", r"\bshould(n'?t)?\b",
    r"\bmake sure\b", r"\bi want\b", r"\bi'?d (like|prefer|rather)\b",
    r"\bplease (use|don'?t|make|keep|stop|always|never|only)\b", r"\bactually\b",
    r"\bwe (should|need to|want|use|don'?t|prefer)\b", r"\bi (like|dislike|hate|prefer)\b",
    r"\buse .* not\b", r"\bavoid\b", r"\bconvention", r"\bin the future\b", r"\bby default\b",
    r"\bmy preference", r"\bremember (that|to)\b", r"\bkeep it\b", r"\bno need to\b",
    r"\bstop (using|doing|trying)\b", r"\bwrong,", r"\bnot like that\b",
]
_NOISE_RE = re.compile("|".join(_NOISE), re.IGNORECASE | re.MULTILINE)
_CUE_RE = re.compile("|".join(_CUES), re.IGNORECASE)


def prefilter(in_path: str, out_path: str) -> dict:
    """Lexical Stage B0: keep windows whose human turn hits a preference cue and
    is not agent-dispatch/status noise. Writes survivors verbatim to out_path."""
    kept = dropped_noise = dropped_nocue = 0
    with open(out_path, "w", encoding="utf-8") as out_fh:
        for w in _load_windows(in_path):
            t = (w.get("user_text") or "").strip()
            if _NOISE_RE.search(t):
                dropped_noise += 1
                continue
            if not _CUE_RE.search(t):
                dropped_nocue += 1
                continue
            out_fh.write(json.dumps(w, ensure_ascii=False) + "\n")
            kept += 1
    return {"kept": kept, "dropped_noise": dropped_noise,
            "dropped_nocue": dropped_nocue}

SYSTEM_PROMPT = (
    "You are a precise binary classifier mining a developer's AI-assistant chat "
    "logs for reusable PREFERENCES. Given one human message (and the assistant "
    "text it was replying to), decide whether it expresses a GENUINE, GENERALIZABLE "
    "user preference, correction, or steering decision — something worth remembering "
    "so a future agent acts the way this user wants without re-asking.\n\n"
    "Answer YES if the message does any of: states how the user wants things done "
    "(style, workflow, tooling, conventions); corrects or overrides the assistant's "
    "approach; makes a design/architecture/process decision with a rationale; sets a "
    "standing rule or boundary; expresses a like/dislike about how work is carried out.\n\n"
    "Answer NO for: pure task requests or questions with no preference ('add a test for X', "
    "'what does this function do'); one-off factual instructions specific to a single task "
    "with no reusable signal; greetings, acknowledgements, meta-chatter ('ok', 'thanks', "
    "'continue'); auto-generated agent/orchestrator dispatch prompts; status/debug pastes.\n\n"
    "Favor recall slightly: if a message plausibly carries a durable preference, say YES. "
    "Reply ONLY with a JSON object: {\"is_decision\": true|false, \"why\": \"<=12 words\"}."
)

_write_lock = threading.Lock()


def _build_user_prompt(win: dict) -> str:
    ctx = (win.get("context_text") or "").strip()
    user = (win.get("user_text") or "").strip()
    parts = []
    if ctx:
        parts.append("ASSISTANT (context it replied to):\n" + ctx[:1500])
    parts.append("HUMAN MESSAGE:\n" + user[:3000])
    return "\n\n".join(parts)


def _post_chat(api_url: str, model: str, system: str, user: str, timeout: int = 120) -> str:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.0,
        "max_tokens": 120,
        "response_format": {"type": "json_object"},
    }
    data = json.dumps(body).encode("utf-8")
    url = api_url.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload["choices"][0]["message"]["content"]


def _parse_verdict(raw: str) -> dict:
    """Lenient JSON extraction — models sometimes wrap the object in prose."""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass
    # last resort: keyword sniff
    low = raw.lower()
    return {"is_decision": ("true" in low and "false" not in low.split("true")[0]),
            "why": "unparseable: " + raw[:60]}


def _classify_one(win: dict, api_url: str, model: str) -> dict:
    try:
        raw = _post_chat(api_url, model, SYSTEM_PROMPT, _build_user_prompt(win))
        v = _parse_verdict(raw)
        is_dec = bool(v.get("is_decision"))
        why = str(v.get("why", ""))[:200]
        err = None
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, TimeoutError, OSError) as e:
        is_dec, why, err = None, "", f"{type(e).__name__}: {e}"
    return {
        "uuid": win.get("uuid"),
        "is_decision": is_dec,
        "why": why,
        "error": err,
        "session_id": win.get("session_id"),
        "timestamp": win.get("timestamp"),
        "cwd": win.get("cwd"),
        "gitBranch": win.get("gitBranch"),
    }


def _load_done_uuids(path: str) -> set:
    done = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # only count a definitive verdict as done; errored rows retry
                if r.get("uuid") and r.get("error") is None:
                    done.add(r["uuid"])
    return done


def _load_windows(path: str) -> list:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def classify(in_path: str, out_path: str, api_url: str, model: str,
             concurrency: int, sample: int | None) -> dict:
    windows = _load_windows(in_path)
    done = _load_done_uuids(out_path)
    todo = [w for w in windows if w.get("uuid") not in done]
    if sample is not None:
        todo = todo[:sample]

    print(f"windows={len(windows)} done={len(done)} todo={len(todo)} "
          f"concurrency={concurrency} model={model}", file=sys.stderr, flush=True)

    out_fh = open(out_path, "a", encoding="utf-8")
    counts = {"yes": 0, "no": 0, "err": 0}
    processed = 0
    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futs = {pool.submit(_classify_one, w, api_url, model): w for w in todo}
            for fut in as_completed(futs):
                v = fut.result()
                with _write_lock:
                    out_fh.write(json.dumps(v, ensure_ascii=False) + "\n")
                    out_fh.flush()
                if v["error"]:
                    counts["err"] += 1
                elif v["is_decision"]:
                    counts["yes"] += 1
                else:
                    counts["no"] += 1
                processed += 1
                if processed % 100 == 0:
                    print(f"  … {processed}/{len(todo)}  yes={counts['yes']} "
                          f"no={counts['no']} err={counts['err']}",
                          file=sys.stderr, flush=True)
    finally:
        out_fh.close()
    return {"processed": processed, **counts}


def build_decision_points(verdicts_path: str, windows_path: str, out_path: str) -> dict:
    """Project yes-verdicts into decision points, rejoining full window text."""
    win_by_uuid = {w["uuid"]: w for w in _load_windows(windows_path)}
    n_yes = 0
    seen = set()
    with open(out_path, "w", encoding="utf-8") as out_fh:
        with open(verdicts_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                v = json.loads(line)
                if v.get("error") is not None or not v.get("is_decision"):
                    continue
                u = v.get("uuid")
                if u in seen:
                    continue
                seen.add(u)
                win = win_by_uuid.get(u)
                if win is None:
                    continue
                out_fh.write(json.dumps({
                    "uuid": u,
                    "why": v.get("why"),
                    "session_id": win.get("session_id"),
                    "timestamp": win.get("timestamp"),
                    "cwd": win.get("cwd"),
                    "gitBranch": win.get("gitBranch"),
                    "user_text": win.get("user_text"),
                    "context_text": win.get("context_text"),
                }, ensure_ascii=False) + "\n")
                n_yes += 1
    return {"decision_points": n_yes}


def split_batches(in_path: str, out_dir: str, batches: int) -> dict:
    """Chunk prefiltered windows into N batch files for parallel Haiku agents.
    Each batch file holds a trimmed view (uuid + capped user/context text) so an
    agent can classify it without re-reading the full corpus."""
    os.makedirs(out_dir, exist_ok=True)
    rows = _load_windows(in_path)
    n = len(rows)
    per = (n + batches - 1) // batches
    written = []
    for i in range(batches):
        chunk = rows[i * per:(i + 1) * per]
        if not chunk:
            break
        path = os.path.join(out_dir, f"batch_{i:02d}.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for w in chunk:
                fh.write(json.dumps({
                    "uuid": w.get("uuid"),
                    "session_id": w.get("session_id"),
                    "timestamp": w.get("timestamp"),
                    "cwd": w.get("cwd"),
                    "gitBranch": w.get("gitBranch"),
                    "user_text": (w.get("user_text") or "")[:4000],
                    "context_text": (w.get("context_text") or "")[:2500],
                }, ensure_ascii=False) + "\n")
        written.append({"path": path, "n": len(chunk)})
    return {"total": n, "batches": len(written), "files": written}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Stage B local-model binary filter.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prefilter")
    p.add_argument("--in", dest="in_path", default=DEFAULT_IN)
    p.add_argument("--out", dest="out_path", default=DEFAULT_PREFILTERED)

    s = sub.add_parser("split")
    s.add_argument("--in", dest="in_path", default=DEFAULT_PREFILTERED)
    s.add_argument("--out-dir", dest="out_dir", required=True)
    s.add_argument("--batches", type=int, default=8)

    c = sub.add_parser("classify")
    c.add_argument("--in", dest="in_path", default=DEFAULT_IN)
    c.add_argument("--out", dest="out_path", default=DEFAULT_VERDICTS)
    c.add_argument("--api-url", required=True)
    c.add_argument("--model", required=True)
    c.add_argument("--concurrency", type=int, default=8)
    c.add_argument("--sample", type=int, default=None)

    d = sub.add_parser("decision-points")
    d.add_argument("--verdicts", dest="verdicts_path", default=DEFAULT_VERDICTS)
    d.add_argument("--windows", dest="windows_path", default=DEFAULT_IN)
    d.add_argument("--out", dest="out_path", default=DEFAULT_POINTS)

    args = ap.parse_args(argv)
    if args.cmd == "prefilter":
        stats = prefilter(args.in_path, args.out_path)
    elif args.cmd == "split":
        stats = split_batches(args.in_path, args.out_dir, args.batches)
    elif args.cmd == "classify":
        stats = classify(args.in_path, args.out_path, args.api_url, args.model,
                         args.concurrency, args.sample)
    else:
        stats = build_decision_points(args.verdicts_path, args.windows_path, args.out_path)
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
