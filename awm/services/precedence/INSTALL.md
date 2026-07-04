# Installing the `precedence` service

A Python feature service in the `awm.precedence` namespace: a semantically-searchable
**archive of user-adjustment decisions** (a past context + the question that arose +
what was decided), so an autonomous agent can ask "was this decided before?" and act
on the answer instead of guessing or re-asking. It needs the `awm` conda env to
contain its package plus the shared component libraries it imports (`config`,
`persistence`, `gatewayclient`). Semantic search reuses the workspace embedding stack
(`sentence-transformers` + `sqlite-vec`, already in the `awm` env).

## Install

    bash install.sh

`install.sh` editable-installs the component libraries and this service into the
`awm` env (override with `AWM_ENV=<name>`) and writes a gitignored `.runtime-env`
sidecar baking `AWM_PYTHON` = the env's absolute interpreter, so the gateway can
respawn the service under systemd's minimal PATH (where `mamba` is not present).

## Data model

The service owns its DB at `AWM_DIR/services/precedence/precedence.db`:

- **decisions** — the free-text triple (`context`, `question`, `decision`) plus
  `created` (when the preference was formed) / `added` (row birth), a
  `status` lifecycle (`active`|`superseded`|`retired`) + `superseded_by`,
  provenance (`source`/`source_ref`), and the reputation columns
  (`upvotes`/`downvotes`/`seen_count`/`last_seen`).
- **notes** — change-signal annotations attached to a decision.
- **tags** — a flat, open tag set for AND-filtering.
- plus the shared per-service `embeddings` + `decisions_fts` tables. Each decision
  is embedded **per field** (three rows: `precedence_context` /
  `precedence_question` / `precedence_decision`) so a search can query any subset
  of the entry shape and score each field's match independently.

## Seeding

The archive is seeded from existing history in two hand-curated steps: **scrape**
candidates, then **import** the curated file.

**1. Scrape** — `awm.precedence.scrape` holds pure functions (never touch the DB)
that mine three sources into candidate rows, and `seed.py scrape` concatenates them
into one candidate manifest for review:

    mamba run -n awm python -m awm.precedence.seed scrape --out candidates.json

The sources, in descending signal: **feedback memories** (`feedback_*.md` in the
auto-memory dir — the user's own corrections), **operator posts** (`scope_posts`
authored by `user:operator`, behind a light test-noise gate), and **journal
decisions** (`scope_posts` `kind='journal'`, `meta.decisions[]` — agent-origin, so
tagged `journal-sourced` + a lower-confidence note; `--all-journal` disables the
"names the user" filter). Override sources with `--memory-dir` / `--scopes-db`.

**2. Curate + import** — review the candidate JSON by hand (drop noise, dedupe, fix
phrasing, attach `context-change` notes where a premise changed), then load the
curated file:

    awm precedence import --manifest-path /path/to/staging.json
    mamba run -n awm python -m awm.precedence.seed import /path/to/staging.json

Import is idempotent (keyed on a stable id — hash of `source_ref`, else the triple),
so re-running updates in place rather than duplicating.

The day-one curated seed ships at **`seed/staging.json`** (4 feedback memories + 8
durable journal-sourced preferences; operator posts were all test noise or one-off
task directives, so none survived curation). Reload it any time with:

    mamba run -n awm python -m awm.precedence.seed import awm/services/precedence/seed/staging.json

## Surface

Read / contribute verbs (`precedence_search` / `precedence_get` / `precedence_stats`
/ `precedence_add` / `precedence_note` / `precedence_vote`) project onto MCP + CLI +
HTTP — agents both consume the archive *and* grow it (add entries, attach notes,
vote). Curation verbs (`precedence_edit` / `precedence_supersede` /
`precedence_remove` / `precedence_merge` / `precedence_import` / `precedence_embed`)
declare `surfaces: [cli, http]`, so they are reachable only from the host
`awm precedence …` CLI (and loopback HTTP) — not advertised to spawned agents on the
MCP surface. This split is what keeps the archive both self-growing and clean; it is
a surface gate, not an auth boundary (the loopback control plane is unauthenticated
by design).

## Run

You never invoke the service by hand in normal operation. The gateway discovers this
folder (any folder with a `run.sh` under `awm/services/`), starts it with
`bash run.sh`, and injects the only three env vars the adapter reads:

| Env var | Set by | Meaning |
|---|---|---|
| `AWM_HUB_URL` | gateway | base URL of the running gateway |
| `AWM_SERVICE_NAME` | gateway | this service's name (= folder name) |
| `AWM_SERVICE_ID` | gateway | assigned on respawn so reconnect targets the same control URL |

No auth — the registration handshake carries no token.

To iterate against a running sandbox without installing, use
`awm dev shadow --port 7821 awm/services/precedence`; it execs this same `run.sh`
as an overlay.
