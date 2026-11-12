#!/usr/bin/env bash
# Renew productivity's Google Drive refresh token on alcor: productivity-gdrive-auth.sh [bucket]
# From your laptop first: ssh -L 8765:127.0.0.1:8765 alcor, then run this there and open the URL it prints.
set -euo pipefail
bucket=${1:-gdrive-phyberosis}
dir=/var/lib/awm/productivity
bin=${AWM_BIN:-$HOME/agentic_workspace/.awm/bin/productivity}
client_id=$(sudo sed -n "/^\[bucket\.$bucket\]/,/^\[/s/^client_id *= *\"\(.*\)\"/\1/p" "$dir/productivity.toml")
[[ -n $client_id ]] || { echo "no client_id for [bucket.$bucket] in $dir/productivity.toml" >&2; exit 1; }
secret=$(sudo cat "$dir/secrets/$bucket.secret")
token=$("$bin" gdrive-auth --client-id "$client_id" --client-secret "$secret" --port 8765 --no-browser | tee /dev/stderr | tail -n 1)
printf '%s\n' "$token" | sudo -u awm-productivity tee "$dir/secrets/$bucket.token" >/dev/null
sudo systemctl restart awm-plugin@productivity
echo "saved a new refresh token for $bucket and restarted productivity"
