# Convo inner loop

LLM-cleaned voice composer for **continuous (convo) mode**. On each silence-cut
the raw STT transcript is run through an LLM that returns a faithfully cleaned
message plus a "should this submit now" guess. PTT mode is untouched.

## Architecture

```
registry.py (STT)              convo/ (this package)             awm.agentcore (shared)
  silence-cut ─on_silence_cut─▶  ConvoSession  ─complete_json─▶  CleanupAgent
  (SEAM)                          raw_log/composer/notes          run_once(opencode, oneshot)
        ◀─ {composer}/{submit} ─  build_prompt + CONVO_SCHEMA     Zen deepseek (bare-JSON)
```

- **`cleanup.py`** (`CleanupAgent`) — the production LLM seam. Drives
  `awm.agentcore.run_once` in **opencode one-shot** mode (a fresh `opencode run`
  per silence-cut: open → send → drain → close). Turns `(prompt, schema)` into a
  parsed JSON dict via the thinking-model-safe path (`run_once(schema=...)`
  appends a bare-JSON instruction — no forced `StructuredOutput` tool_choice).
  Default model: opencode Zen's free `deepseek-v4-flash-free` (overridable via
  `CONVO_PROVIDER` / `CONVO_MODEL`). This replaced the former in-tree `agent/`
  package + warm `opencode serve` process — agentcore is the one shared harness
  layer for the whole system now.
- **`convo/`** (this package) — voice-cleanup domain logic. `ConvoSession` holds
  `raw_log` (verbatim, reset on submit), `composer` (latest cleaned), `notes_pad`
  (persists across submits), and the frontend-supplied `context`. It depends only
  on a seam object exposing `async complete_json(prompt) -> dict` (the production
  `CleanupAgent`, or a stub in tests). `on_silence_cut` accumulates and returns
  the LLM's `should_submit` *completeness* judgement — it does NOT flush. The
  registry driver gates the actual send on **complete AND no new speech** (a
  confirm-window debounce, `PTT_SUBMIT_CONFIRM`) and calls `take_submission()` to
  flush once the user stays silent.

## Flow per silence-cut

1. Append the new utterance to `raw_log`.
2. Build a prompt: instructions + notes + 2k chat-history context + transcript
   split into `prior_raw` (already composed) and `new_since_last_cut`.
3. LLM → `{cleaned_text, should_submit, notes_update?}`.
4. Broadcast `{type:"composer", text}` (always).
5. If `should_submit`, arm a confirm-window debounce (`PTT_SUBMIT_CONFIRM`, default
   2.5s). It fires `{type:"submit", text}` + flushes (`take_submission`) only if no
   new partial (= no new STT raw) arrives in the window; any new speech aborts it
   and the next cut keeps building the same message.

LLM failure (`CleanupError`) degrades to the raw accumulated text with no
auto-submit.

## One-time setup: opencode provider auth

`opencode run` reads `~/.local/share/opencode/auth.json` automatically — **no
service env var needed**. Add a Zen credential once:

```bash
opencode auth login        # choose "opencode" (Zen); paste the key
# verify the configured free model resolves:
opencode run --model opencode/deepseek-v4-flash-free "say hi"
```

Without it, calls fail and the convo loop falls back to raw text. To use a
different provider that's already authed (e.g. the official DeepSeek API):

```bash
CONVO_PROVIDER=deepseek CONVO_MODEL=deepseek-chat <run the service>
```

## Tests

```bash
# Per-dist runner (PYTHONPATH = stt dist root + components):
awm/gateway/scripts/run-tests.sh stt          # stubbed seam, no network

# Or directly:
PYTHONPATH=awm/services/stt mamba run -n awm \
    python -m pytest awm/services/stt/awm/stt/tests/test_convo.py -q
```

Real end-to-end (needs an authed provider) drives the loop with two utterances:
see the integration snippet in the scope plan / session log.
