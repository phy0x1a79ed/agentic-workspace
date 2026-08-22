#!/usr/bin/env bash
# mira-standup.sh — stand up a proper modular AWM as a full peer node on mira
# (host: pavilion), with 2fa + social as canonical SINGLETONS on mira.
#
# ⚠️  This is a REVIEWED RUNBOOK to be walked step-by-step in the dedicated
#     federation session together with the operator, NOT a fire-and-forget
#     installer. It stops before every host-specific / disruptive decision and
#     asks. Read it top to bottom first. All SSH steps are held for the joint
#     session (fir is MFA-lockout-sensitive; the standup itself touches live
#     units on mira).
#
# Scope (per the approved plan — breaking changes AUTHORIZED, mira's current
# stack is not in active use):
#   - Replace mira's old MONOLITH (`awm.service`, pre-modular) with the modular
#     gateway on the same loopback :7819. No alt-port coexistence.
#   - Run ALL capabilities on mira, with 2fa + social ENABLED as canonical
#     singletons (mira OWNS them; capella borrows them via env selectors — see
#     PHASE 11). This is the reverse of federation v1, which left them disabled.
#   - Keep the mira DAEMON `awm-mira-api.service` (172.16.0.24:7822, + Xvfb:20 /
#     openbox / opera-teams / slack) RUNNING — social@mira fronts it. Breaking
#     changes cover restarts, NOT deleting the daemon.
#   - Stop+disable the retired `awm-exposed.service` (its log leaks).
#
# Cross-node model recap (see FEDERATION.md): each node runs its own gateway on
# its OWN loopback :7819; cross-node traffic never touches :7819 — it goes
# host-to-host through the httpsfront edge on :12100 (real IP + TLS + peer
# bearer). The only "collision" is old-monolith vs new-gateway on mira's own
# loopback, resolved by RETIRING the monolith.
set -uo pipefail

# --- resolve paths (edit if the operator chose different locations) ---------
: "${AWM_WS:=$HOME/agentic_workspace}"      # modular workspace root on mira
: "${AWM_ENV:=awm}"                          # mamba env name
: "${MIRA_HTTPS_PORT:=12100}"                # mira's exposed HTTPS edge port
: "${MIRA_DAEMON_URL:=https://172.16.0.24:7822}"   # the Teams/Slack/OneDrive daemon
: "${MIRA_DAEMON_TOKEN_FILE:=$HOME/.awm/auth.token}"  # daemon bearer (existing)
: "${CAPELLA_EDGE:=}"                         # e.g. https://10.74.81.110:12100
: "${CAPELLA_SSH_ALIAS:=capella}"            # ssh alias capella is reachable at
: "${MIRA_EDGE:=https://172.16.0.24:$MIRA_HTTPS_PORT}"  # mira's edge, seen from capella

step() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
pause() { read -r -p "  ↳ $* [enter to continue, Ctrl-C to stop] " _; }

step "PHASE 0 — stop the awm-exposed.log leak (safe, independent, do first)"
echo "The retired awm-exposed.service is spinning and its log is ~419MB+ and growing."
echo "Confirm it is the retired listener (NOT awm-mira-api), then stop+disable it:"
cat <<'EOS'
    systemctl --user status awm-exposed.service 2>/dev/null || sudo systemctl status awm-exposed.service
    # once confirmed retired:
    sudo systemctl disable --now awm-exposed.service   # or: systemctl --user
    : > ~/agentic_workspace/awm-exposed.log            # truncate the leaked log
EOS
pause "Stop the awm-exposed leak, then continue"

step "PHASE 1 — mamba/miniforge (mira has no mamba today)"
if ! command -v mamba >/dev/null 2>&1; then
    echo "Install miniforge, then re-run. e.g.:"
    echo "    curl -L -o /tmp/mf.sh https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh"
    echo "    bash /tmp/mf.sh -b -p \$HOME/lib/miniforge3 && \$HOME/lib/miniforge3/bin/mamba init bash"
    exit 1
fi
echo "mamba found: $(command -v mamba)"

step "PHASE 2 — sync the modular AWM tree to mira"
echo "The monolith lives at ~/agentic_workspace (frozen, pre-modular). Decide the"
echo "modular workspace location (AWM_WS=$AWM_WS). Sync the code from capella, e.g.:"
cat <<EOS
    # from capella (this repo's worktree root), pushing to mira:
    rsync -av --delete \\
      --exclude '.awm/services/*/*.db*' --exclude 'node_modules' --exclude '**/dist' \\
      --exclude '**/__pycache__' --exclude '.certs' --exclude '.runtime-env' \\
      --exclude '.claude/worktrees' \\
      ./awm/  mira:$AWM_WS/awm/
    # plus the workspace docs agents load (the onboarding fix) + the operator contract:
    rsync -av ./AGENTS.md ./WORKSPACE.md ./README.md ./FEDERATION.md mira:$AWM_WS/
EOS
pause "Sync the tree, then continue on mira"
[ -d "$AWM_WS/awm/gateway" ] || { echo "!! $AWM_WS/awm/gateway not found — sync first"; exit 1; }

step "PHASE 3 — RETIRE the monolith (frees :7819 for the modular gateway)"
echo "The old monolith awm.service binds :7819. The modular gateway wants the same"
echo "loopback port. Breaking changes are authorized, so retire the monolith rather"
echo "than coexist. The mira DAEMON awm-mira-api.service (172.16.0.24:7822) is"
echo "SEPARATE and MUST stay running."
cat <<'EOS'
    systemctl --user status awm.service 2>/dev/null || sudo systemctl status awm.service
    sudo systemctl disable --now awm.service    # or: systemctl --user disable --now
    # sanity: nothing left on :7819
    ss -ltnp | grep ':7819' || echo ":7819 is free"
    # confirm the daemon is UNTOUCHED and still listening:
    ss -ltnp | grep ':7822' && systemctl status awm-mira-api.service --no-pager | head -3
EOS
pause "Retire the monolith (keep the daemon), then continue"

step "PHASE 4 — mamba env + install (composition root)"
cat <<EOS
    cd $AWM_WS
    mamba env create -f awm/gateway/environment.yml   # or: mamba env update
    bash awm/gateway/install.sh                        # component libs + every service + gateway
    mamba run -n $AWM_ENV python -m awm.gateway init
EOS
echo "NOTE (two-source-trees trap): install.sh must actually install awm-config /"
echo "auth / httpsfront INTO the $AWM_ENV env — do not rely on a stale editable tree."
pause "Create env + install, then continue"

step "PHASE 5 — per-workspace env (.awm/env)"
echo "mira OWNS the singletons, so it does NOT set AWM_SOCIAL_PEER / AWM_TWOFA_PEER"
echo "(those are for BORROWING nodes — capella sets them in PHASE 11)."
mkdir -p "$AWM_WS/.awm"
echo "Writing $AWM_WS/.awm/env (review before boot):"
cat > "$AWM_WS/.awm/env" <<EOS
# mira modular AWM — per-workspace env (merged into the gateway env at startup)
AWM_WORKSPACE=$AWM_WS
AWM_HTTPS_PORT=$MIRA_HTTPS_PORT
# mira is the singleton OWNER: 2fa + social are local here, so NO peer selectors.
EOS
cat "$AWM_WS/.awm/env"

step "PHASE 6 — enable the full capability set incl. the singletons"
# enabled.json: {name: bool}; absent ⇒ enabled. Seed BEFORE first boot. mira runs
# ALL capabilities with 2fa + social ENABLED (canonical singletons). Only disable
# services that are genuinely capella-only (host-bound hardware, etc.) — review
# the list with the operator; default is to leave everything enabled.
mkdir -p "$AWM_WS/.awm/services"
ENJSON="$AWM_WS/.awm/services/enabled.json"
python3 - "$ENJSON" <<'PY'
import json, sys, os
p = sys.argv[1]
state = {}
if os.path.exists(p):
    try: state = json.load(open(p))
    except Exception: state = {}
# Singletons + federation services ENABLED on mira (belt-and-braces; absent also
# means enabled, but be explicit for the ones that matter).
for name in ("2fa", "social", "auth", "httpsfront"):
    state[name] = True
json.dump(state, open(p, "w"), indent=2, sort_keys=True)
print("wrote", p, "->", state)
PY
echo "REVIEW with operator: are any services capella-only and to be disabled here?"
echo "  (e.g. host-bound mic/tts/stt if mira has no such hardware role) — if so:"
echo "  awm services disable <name>"

step "PHASE 7 — social.toml: point social@mira at the LOCAL daemon"
echo "social@mira fronts the existing daemon at $MIRA_DAEMON_URL (it binds"
echo "172.16.0.24 only, NOT loopback). Confirm the daemon bearer exists:"
cat <<EOS
    ls -l $MIRA_DAEMON_TOKEN_FILE   # daemon bearer used by MiraConnector/OneDriveBucket
EOS
echo "Write \$AWM_DIR/social.toml with a [mira] block (review before boot):"
cat <<EOS
    [mira]
    url = "$MIRA_DAEMON_URL"
    token_file = "$MIRA_DAEMON_TOKEN_FILE"
    verify_tls = false
    # ... plus the account/bucket sections social needs (Discord/Slack/Teams/
    #     Gmail/OneDrive) — copy from capella's social.toml, keeping secrets on
    #     mira's own filesystem. Confirm each account's creds are present on mira.
EOS
pause "Write social.toml + confirm the daemon token, then continue"

step "PHASE 8 — systemd unit for the modular gateway"
echo "Model on capella's awm.service. The monolith unit is retired (PHASE 3), so the"
echo "unit may reuse the name 'awm.service' (recommended) or a new one. Ensure it:"
echo "  - runs 'mamba run -n $AWM_ENV python -m awm.gateway serve' (or baked interp),"
echo "  - sets WorkingDirectory=$AWM_WS and EnvironmentFile for AWM_WORKSPACE,"
echo "  - starts on boot; the GATEWAY bootstraps the feature services."
pause "Install + enable + start the unit, then continue"

step "PHASE 9 — SSH peer-auth channel (\$AWM_PEER_CRED)"
echo "Add to ~/.bashrc ABOVE the interactive guard (see FEDERATION.md — the trap"
echo "that silently breaks this): a non-interactive 'ssh mira cmd' does NOT source"
echo "the interactive part of ~/.bashrc."
echo "    export AWM_PEER_CRED=\"$AWM_WS/.awm/services/auth/peer_cred.current\""
echo "auth writes that file on first mint once the service is enabled + running."
echo "Verify from capella (dedicated ssh session, HELD for the joint call):"
echo "    ssh mira 'cat \"\$AWM_PEER_CRED\"'   # must return a non-empty value"

step "PHASE 10 — peer_join (both directions; nothing is synced)"
if [ -n "$CAPELLA_EDGE" ]; then
    echo "On mira:"
    echo "    awm peer join capella $CAPELLA_EDGE --ssh-alias $CAPELLA_SSH_ALIAS"
    echo "On capella:"
    echo "    awm peer join mira $MIRA_EDGE --ssh-alias mira"
else
    echo "Set CAPELLA_EDGE (e.g. https://10.74.81.110:12100) and re-run this phase."
    echo "On capella:  awm peer join mira $MIRA_EDGE --ssh-alias mira"
fi

step "PHASE 11 — capella BORROWS the mira singletons (env selectors)"
echo "On the CAPELLA gateway carrying the federation code, export the selectors so"
echo "its ssh/2fa/auth consumers route to mira (local-default otherwise):"
cat <<'EOS'
    # in capella's .awm/env (or the gateway unit's EnvironmentFile):
    AWM_SOCIAL_PEER=mira
    AWM_TWOFA_PEER=mira
EOS
echo "mira itself leaves these UNSET (it owns the singletons — PHASE 5)."
echo "NOTE: prod capella runs 'release' (no federation code); this applies to the"
echo "feat-federation gateway used for the joint verify. Making prod capella use"
echo "2fa@mira permanently is a later feat->dev->release deploy, out of scope here."

step "PHASE 12 — verify"
cat <<'EOS'
    # on mira:
    awm services list            # auth + httpsfront + 2fa + social all UP
    awm auth status              # a generation minted; peer_cred_path exists
    awm auth password            # prints the day's login password
    awm peer list                # capella recorded
    ss -ltnp | grep -E ':7819|:12100|:7822'   # gateway, edge, daemon all listening
    # from a browser:  https://<mira-edge>:12100/  -> login page -> landing page
    # from capella (feat-federation gateway):
    awm peer resolve mira
    ssh mira 'cat "$AWM_PEER_CRED"'            # HELD for the joint ssh session
    # then, TOGETHER, exactly one ssh fir through the ssh service -> 2fa@mira Duo.
EOS
echo
echo "Done reviewing. mira now OWNS 2fa + social as canonical singletons; capella"
echo "borrows them via AWM_SOCIAL_PEER / AWM_TWOFA_PEER. All SSH steps are held for"
echo "the joint session."
