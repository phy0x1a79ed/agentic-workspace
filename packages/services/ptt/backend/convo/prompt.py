"""The convo cleanup domain contract: the prompt and the output schema.

This is the *domain* layer — what we ask the LLM to do and the shape we want
back. It lives in the convo subsystem (not the generic ``agent`` package),
because cleaning live speech into a composable message is the ptt service's
business, not the LLM backend's.
"""

from __future__ import annotations

# Defensive caps. The frontend already trims context to ~2k; we re-cap here so
# a misbehaving client can't blow the prompt budget. Notes are model-owned but
# bounded so they can't grow without limit across a long session.
MAX_CONTEXT_CHARS = 2000
MAX_NOTES_CHARS = 4000

# JSON Schema handed to opencode's StructuredOutput tool. The model is forced
# to return exactly this shape; ``agent.complete`` returns the validated dict.
CONVO_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "cleaned_text": {
            "type": "string",
            "description": (
                "The user's full message so far, faithfully cleaned: fix "
                "transcription errors, drop filler and false starts, add "
                "punctuation. Do NOT add information or change meaning."
            ),
        },
        "should_submit": {
            "type": "boolean",
            "description": (
                "True only if the user appears to have FINISHED the message "
                "(a complete thought, or an explicit send cue). False if they "
                "are mid-thought or likely to keep speaking."
            ),
        },
        "notes_update": {
            "type": ["string", "null"],
            "description": (
                "Optional concise scratchpad of proper nouns, spellings, "
                "domain terms, or context worth remembering for cleaning "
                "later chunks. Null to leave the notes unchanged."
            ),
        },
    },
    "required": ["cleaned_text", "should_submit"],
    "additionalProperties": False,
}


_INSTRUCTIONS = """\
You are the cleanup stage of a live voice composer. A user is dictating a \
message by speaking; speech-to-text produces raw, noisy transcripts in chunks \
separated by pauses. Each time a new chunk arrives you must reply with ONLY a \
single JSON object (no markdown fences, no commentary before or after) with \
exactly these fields:

1. cleaned_text (string) — the user's full message-so-far, faithfully cleaned. \
Fix transcription errors, remove filler/disfluencies and false starts, add \
punctuation and capitalization. DO NOT add information, answer the user, or \
change meaning. Preserve their words and intent. Stay stable: keep your prior \
cleaned version and only integrate the new chunk unless it corrects earlier text.

2. should_submit (boolean) — true ONLY if the user seems to have finished the \
message (a complete thought, a natural sign-off, or an explicit cue like "send \
it"). False if they are mid-thought or likely to continue speaking.

3. notes_update (string or null) — optionally a short scratchpad of proper \
nouns, spellings, domain terms, or context worth remembering for cleaning later \
chunks. Keep it concise. Null to leave notes unchanged.

Example reply: {"cleaned_text": "Deploy Hyperion now.", "should_submit": true, \
"notes_update": null}

The transcript below is split into ALREADY-COMPOSED text and a NEW chunk since \
the last pause. Integrate the new chunk into the full cleaned message — it may \
continue, correct, or finish the earlier text."""


def _section(title: str, body: str) -> str:
    body = body.strip()
    return f"[{title}]\n{body if body else '(none)'}"


def build_prompt(
    *,
    prior_raw: str,
    new_raw: str,
    prev_composer: str,
    notes: str,
    context: str,
) -> str:
    """Assemble the single text prompt sent to the LLM on each silence-cut.

    ``prior_raw`` is the raw transcript already composed (all earlier cuts);
    ``new_raw`` is just-arrived since the last pause — the split the model uses
    to tell what is new. ``prev_composer`` anchors stability, ``notes`` and
    ``context`` provide carry-over and conversational reference.
    """
    context = context[-MAX_CONTEXT_CHARS:] if context else ""
    notes = notes[-MAX_NOTES_CHARS:] if notes else ""
    return "\n\n".join(
        [
            _INSTRUCTIONS,
            _section("NOTES", notes),
            _section("CONVERSATION CONTEXT (reference only)", context),
            _section("ALREADY COMPOSED — raw", prior_raw),
            _section("YOUR PRIOR CLEANED VERSION", prev_composer),
            _section("NEW CHUNK SINCE LAST PAUSE — raw", new_raw),
        ]
    )
