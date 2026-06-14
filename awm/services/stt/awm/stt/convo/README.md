# Convo inner loop

Raw-transcript voice composer for **continuous (convo) mode**. On each silence-cut
the raw STT transcript is accumulated into a single composer message; once the mic
has been silent long enough the message auto-submits. There is no LLM. PTT mode is
untouched.

> The former LLM "refiner" (an `awm.agentcore` opencode one-shot that rewrote each
> cut and voted on completeness) was removed — it added ~6s of latency and the raw
> whisper text, re-passed accurately on each cut, is good enough. The
> `CONVO_REFINE`/`CONVO_PROVIDER`/`CONVO_MODEL` knobs and the `cleanup.py`/`prompt.py`
> modules are gone.

## Architecture

```
registry.py (STT)                        convo/ (this package)
  _dispatch_convo_cut ─on_silence_cut──▶  ConvoSession
    (whisper re-pass, accurate)            raw_log / composer
        ◀──── composed text ───────────    take_submission()
  _silence_loop  ── owns submit timing (trailing-silence guarantee) ──▶ {type:"submit"}
```

- **`session.py`** (`ConvoSession`) — the whole package. Holds `raw_log` (each
  finalized cut chunk, verbatim) and `composer` (their join, the message being
  built); both reset on submit. `on_silence_cut(new_raw)` appends and returns the
  composed text. `take_submission()` flushes. No external dependencies, no network.

## Flow per silence-cut

1. The registry's silence poll detects ≥ `PTT_SILENCE_HANG` (1.2s) of trailing
   silence after speech → fires a cut.
2. The cut broadcasts an instant raw preview, then re-transcribes the tail
   accurately (off the critical path) and appends it via `on_silence_cut`.
3. `{type:"composer", text}` is broadcast with the accumulated raw transcript.
4. The silence poll keeps measuring trailing silence (blip-tolerant — short
   noise regions below `PTT_VAD_BLIP_MS` are ignored). Once it reaches
   `PTT_SUBMIT_SILENCE` (2.0s) it fires `{type:"submit", text}` and flushes via
   `take_submission()`. The decision is re-derived from the live audio every
   poll, so sustained silence is guaranteed to submit and a lone blip never
   restarts the clock; genuine resumed speech folds into the same message.

## Tests

```bash
# Per-dist runner (PYTHONPATH = stt dist root + components):
awm/gateway/scripts/run-tests.sh stt          # no network

# Or directly:
PYTHONPATH=awm/services/stt mamba run -n awm \
    python -m pytest awm/services/stt/awm/stt/tests/test_convo.py -q
```
