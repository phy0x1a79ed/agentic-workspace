#!/usr/bin/env bash
# Build the demo chain: a Penpot drawing, reused as a component on a second
# page, embedded live in a vault note.
#
#   scripts/sirius/demo-chain.sh [username]
#
# Two verbs and the plumbing between them. `penpot-view seed-demo` creates the
# Penpot file and answers with two `file/page/board` triples; `trilium
# note-upsert` writes the note that embeds one of them. Both are idempotent, so
# this is safe to re-run and is how the demo is repaired rather than rebuilt.
#
# The note embeds the board on page *Reuse*, which is a component copy of the
# board on page *Source*. Penpot syncs a copy against its main on every edit
# within a file, so editing the source drawing changes what the note shows with
# nothing else done. That is the whole point of the demo.
#
# The `<img>` needs no new HTTP surface. `/penpot-view/` is already forwarded to
# anyone signed in, and the note is served on the same origin behind the same
# session, so the browser sends the same cookie for the picture as for the page.
#
# CAUTION The path in the `src` is absolute-rooted. A relative one resolves
# under `/trilium/` and answers with the vault's own 404 page.
set -euo pipefail

NOTE_TITLE=${DEMO_NOTE_TITLE:-Penpot demo}
NOTE_PARENT=${DEMO_NOTE_PARENT:-root}

step() { echo "== $*"; }
for tool in awm python3; do
    command -v "$tool" >/dev/null || { echo "$tool not on PATH" >&2; exit 1; }
done

# Penpot content belongs to a profile, and penpot-view's own render account is
# deliberately a read-only member of the shared team (see penpot-team.sh), so
# the seed authors as a person. Any member with an editor's seat will do. The
# default is the earliest-created credential, the same durable-account rule
# penpot-team.sh applies to the owner seat, so the demo and the team belong to
# the same person.
USER_NAME=${1:-$(awm auth penpot-list | awk -F'"' '
    /"username":/   { u = $4 }
    /"created_at":/ {
        split($0, a, ":"); c = a[2] + 0
        if (u != "" && (best == "" || c < bestc)) { best = u; bestc = c }
        u = ""
    }
    END { print best }')}
[ -n "$USER_NAME" ] \
    || { echo "!! no awm user holds a Penpot credential; run add-user.sh first" >&2; exit 1; }

step "penpot session for $USER_NAME"
# The session reaches one process as an argument and is unset immediately
# after, the same exposure add-user.sh already accepts for a Penpot password.
# It is a session rather than the credential, and the credential is rotated
# nightly, so what is briefly in `ps` outlives nothing.
TOKEN=$(awm auth penpot-session --username "$USER_NAME" \
        | sed -n 's/.*"token": "\([^"]*\)".*/\1/p')
[ -n "$TOKEN" ] || { echo "!! no Penpot session for $USER_NAME" >&2; exit 1; }

step "penpot file"
SEED=$(awm penpot-view seed-demo --token "$TOKEN")
unset TOKEN
echo "$SEED" | sed -n 's/^/   /p'

step "note \"$NOTE_TITLE\""
CONTENT=$(SEED="$SEED" python3 - <<'PY'
import json, os

d = json.loads(os.environ["SEED"])
src, reuse = d["source"], d["reuse"]
img = f'/penpot-view/{d["file_id"]}/{reuse["page_id"]}/{reuse["board_id"]}'
print(
    "<p>A Penpot board, rendered on request and embedded live: the copy on "
    "page <em>Reuse</em> of the file <em>Chain demo</em>. It is a component "
    "copy of the board on page <em>Source</em>, so editing the source drawing "
    "in Penpot changes this picture on the next reload.</p>"
    f'<p><img src="{img}"></p>'
    f'<p>Source board: <code>{d["file_id"]}/{src["page_id"]}/'
    f'{src["board_id"]}</code>. Written by '
    f'<code>scripts/sirius/demo-chain.sh</code>.</p>')
PY
)
awm trilium note-upsert --title "$NOTE_TITLE" --parent "$NOTE_PARENT" \
    --content "$CONTENT" | sed -n 's/^/   /p'

echo "the note is at /trilium/ under \"$NOTE_TITLE\""
