#!/usr/bin/env bash
# Install the parallel-work skill and its pointer in the global CLAUDE.md (parallel-work.md R59).
# Runs only once `reflection wait` serves on this node, because the skill's wave step calls it.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
claude_home="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
skill_dir="$claude_home/skills/parallel-work"
global_md="$claude_home/CLAUDE.md"
anchor='**Load the `write-docs` skill before writing the plan doc.**'
pointer="$(cat "$here/pointer.md")"
awm="$(command -v awm || echo "$HOME/miniforge3/envs/awm/bin/awm")"

if ! "$awm" reflection wait --help >/dev/null 2>&1; then
  echo "reflection wait is not served here yet; install nothing" >&2
  exit 1
fi

mkdir -p "$skill_dir"
install -m 0644 "$here/SKILL.md" "$skill_dir/SKILL.md"

if grep -qF "$pointer" "$global_md"; then
  echo "pointer already in $global_md"
  exit 0
fi
grep -qF "$anchor" "$global_md" || { echo "no planning-style anchor in $global_md" >&2; exit 1; }

tmp="$(mktemp "$global_md.XXXX")"
awk -v anchor="$anchor" -v pointer="$pointer" '
  index($0, anchor) == 1 && !done { print; getline; print; print pointer; print ""; done = 1; next }
  { print }
' "$global_md" > "$tmp"
mv "$tmp" "$global_md"
echo "installed $skill_dir/SKILL.md and the pointer in $global_md"
