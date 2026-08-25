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
  sqlite-vec) — the same stack as the `writing` service. It is an **opt-in
  extra**, `awm-persistence[search]`, which `install.sh` installs explicitly;
  without it every other verb still works and only semantic search degrades.

## Storage

- DB: `AWM_DIR/services/notes/notes.db` (own tables: `notes`, `notes_fts`,
  `vocab`, `embeddings`).
- Note bodies: `AWM_DIR/services/notes/files/<uuid>.md` (durable content).
- Checkouts: `AWM_DIR/services/notes/checkouts/<handle>/` — one directory per
  open working copy.
- Orphaned writes: `AWM_DIR/services/notes/orphaned/` — see *The trap*, below.

## Editing a note

**The file is not the note.** While a note is open in the browser its live copy
is an in-memory room, and the `.md` on disk lags it by up to a flush interval.
Every reader — agent, CLI, another tab — is served the room. So a write to the
file is invisible to everyone and is erased by the next flush, in both
directions, with no error anywhere. That happened: an agent rewrote sixteen
figure paths in a note, saw a successful write, and the user saw nothing.

So there is one way to write a note, and everything uses it:

    notes checkout --id <note>       →  handle
    notes path <handle>                 the working copy, edit it however you like
    notes read/write <handle>           same thing without filesystem access
    notes status <handle>               ahead / behind / conflicted
    notes update <handle>               pull the live note in — the ONLY place
                                        reconciliation happens
    notes resolve <handle>              declare a hand-resolved checkout clean
    notes merge <handle>                land it
    notes discard <handle>

`merge` **is never actually a merge.** It refuses while the checkout is behind,
so landing is a guarded single write: atomic, and incapable of producing a note
neither side asked for. All reconciliation happens in `update`, inside your own
checkout, at a moment you chose, where you can read the result before landing it.

Landing goes through the room rather than around it: the merged text becomes the
room's content under the same lock the browser's keystrokes take, and is then
pushed to every open tab. So a merge appears in the editor without anyone typing,
and keystrokes made while it landed survive — the browser folds the update in by
diffing against its own shadow rather than adopting it wholesale.

### Why the merge is dumb on purpose

The boundary uses git's line-based three-way merge (`git merge-file`, whose exit
status is the conflict count). Something markdown-aware would merge more cases —
and would also merge two writers editing the same sentence into a sentence
neither wrote, silently. A merge algorithm that cannot tell you it failed is not
a merge algorithm.

### The escape hatch

`update` conflicts land in the working copy as ordinary `<<<<<<<` markers. Edit
the file at `notes path <handle>`, then call `notes resolve`. `merge` refuses
while markers remain. This is deliberately the same thing you would do to any
other conflicted text file.

### `save` is a one-shot checkout

`notes save --id <note> --content <text>` still exists and still works, but it
**merges** rather than replaces: the text is reconciled against whatever the note
holds now, using the same three-way merge, and a genuine conflict is refused with
the checkout named. Pass the `rev` from any read verb as `--base-rev` to make the
merge base exact; without it the base is the note's file, which is right for the
case that motivates it — read the file, edit it, write it back. Use it for a
one-shot write; take a checkout for anything longer than that.

### No history

Drawio's equivalent stores diagrams in a git repo and can ask it what a document
looked like when a checkout was taken. A note is one file, overwritten in place.
So a checkout carries its own base snapshot beside the working copy. The happy
consequence is that a checkout is entirely on disk and needs no rebuilding after
a service restart; the cost is that `notes save --base-rev` can only reach a base
that still exists somewhere (the file, or the room's recent snapshot ring), and
says so rather than guessing when it cannot.

### The trap, for the cases still outside the contract

Writing the `.md` directly is still possible, and still loses — the room holds
text a person typed, and declining the flush would destroy *that* instead. What
changed is that it is no longer silent: the flusher copies the file's bytes to
`orphaned/<note>.<ts>.md` and logs at warning level before overwriting, and any
row for a note whose room and file have diverged carries `stale_file: true`
alongside the `rev` you would need to write back safely.

## Surface

Every verb is on MCP + CLI + HTTP — the three surfaces carry the same list, so
the tool an agent can see is the tool it should reach for. That is safe because
writing goes through the contract above rather than around it.

Two exceptions: `collab_open` and `collab_edit` are gated to CLI + HTTP. They are
a keystroke-level browser protocol carrying a room version, and putting them on
the agent surface would only be a way to corrupt a note. The page reaches them
over the `/svc/notes/fn/<fn>` proxy, which is not the MCP catalog. The `drawio`
service gates its editor protocol the same way and for the same reason.

Loopback, unauthenticated.

The notes **page** is a separate front-end bundle at `awm/pages/notes/`, served
at `/ui/notes/`. Dictation uses the existing `stt` service's `stream` session.
