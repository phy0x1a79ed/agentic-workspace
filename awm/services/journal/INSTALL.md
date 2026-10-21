# Installing the `journal` service

A Python feature service in the `awm.journal` namespace. It owns the scope post
channels: messages, journal (debrief) entries, system notices and goals. It also
renders each scope's `.awm/history.md`. It needs the `awm` conda env to contain its
package plus the shared component libraries it imports (`config`, `persistence`,
`gatewayclient`). Semantic search reuses the workspace embedding stack
(`sentence-transformers`, already in the `awm` env).

## Install

    bash install.sh

`install.sh` editable-installs the component libraries and this service into the
`awm` env (override with `AWM_ENV=<name>`) and writes a gitignored `.runtime-env`
sidecar baking `AWM_PYTHON` = the env's absolute interpreter, so the gateway can
respawn the service under systemd's minimal PATH (where `mamba` is not present).

## Data model

The service owns its DB at `AWM_DIR/services/journal/journal.db`:

- **scope_posts** — one append-only log. A row is `(owner_project, owner_scope)`,
  `author`, `kind`, `body`, `meta` (JSON) and `ts` (epoch ms). A non-agent inbox
  (`user:`, `project:`, `workspace`) is a channel with `owner_project=''`. A goal is a
  post of `kind='goal'`; its level is the channel it sits on.
- **scope_subscribers** — guests enrolled on a channel.
- the shared `embeddings` tables, holding the `post` index.

A post to a literal scope is checked through the `resolveScope` RPC to the scopes
service. An unreachable scopes service never blocks a post.

A post from a Claude Code session records the session id in `meta.session`. The
service resolves it from the pid the gateway stamps on the call and drops any
`session` the caller sends. Reporting relies on it to put a journal post on the
poster's card only.

### First-start migration

Before this service existed, `scope_posts` and `scope_subscribers` lived in
`services/scopes/scopes.db`. On start, if the journal log is empty, the service
copies both tables across with `INSERT OR IGNORE`, after a sqlite backup of its own
DB (`journal.db.bak-premigrate-<stamp>`). It opens the scopes DB read-only and leaves
its tables in place. A restart finds a non-empty log and does nothing. The search
index rebuilds from the copied rows in the first backfill.

## Surface

Each function publishes as `journal_<verb>`:

- `post`, `fetch`, `subscribe`, `unsubscribe` — the channel.
- `goal_set`, `goal_retire`, `goal_read`, `goal_history` — goals, by supersede and
  tombstone only.
- `render` — writes a scope's `.awm/history.md`. `write=false` returns the text only.
- `export`, `reindex` — CLI and HTTP only. `export` pages the indexed posts for the
  search archive; `reindex` rebuilds the post index.

The service publishes the `posts` topic, once per new post, with payload
`{project, scope, post}`.

## Search archive

`service.toml` declares an `[[archive]]` table named `journals`, mode `index`, fed by
`journal_export` and pushed on the `posts` topic. `export` takes `cursor` and `limit`
and returns `{documents, next_cursor, done, ids}`. Pages run in ascending post-id
order. `ids` is the full id set and appears only on the last page.

## Run

You never invoke the service by hand in normal operation. The gateway discovers this
folder (any folder with a `run.sh` under `awm/services/`), starts it with
`bash run.sh`, and injects `AWM_HUB_URL`, `AWM_SERVICE_NAME` and `AWM_SERVICE_ID`.
Registration carries no token.

To iterate against a running sandbox without installing, use
`awm dev shadow --port 7821 awm/services/journal`.

## Tests

    mamba run -n awm python -m pytest awm/services/journal/tests -q
