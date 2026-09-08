#!/usr/bin/env bash
# Install a fixed set of Penpot plugins into every Penpot profile on this box.
#
#   scripts/sirius/penpot-plugins.sh
#
# Penpot keeps its plugin registry per profile rather than per instance, and
# offers no way to install one for everybody: the Plugin Manager is a dialog
# one person drives for one account. A plugin this box has decided everyone
# should have is therefore a row nobody can write through the UI. This script
# is that decision, expressed once and re-runnable.
#
# Idempotent, and self-healing rather than incremental: it sweeps every
# non-deleted profile every run, so a profile created while this was not
# running still carries the plugins after the next one.
#
# `add-user.sh` calls it after creating a Penpot profile. Run it by hand to
# fold in accounts that predate it, and after changing the set below.
#
# It only adds. A plugin somebody installed for themselves is left alone, and
# so is one dropped from the set below -- taking a plugin away from an account
# is a different decision, and not one a sweep should make on its own.
#
# CAUTION Every field of an entry is pinned in PLUGIN_SET below rather than
# read from the plugin's manifest at install time. The permissions are the
# reason: a manifest is served by whoever hosts the plugin, so reading one at
# install time would let that host widen what this box grants it, on a run
# nobody was watching. Changing the set is a commit.
#
# Host-agnostic in the same way penpot-team.sh is: where there is no Penpot
# stack it says so and exits 0, which is how a dev box runs it unchanged.
set -euo pipefail

# name|manifest url|host|code|icon|version|description|permissions
#
# `host` and `version` are Penpot's own derivation, not fields of the manifest,
# and the two are tied. A manifest with no `version` key is a version 1 plugin,
# whose host is the manifest URL with the path stripped and NO trailing slash.
# A version 2 manifest keeps the trailing slash instead. The runtime resolves
# the code as `new URL(code, host)`, so a slash added or dropped here silently
# loads a different file, or nothing.
#
# CAUTION ConnectFlow is somebody else's build, fetched from their Cloudflare
# host at every open, and it runs with content:write against whatever file is
# open. Pinning it would mean vendoring it into awm's own /penpot-plugins
# mount. See awm/services/penpot-plugins/INSTALL.md.
PLUGIN_SET=$(cat <<'EOF'
ConnectFlow|https://connectflow-plugin.pages.dev/manifest.json|https://connectflow-plugin.pages.dev|plugin.js|/icon.png|1|Generate visual connectors between objects in Penpot|content:write,content:read
EOF
)

PENPOT_COMPOSE_DIR=${PENPOT_COMPOSE_DIR:-/etc/awm/penpot}

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
# fails with a syntax error at the colon. Interpolation is what keeps a plugin
# name and a JSON document out of the SQL text, so this is not a style choice.
# `docker compose exec -T` rather than `docker exec -T`, which this docker
# build rejects outright ("unknown shorthand flag: 'T'").
psql_() { pcompose exec -T penpot-postgres psql -qtAX -U penpot -d penpot "$@"; }

# The id is a function of the manifest URL, so a second run finds the entry the
# first one wrote instead of appending a second copy of the same plugin. Shaped
# like the ids Penpot generates for itself -- version nibble 8, variant 8 --
# because it lands in the same map as theirs. Nothing parses it.
plugin_id() {
    local h
    h=$(printf '%s' "$1" | md5sum | cut -c1-32)
    printf '%s-%s-8%s-8%s-%s' \
        "${h:0:8}" "${h:8:4}" "${h:12:3}" "${h:16:3}" "${h:20:12}"
}

# Transit writes a set as an object under `~#set`, not as an array. Penpot
# decodes `~:permissions` into a Clojure set and tests membership against it,
# so an array here reads as no permissions at all rather than as an error.
perms_json() {
    local out= p
    local IFS=,
    for p in $1; do out="$out,\"$p\""; done
    printf '[%s]' "${out#,}"
}

TOTAL=$(psql_ <<'SQL'
SELECT count(*) FROM profile WHERE deleted_at IS NULL;
SQL
)
[ -n "$TOTAL" ] || { echo "!! could not read the profile table" >&2; exit 1; }

while IFS='|' read -r NAME URL HOST CODE ICON VERSION DESC PERMS; do
    [ -n "${NAME:-}" ] || continue
    [[ "$VERSION" =~ ^[0-9]+$ ]] || { echo "!! plugin \"$NAME\": version is not a number" >&2; exit 1; }
    # The entry below is assembled with printf, so a quote or a backslash in
    # any field would produce a document Postgres rejects at the ::jsonb cast,
    # several steps further on. Say which field instead.
    case "$URL$HOST$CODE$ICON$NAME$DESC$PERMS" in
        *[\"\\]*) echo "!! plugin \"$NAME\": a field holds a quote or a backslash" >&2; exit 1 ;;
    esac

    step "$NAME"
    PID=$(plugin_id "$URL")
    ENTRY=$(printf '{"~:url":"%s","~:code":"%s","~:host":"%s","~:icon":"%s","~:name":"%s","~:version":%s,"~:plugin-id":"%s","~:description":"%s","~:permissions":{"~#set":%s}}' \
        "$URL" "$CODE" "$HOST" "$ICON" "$NAME" "$VERSION" "$PID" "$DESC" "$(perms_json "$PERMS")")

    # `props` holds every preference the profile has -- the nudge amounts, the
    # tutorial flags, and on this box a plugin somebody installed themselves.
    # `||` on two jsonb objects is a shallow merge, so only the `~:plugins` key
    # is replaced, and it is rebuilt from the value already there rather than
    # from nothing. Writing the column wholesale would silently uninstall
    # everything else.
    #
    # A profile is skipped when it already carries this id, and when it carries
    # some other id with the same host and name -- which is the pair Penpot's
    # own installer treats as "already installed", and the state of any account
    # that installed the plugin by hand before this ran.
    ADDED=$(psql_ -v pid="$PID" -v entry="$ENTRY" -v host="$HOST" -v nm="$NAME" <<'SQL'
WITH added AS (
    UPDATE profile p
    SET props = COALESCE(p.props, '{}'::jsonb) || jsonb_build_object(
            '~:plugins', jsonb_build_object(
                '~:ids',  COALESCE(p.props->'~:plugins'->'~:ids',  '[]'::jsonb)
                          || to_jsonb(:'pid'::text),
                '~:data', COALESCE(p.props->'~:plugins'->'~:data', '{}'::jsonb)
                          || jsonb_build_object(:'pid'::text, :'entry'::jsonb))),
        modified_at = clock_timestamp()
    WHERE p.deleted_at IS NULL
      AND NOT (COALESCE(p.props->'~:plugins'->'~:data', '{}'::jsonb) ? :'pid')
      AND NOT EXISTS (
          SELECT 1
          FROM jsonb_each(COALESCE(p.props->'~:plugins'->'~:data', '{}'::jsonb)) e
          WHERE e.value->>'~:host' = :'host'
            AND e.value->>'~:name' = :'nm')
    RETURNING 1)
SELECT count(*) FROM added;
SQL
    )
    CARRY=$(psql_ -v host="$HOST" -v nm="$NAME" <<'SQL'
SELECT count(*) FROM profile p
WHERE p.deleted_at IS NULL
  AND EXISTS (
      SELECT 1
      FROM jsonb_each(COALESCE(p.props->'~:plugins'->'~:data', '{}'::jsonb)) e
      WHERE e.value->>'~:host' = :'host'
        AND e.value->>'~:name' = :'nm');
SQL
    )
    echo "   $CARRY of $TOTAL profile(s) carry it, $ADDED added"
    [ "$CARRY" = "$TOTAL" ] || echo "   !! $((TOTAL - CARRY)) profile(s) still without it" >&2
    # The entry is a pointer, not a copy: the code is fetched from the plugin's
    # host every time somebody opens it. A host that has gone away leaves a
    # menu entry that does nothing, and nothing else says so.
    if command -v curl >/dev/null && ! curl -fsS -o /dev/null --max-time 10 "$URL"; then
        echo "   !! $URL is unreachable; the entry is written but the plugin will not load" >&2
    fi
done <<<"$PLUGIN_SET"

echo "plugin sweep done across $TOTAL profile(s)"
echo "       a signed-in browser holds its profile in memory — reload to see the menu change"
