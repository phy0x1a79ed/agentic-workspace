# awm-notes — install notes

Markdown notebook backend. Each note is a uuid-named `.md` file on disk; the
service DB owns the title-as-path tree, the FTS/embedding search indexes, a
30-day soft-delete trash, and the custom dictation vocabulary.

## Install

```bash
bash awm/services/notes/install.sh          # editable into the `awm` env
# or, from the composition root:
bash awm/gateway/install.sh                 # installs every discovered service
```

`install.sh` editable-installs the shared components (`config`, `persistence`,
`gatewayclient`) it imports, then the service, then writes the `.runtime-env`
sidecar (baked `AWM_PYTHON`) so the supervisor can respawn it under systemd.

## Dependencies

- `awm-config`, `awm-persistence`, `awm-gatewayclient` (shared imported-source components).
- Semantic search reuses `awm.persistence.embeddings` (all-MiniLM-L6-v2 +
  sqlite-vec) — the same stack as the `writing` service, no extra deps here.

## Storage

- DB: `AWM_DIR/services/notes/notes.db` (own tables: `notes`, `notes_fts`,
  `vocab`, `embeddings`).
- Note bodies: `AWM_DIR/services/notes/files/<uuid>.md` (canonical content).

## Surface

Read verbs (`search`, `get`, `tree`, `vocab_list`) are on MCP + CLI + HTTP.
Write verbs (`create`, `save`, `trash`, `restore`, `purge`, `vocab_add`,
`vocab_remove`) are gated to CLI + HTTP (off the agent MCP surface); the notes
page calls them over the `/svc/notes/fn/<fn>` proxy. Loopback, unauthenticated.

The notes **page** is a separate front-end bundle at `awm/pages/notes/`, served
at `/ui/notes/`. Dictation uses the existing `stt` service's `stream` session.
