#!/usr/bin/env bash
# mira-standup.sh — stand up modular AWM as a peer node on mira (host: pavilion).
#
# ⚠️  UNTESTED against the live mira host. This is a REVIEWED RUNBOOK to be run
#     step-by-step in the dedicated federation session, NOT a fire-and-forget
#     installer. It stops before every host-specific / potentially-disruptive
#     decision and asks. Read it top to bottom first.
#
# Federation v1 scope for mira (per the approved plan + operator directives):
#   - Bring the modular AWM framework onto mira so agents there inherit the
#     3-tier context docs (the onboarding fix) and mira is a full peer node.
#   - Leave the SINGLETON services (2fa, social) DISABLED on mira for v1 — do NOT
#     migrate them yet. They stay canonical on Capella until a dedicated re-home
#     session (which must prove prod `social` reaches the re-homed daemon BEFORE
#     the mira monolith is retired).
#   - Do NOT retire the monolith or the mira daemon (awm-mira-api.service) here.
#
# Non-negotiable safety:
#   - awm-mira-api.service (the Teams/Slack/OneDrive daemon prod `social` depends
#     on) is LEFT RUNNING and UNTOUCHED.
#   - The old monolith awm.service is NOT deleted. If it conflicts on :7819,
#     either stop it (after confirming nothing depends on it) or run the modular
#     gateway on an alternate AWM_PORT — see PHASE 3.
set -uo pipefail

# --- resolve paths (edit if the operator chose different locations) ---------
: "${AWM_WS:=$HOME/agentic_workspace}"      # modular workspace root on mira
: "${AWM_ENV:=awm}"                          # mamba env name
: "${MIRA_HTTPS_PORT:=12100}"                # mira's exposed HTTPS edge port
: "${AWM_PORT:=7819}"                         # loopback gateway port (see PHASE 3)
: "${CAPELLA_EDGE:=}"                         # e.g. https://10.74.81.110:12100
: "${CAPELLA_SSH_ALIAS:=capella}"            # ssh alias Capella is reachable at

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
echo "modular workspace location (AWM_WS=$AWM_WS). Sync the code from Capella, e.g.:"
cat <<EOS
    # from Capella (this repo's worktree root), pushing to mira:
    rsync -av --delete \\
      --exclude '.awm/services/*/*.db*' --exclude 'node_modules' --exclude '**/dist' \\
      --exclude '**/__pycache__' --exclude '.certs' --exclude '.runtime-env' \\
      ./awm/  mira:$AWM_WS/awm/
    # plus the workspace docs agents load (the onboarding fix):
    rsync -av ./AGENTS.md ./WORKSPACE.md ./README.md ./FEDERATION.md mira:$AWM_WS/
EOS
pause "Sync the tree, then continue on mira"
[ -d "$AWM_WS/awm/gateway" ] || { echo "!! $AWM_WS/awm/gateway not found — sync first"; exit 1; }

step "PHASE 3 — port coexistence decision (monolith on :7819)"
echo "The old monolith awm.service may bind :7819. The modular gateway also wants"
echo ":7819. The mira daemon awm-mira-api.service (172.16.0.24:7822) is SEPARATE and"
echo "stays untouched. Choose ONE:"
echo "  (a) Stop the monolith:  sudo systemctl disable --now awm.service   (if nothing needs it)"
echo "  (b) Run modular on an alternate port: set AWM_PORT (e.g. 7820) in .awm/env below."
echo "Current AWM_PORT=$AWM_PORT."
pause "Resolve the port decision, then continue"

step "PHASE 4 — mamba env + install (composition root)"
cat <<EOS
    cd $AWM_WS
    mamba env create -f awm/gateway/environment.yml   # or: mamba env update
    bash awm/gateway/install.sh                        # component libs + every service + gateway
    mamba run -n $AWM_ENV python -m awm.gateway init
EOS
pause "Create env + install, then continue"

step "PHASE 5 — per-workspace env (.awm/env)"
mkdir -p "$AWM_WS/.awm"
echo "Writing $AWM_WS/.awm/env (review before boot):"
cat > "$AWM_WS/.awm/env" <<EOS
# mira modular AWM — per-workspace env (merged into the gateway env at startup)
AWM_WORKSPACE=$AWM_WS
AWM_HTTPS_PORT=$MIRA_HTTPS_PORT
# Uncomment if coexisting with the monolith on an alternate port (PHASE 3b):
# AWM_PORT=$AWM_PORT
EOS
cat "$AWM_WS/.awm/env"

step "PHASE 6 — leave SINGLETONS disabled (v1 directive)"
# enabled.json: {name: bool}; absent ⇒ enabled. Seed BEFORE first boot so the
# singletons never start on mira. Only 2fa + social per the directive.
mkdir -p "$AWM_WS/.awm/services"
ENJSON="$AWM_WS/.awm/services/enabled.json"
python3 - "$ENJSON" <<'PY'
import json, sys, os
p = sys.argv[1]
state = {}
if os.path.exists(p):
    try: state = json.load(open(p))
    except Exception: state = {}
# Disable the singletons for v1; they stay canonical on Capella.
state["2fa"] = False
state["social"] = False
# Enable the two federation services explicitly (belt-and-braces).
state["auth"] = True
state["httpsfront"] = True
json.dump(state, open(p, "w"), indent=2, sort_keys=True)
print("wrote", p, "->", state)
PY
echo "NOTE: other host-specific services (mic/tts/stt/vpn/…) may also not belong on"
echo "mira — disable them too if desired: awm services disable <name>"

step "PHASE 7 — systemd unit for the modular gateway"
echo "Model on Capella's /etc/systemd/system/awm.service. Ensure it:"
echo "  - runs 'mamba run -n $AWM_ENV python -m awm.gateway serve' (or the baked interp),"
echo "  - sets WorkingDirectory=$AWM_WS and Environment/EnvironmentFile for AWM_WORKSPACE,"
echo "  - does NOT collide with the monolith unit name if the monolith is kept."
pause "Install + enable the unit, then continue"

step "PHASE 8 — SSH peer-auth channel (\$AWM_PEER_CRED)"
echo "Add to ~/.bashrc ABOVE the interactive guard (see FEDERATION.md — the trap):"
echo "    export AWM_PEER_CRED=\"$AWM_WS/.awm/services/auth/peer_cred.current\""
echo "Verify from Capella (dedicated ssh session): ssh mira 'cat \"\$AWM_PEER_CRED\"'"
echo "(auth writes that file on first mint once the service is enabled + running.)"

step "PHASE 9 — peer_join (both directions; nothing is synced)"
if [ -n "$CAPELLA_EDGE" ]; then
    echo "On mira:"
    echo "    awm peer join capella $CAPELLA_EDGE --ssh-alias $CAPELLA_SSH_ALIAS"
    echo "On Capella:"
    echo "    awm peer join mira <mira-edge-url> --ssh-alias mira"
else
    echo "Set CAPELLA_EDGE (e.g. https://10.74.81.110:12100) and re-run this phase."
fi

step "PHASE 10 — verify"
cat <<'EOS'
    awm services list            # auth + httpsfront running; 2fa + social DISABLED
    awm auth status              # a generation minted; peer_cred_path exists
    awm auth password            # prints the day's login password
    awm peer list                # capella recorded
    # from a browser to https://<mira-edge>:12100/  -> login page -> landing page
    # from Capella: awm peer resolve mira ; then a 2fa@... style call in the
    #   dedicated ssh session once the peer-cred fetch is wired.
EOS
echo
echo "Done reviewing. Singletons (2fa, social) remain canonical on Capella."
