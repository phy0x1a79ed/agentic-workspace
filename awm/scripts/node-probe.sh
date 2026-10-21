#!/usr/bin/env bash
# Print this node's setup as one `layer:item` or `layer:item=value` line per fact,
# for node-diff.sh to compare. Read-only, runs as tony without sudo, and runs
# from stdin over ssh, so it may not source anything beside it.
#
# It never prints a value from .awm/env or a key file: env keys are names only,
# and a file whose content matters is printed as an md5.
set -uo pipefail

W=${AWM_WORKSPACE:-$HOME/agentic_workspace}
AWM_BIN=$HOME/lib/miniforge3/envs/awm/bin
export PATH=$HOME/.local/bin:$HOME/bin:$HOME/.opencode/bin:$AWM_BIN:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin
NOISE='\.bak|bak-|\.pre-|\.before-|-shm$|-wal$|\.log$|__pycache__'

emit() { local layer=$1; sed "s|^|$layer:|"; }
names() { ls -A "$@" 2>/dev/null | grep -vE "$NOISE" | sort -u; }
md5() { [[ -e $2 ]] && echo "$1=$(md5sum <"$2" | cut -c1-12)"; }

echo "node:$(grep -oP '^(export )?AWM_NODE_NAME=\K\S+' "$W/.awm/env" | tr -d "\"'")"

grep -oE '^(export )?[A-Za-z_][A-Za-z0-9_]*=' "$W/.awm/env" | sed 's/^export //; s/=$//' | sort -u | emit env
python3 -S -c "
import json, sys
for k, v in sorted(json.load(open(sys.argv[1])).items()):
    print(f'enabled:{k}={str(v).lower()}')" "$W/.awm/services/enabled.json"
awm services list 2>/dev/null | awk 'NR>1 && $3=="True" {print $1}' | emit running
names "$W/.awm" | emit awm-dotdir
names "$W/.awm/services" | emit awm-svc-state
echo "release=$(git -C "$W" rev-parse --short HEAD 2>/dev/null)" | emit checkout

for f in "$HOME"/.claude/skills/*/SKILL.md; do
  [[ -e $f ]] && md5 "$(basename "$(dirname "$f")")" "$f"
done | emit claude-skill
names "$HOME/.claude/agents" | emit claude-agent
names "$HOME/.claude/commands" | emit claude-command
for f in $(names "$HOME/.claude/hooks"); do md5 "$f" "$HOME/.claude/hooks/$f"; done | emit claude-hook
md5 CLAUDE.md "$HOME/.claude/CLAUDE.md" | emit claude-file
python3 -S -c "
import json, os
h = os.path.expanduser('~')
def load(p):
    try: return json.load(open(p))
    except Exception: return {}
for k in sorted(load(h + '/.claude/plugins/installed_plugins.json').get('plugins', {})):
    print('claude-plugin:' + k)
def walk(path, o):
    if isinstance(o, dict) and o:
        for k, v in o.items(): yield from walk(path + [k], v)
    else:
        yield '.'.join(path)
for p in sorted(set(walk([], load(h + '/.claude/settings.json')))):
    print('claude-setting:' + p)
for k in sorted(load(h + '/.claude.json').get('mcpServers', {})):
    print('claude-mcp:' + k)
for k in sorted(load('$W/.mcp.json').get('mcpServers', {})):
    print('workspace-mcp:' + k)"

systemctl --user list-unit-files --state=enabled --no-legend 2>/dev/null | awk '{print $1}' | sort -u | emit user-unit
ls /etc/systemd/system/*.service /etc/systemd/system/*.timer 2>/dev/null | xargs -rn1 basename | sort -u | emit system-unit
crontab -l 2>/dev/null | grep -vE '^\s*(#|$)' | sed -E 's/[A-Za-z0-9_-]{24,}/<masked>/g' | sort -u | emit cron

names "$HOME/lib/miniforge3/envs" | emit mamba-env
names "$HOME/bin" "$HOME/.local/bin" | grep -v ':$' | emit bin
names "$AWM_BIN/../lib/node_modules" | emit npm
for t in claude opencode codex gh jq tmux docker dvc git-annex rclone globus node bwrap zerotier-cli tailscale ollama logrotate; do
  command -v "$t" >/dev/null && echo "$t"
done | emit cli
names "$W/projects" | emit project
names "$HOME/.config" | emit config-dir
names "$HOME/.config/opencode" | emit opencode
names "$HOME/.awm-secrets" | emit secret-file

grep -iE '^\s*Host\s' "$HOME/.ssh/config" 2>/dev/null | sed -E 's/^\s*Host\s+//I' | tr ' ' '\n' | grep -v '^$' | sort -u | emit ssh-host
names "$HOME/.ssh" | grep -vE '^(config|known_hosts.*|live_connections|authorized_keys)$' | emit ssh-file
git config --global --list --name-only 2>/dev/null | sort -u | emit gitconfig

for f in .bashrc .profile .bash_profile .tmux.conf .codex/config.toml .config/awm/logrotate.conf; do
  md5 "$f" "$HOME/$f"
done | emit dotfile
