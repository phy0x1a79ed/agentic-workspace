#!/usr/bin/env bash
# Put every Penpot profile on this box into one shared team, and make that
# team where each person lands.
#
#   scripts/sirius/penpot-team.sh
#
# Penpot ships one private team per profile. The vault beside it is one shared
# vault, and diagrams are meant to work the same way: everyone sees everyone's
# figures, and penpot-view's render account sees them too, which is what lets
# a board render into a note somebody else wrote. This script is that decision,
# expressed once and re-runnable.
#
# Idempotent, and self-healing rather than incremental: it sweeps every
# non-deleted profile every run, so a profile created while this was not
# running still ends up in the team on the next one.
#
# `add-user.sh` calls it after creating a Penpot profile. Run it by hand to
# fold in accounts that predate it.
#
# Membership is written to Penpot's own Postgres. The alternative is
# `create-team-invitation`, which mails a token, and this box has no SMTP --
# so an invitation would be a row nobody could ever accept. The team itself is
# still created over the RPC by a real member, so Penpot builds the owner row
# and the team's Drafts project rather than this script assembling half a team
# by hand.
#
# CAUTION The render account is deliberately can_edit=false and never the
# owner. `get-file` and `export-shapes` need neither, and an owner row on a
# shared team turns a deleted profile into everybody's team deleted with it.
#
# Host-agnostic in the same way add-user.sh is: where there is no Penpot
# stack it says so and exits 0, which is how a dev box runs it unchanged.
set -euo pipefail

TEAM_NAME=${PENPOT_SHARED_TEAM:-Shared}
[[ "$TEAM_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9\ _-]{0,63}$ ]] \
    || { echo "bad team name: $TEAM_NAME" >&2; exit 1; }
PENPOT_COMPOSE_DIR=${PENPOT_COMPOSE_DIR:-/etc/awm/penpot}
# The frontend nginx on loopback -- the same origin penpot-view uses, and not
# the edge, whose policy refuses most of Penpot's own commands.
PENPOT_RPC_BASE=${PENPOT_RPC_BASE:-http://127.0.0.1:9001}

step() { echo "== $*"; }

# /etc/awm is root:awm 0750 and the dev user who runs this is not in group awm,
# so a plain `[ -f ]` answers "no stack" on the one box that has one. Ask again
# through sudo, which every step below needs anyway.
penpot_stack_here() {
    [ -f "$PENPOT_COMPOSE_DIR/docker-compose.yml" ] && return 0
    sudo -n test -f "$PENPOT_COMPOSE_DIR/docker-compose.yml" 2>/dev/null
}
if ! penpot_stack_here || ! command -v docker >/dev/null; then
    echo "   no penpot stack at $PENPOT_COMPOSE_DIR — skipped"
    exit 0
fi

pcompose() {
    sudo docker compose -p awm-penpot \
        -f "$PENPOT_COMPOSE_DIR/docker-compose.yml" \
        -f "$PENPOT_COMPOSE_DIR/docker-compose.sirius.yml" "$@"
}
# -qtAX: one bare value per line, no headers, no alignment, no ~/.psqlrc.
# Every query arrives on stdin rather than through `-c`, because psql does not
# interpolate `:'var'` into a `-c` string -- it reaches the server verbatim and
# fails with a syntax error at the colon. Interpolation is what keeps a name
# out of the SQL text, so this is not a style choice.
# `docker compose exec -T` rather than `docker exec -T`, which this docker
# build rejects outright ("unknown shorthand flag: 'T'").
psql_() { pcompose exec -T penpot-postgres psql -qtAX -U penpot -d penpot "$@"; }

# The render account, so it can be given a read-only seat. Named in
# /etc/awm/env, which only root and group awm may read. Absent -- on a dev box,
# say -- nothing is singled out and everybody gets an editor's seat.
SVC_EMAIL=${PENPOT_SERVICE_USERNAME:-$(
    sudo -n sed -n 's/^PENPOT_SERVICE_USERNAME=//p' /etc/awm/env 2>/dev/null || true)}

# Who holds the owner seat. The earliest-created credential rather than the
# first alphabetically: on this box that is whoever set it up, where the
# alphabetical answer was "guest". Recomputed every run and re-applied below,
# so a team that was created with the wrong owner is repaired rather than
# left. Never the render account -- deleting that profile would take
# everyone's team with it.
read -r OWNER OWNER_EMAIL <<<"$(
    awm auth penpot-list | awk -F'"' '
        /"username":/  { u = $4 }
        /"email":/     { e = $4 }
        /"created_at":/ {
            split($0, a, ":"); c = a[2] + 0
            if (u != "" && (best == "" || c < bestc)) { best = u; beste = e; bestc = c }
            u = ""; e = ""
        }
        END { print best, beste }')"
if [ -n "${PENPOT_TEAM_OWNER:-}" ]; then
    OWNER=$PENPOT_TEAM_OWNER
    OWNER_EMAIL=$(awm auth penpot-list \
        | awk -F'"' -v want="$OWNER" '
            /"username":/ { u = $4 }
            /"email":/    { if (u == want) { print $4; exit } }')
fi
[ -n "$OWNER" ] && [ -n "$OWNER_EMAIL" ] \
    || { echo "!! no awm user holds a Penpot credential; cannot own the shared team" >&2; exit 1; }

find_team() {
    psql_ -v nm="$TEAM_NAME" <<'SQL' | tr -d '\r'
SELECT id FROM team WHERE name = :'nm' AND deleted_at IS NULL ORDER BY created_at;
SQL
}

step "team \"$TEAM_NAME\""
TEAM_IDS=$(find_team)
COUNT=$(printf '%s' "$TEAM_IDS" | grep -c . || true)
if [ "$COUNT" -gt 1 ]; then
    # Penpot puts no unique constraint on team.name, so "the one called
    # Shared" is a question with several answers here. Guessing would move
    # everybody's default team to whichever row sorted first.
    echo "!! $COUNT teams are named \"$TEAM_NAME\"; refusing to guess:" >&2
    echo "$TEAM_IDS" >&2
    exit 1
fi
if [ "$COUNT" -eq 0 ]; then
    # Created by a real member over the RPC, so Penpot writes the owner row
    # and the team's Drafts project itself.
    TOKEN=$(awm auth penpot-session --username "$OWNER" \
            | sed -n 's/.*"token": "\([^"]*\)".*/\1/p')
    [ -n "$TOKEN" ] || { echo "!! no Penpot session for $OWNER" >&2; exit 1; }
    curl -fsS -o /dev/null -X POST \
        "$PENPOT_RPC_BASE/api/rpc/command/create-team" \
        -H 'content-type: application/transit+json' \
        -H 'accept: application/transit+json' \
        -H "cookie: auth-token=$TOKEN" \
        --data-binary "{\"~:name\":\"$TEAM_NAME\"}"
    unset TOKEN
    TEAM_IDS=$(find_team)
    COUNT=$(printf '%s' "$TEAM_IDS" | grep -c . || true)
    [ "$COUNT" -eq 1 ] || { echo "!! create-team ran but $COUNT teams are named \"$TEAM_NAME\"" >&2; exit 1; }
    echo "   created, owned by $OWNER"
else
    echo "   exists"
fi
TEAM_ID=$(printf '%s' "$TEAM_IDS" | head -1)

step "members"
BEFORE=$(psql_ -v team="$TEAM_ID" <<'SQL'
SELECT count(*) FROM team_profile_rel WHERE team_id = :'team'::uuid;
SQL
)
psql_ -v team="$TEAM_ID" -v svc="$SVC_EMAIL" <<'SQL'
INSERT INTO team_profile_rel (team_id, profile_id, can_edit)
SELECT :'team'::uuid, p.id, (p.email IS DISTINCT FROM :'svc')
FROM profile p
WHERE p.deleted_at IS NULL
ON CONFLICT (team_id, profile_id) DO UPDATE SET can_edit = EXCLUDED.can_edit;
SQL
AFTER=$(psql_ -v team="$TEAM_ID" <<'SQL'
SELECT count(*) FROM team_profile_rel WHERE team_id = :'team'::uuid;
SQL
)
echo "   $AFTER member(s), $((AFTER - BEFORE)) added"

step "landing team"
# Everyone's own Default team stays in the switcher -- that is Penpot's, not
# ours to delete. This only changes which one the dashboard opens on.
LANDED=$(psql_ -v team="$TEAM_ID" <<'SQL'
WITH moved AS (
    UPDATE profile SET default_team_id = :'team'::uuid,
                       modified_at = clock_timestamp()
    WHERE deleted_at IS NULL
      AND default_team_id IS DISTINCT FROM :'team'::uuid
    RETURNING 1)
SELECT count(*) FROM moved;
SQL
)
echo "   $LANDED profile(s) moved"

step "owner"
# Enforced every run, not only at creation: the seat is what decides who can
# delete the team, and it has to be a durable account rather than whichever
# one happened to run this first.
MOVED=$(psql_ -v team="$TEAM_ID" -v owner="$OWNER_EMAIL" <<'SQL'
WITH seated AS (
    UPDATE team_profile_rel r
    SET is_owner = (p.email = :'owner'),
        is_admin = (p.email = :'owner')
    FROM profile p
    WHERE r.profile_id = p.id
      AND r.team_id = :'team'::uuid
      AND (r.is_owner, r.is_admin) IS DISTINCT FROM ((p.email = :'owner'), (p.email = :'owner'))
    RETURNING 1)
SELECT count(*) FROM seated;
SQL
)
echo "   $OWNER, $MOVED seat(s) corrected"

echo "shared team \"$TEAM_NAME\" is $TEAM_ID"
