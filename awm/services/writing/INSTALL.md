# Installing the `writing` service

A Python feature service in the `awm.writing` namespace: a searchable/curatable
corpus of the author's own prose, used as a **style reference** so an assistant
can match the voice. It needs the `awm` conda env to contain its package plus the
shared component libraries it imports (`config`, `persistence`, `gatewayclient`).
Semantic search reuses the workspace embedding stack (`sentence-transformers` +
`sqlite-vec`, already in the `awm` env).

## Install

    bash install.sh

`install.sh` editable-installs the component libraries and this service into the
`awm` env (override with `AWM_ENV=<name>`) and writes a gitignored `.runtime-env`
sidecar baking `AWM_PYTHON` = the env's absolute interpreter, so the gateway can
respawn the service under systemd's minimal PATH (where `mamba` is not present).

## One-time data migration

The service owns its DB at `AWM_DIR/services/writing/writing.db`. To bring across
the existing curated corpus (samples, tags, dedup/grade curation, and embeddings
copied verbatim — no re-embedding) from the historical self-improvement corpus:

    mamba run -n awm python -m awm.writing.seed          # default corpus dir
    mamba run -n awm python -m awm.writing.seed /path/to/writing_samples

Idempotent (keyed on sample id) — safe to re-run.

## Run

You never invoke the service by hand in normal operation. The gateway discovers
this folder (any folder with a `run.sh` under `awm/services/`), starts it with
`bash run.sh`, and injects the only three env vars the adapter reads:

| Env var | Set by | Meaning |
|---|---|---|
| `AWM_HUB_URL` | gateway | base URL of the running gateway |
| `AWM_SERVICE_NAME` | gateway | this service's name (= folder name) |
| `AWM_SERVICE_ID` | gateway | assigned on respawn so reconnect targets the same control URL |

No auth — the registration handshake carries no token.

## Surface

Read verbs (`writing_search` / `writing_get` / `writing_stats` / `writing_vocab`
/ `writing_feed`) project onto MCP + CLI + HTTP — any agent can search the corpus.
Write / maintenance verbs (`writing_add` / `writing_retag` / `writing_remove` /
`writing_link_dup` / `writing_unlink_dup` / `writing_import` / `writing_embed` /
`writing_dedup`) declare `surfaces: [cli, http]`, so they are reachable only from
the host `awm writing …` CLI (and loopback HTTP) — not advertised to spawned
agents on the MCP surface. This is a surface gate, not an auth boundary; the
loopback control plane is unauthenticated by design.

To iterate against a running sandbox without installing, use
`awm dev shadow awm/services/writing`; it execs this same `run.sh` as an overlay.
