# Installing the `graphify` service (knowledge graph of the awm source tree)

A Python feature service in the `awm.graphify` namespace. It wraps the
[`graphify`](https://github.com/safishamsi/graphify) CLI to keep a queryable
knowledge graph of the awm codebase, and exposes a thin `build` / `query` /
`path` / `status` surface over the gateway catalog (MCP + CLI + HTTP) so agents
can ask structural questions about awm. On the collapsed MCP surface these are
verbs under the single `graphify` domain tool (`graphify(verb="build")`,
`verb="query"`, `verb="path"`, `verb="status"`); CLI/HTTP stay expanded as
`graphify_*` (`awm graphify build`, `POST /invoke {name:"graphify_build"}`).
Call `graphify(verb="describe")` to list verbs and their parameter schemas.

**AST-only, local, no API key.** The graph is built from tree-sitter ASTs only;
no LLM backend or key is involved (see *Indexing policy* below). Build of the
full awm tree (~440 code files) is ~5s and incremental across runs.

## Install

    bash install.sh

`install.sh`:
1. editable-installs the component libraries (`config`, `gatewayclient`) and
   this service into the `awm` env (override with `AWM_ENV=<name>`);
2. provisions the `graphify` CLI into its **own** isolated env (override with
   `GRAPHIFY_ENV=<name>`, default `graphify`) — its 30+ tree-sitter grammar
   wheels + numpy are kept out of the `awm` env;
3. writes a gitignored `.runtime-env` sidecar baking `AWM_PYTHON` (the env's
   absolute interpreter) and `GRAPHIFY_BIN` (the graphify executable), so the
   gateway can respawn the service under systemd's minimal PATH (no `mamba`).

## Python dependencies

| Dep | Why |
|---|---|
| `awm-config`, `awm-gatewayclient` | component libs (ServiceAdapter register/control loop; `SERVICES_DIR`) |

The graphify CLI itself lives in its own env, invoked as a subprocess via
`GRAPHIFY_BIN` — it is **not** a dependency of the `awm` env.

## Indexing policy — `awm/.graphifyignore`

graphify requires an LLM key only for *semantic* extraction of doc/paper/image
files. A committed `awm/.graphifyignore` excludes all doc-class extensions
(`*.md`, `*.yaml`, `*.html`, …) plus build output / vendored dirs, so the corpus
is **code-only** and the build is pure-local AST with no key. As a second
guarantee, the service strips known LLM API-key env vars from the build
subprocess, so a build can never make a paid call. To add semantic doc
extraction later, relax that ignore file and configure a backend.

## What it tracks — "the active tree"

By default the service indexes the awm source root of the worktree **it runs
in**: under the editable install that is the release tree; under a dev sandbox
(`DEV_PYTHONPATH`) it is that sandbox's worktree. Override per-build with the
`target` parameter, or globally with `GRAPHIFY_TARGET=<path>`.

The generated graph lives under `$AWM_DIR/services/graphify/<hash>/graphify-out/`
(one subdir per indexed target) — **never** inside the indexed worktree.

## Env overrides

| Var | Default | Effect |
|---|---|---|
| `GRAPHIFY_BIN` | from `.runtime-env` / PATH | absolute path to the graphify executable |
| `GRAPHIFY_TARGET` | the awm tree this service runs in | source tree to index |
| `GRAPHIFY_ENV` (install only) | `graphify` | isolated env install.sh provisions the CLI into |

## Verify

    awm services list            # graphify → running
    awm graphify build           # ~5s; returns nodes/edges/built_at
    awm graphify status          # exists=true, node/edge counts
    awm graphify query --question "what connects the gateway to the scopes service"
    awm graphify path --a serve --b create_session

Via MCP (collapsed domain surface):

    graphify(verb="describe")    # list verbs + param schemas
    graphify(verb="build")
    graphify(verb="status")
