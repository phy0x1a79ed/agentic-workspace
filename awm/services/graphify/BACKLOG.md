# graphify backlog — what to build, in order

Companion to `LOOP.md`. Tiers are priority order. Check items off as they ship
(code + green unit tests + live :7871 dogfood + commit). Add discovered work
under the right tier rather than going off-script.

Consumer lens: every item is judged by "does this make graphify more
trustworthy or more useful to an agent navigating awm during development?"

---

## Tier 0 — prerequisites (must land before the rest is dogfoodable)

- [x] **#2 dev-shadow GRAPHIFY_BIN resolution.** run.sh dev branch runs under
  `mamba run -n awm` without GRAPHIFY_BIN → shadow builds fail. Fix: source
  `.runtime-env` if present, else resolve the binary from `${GRAPHIFY_ENV:-graphify}`.
  This unblocks the whole :7871 dogfood workflow. _(9b1f570; shell resolution
  verified against graphify-spike. Live :7871 dogfood pending Task #3.)_

## Tier 1 — make the existing graph trustworthy + usable

- [x] **#1 Pin the CLI.** install.sh `pip install graphifyy` → `graphifyy==0.9.1`
  (the runner contract was reverse-engineered against 0.9.1; an unpinned bump
  could silently break argv/output). _(ca300a8; confirmed spike env runs 0.9.1.)_
- [ ] **Auto-rebuild-if-stale (trust).** query/path/status should know when the
  graph is older than the source tree. Add a `stale` signal first (see #4),
  then let query/path opt to auto-rebuild (or loudly warn) when stale, so a
  consumer never silently reasons over a graph that predates their edits.
- [ ] **#4 Staleness signal.** `status()` returns `stale: bool` = (max source
  mtime under target, excluding ignored paths) > built_at. Cheap, unlocks the
  above.
- [ ] **#3 Read-during-rebuild safety.** query/path don't take `_BUILD_LOCK`
  while build rewrites graph.json (torn-read race). Fix: read under a shared
  lock, or build to a temp dir + atomic swap.
- [ ] **Agent-shaped output.** query/path return raw CLI text. Give the consumer
  structured JSON: parsed nodes/edges, `file:line` refs that are clickable,
  deduped + ranked, with edge labels. Plain text is hard to act on
  programmatically.

## Tier 2 — the questions an agent actually asks

- [ ] **`refs` / `neighbors <symbol>`.** Directional callers / callees /
  importers of a symbol. Highest-value, most common navigation need.
- [ ] **`affected <symbol>`.** Transitive impact of changing a symbol (who
  breaks). Highest ceiling — the question agents most want answered before a
  refactor.
- [ ] **`find <label>`.** Resolve/disambiguate a fuzzy name to concrete nodes
  before pathing (path needs exact labels today).

## Tier 3 — semantic wiring (AST-only blind spots)

- [ ] **Dynamic-wiring edges.** AST misses awm's manifest→MCP/RPC/topic-string
  wiring (the hardest "what connects X to Y" questions). Lever: a Phase-2
  `--backend claude` pass over AGENTS.md / manifests — but that breaks the
  no-key invariant, so it must be **opt-in + clearly separated** from the
  default AST build. Likely a human-gated decision; flag rather than assume.

## Cross-cutting constraint

- **MCP verb budget.** Adding `refs`/`affected`/`find` as 3 new top-level verbs
  worsens the 71-tool surface (memory: todo_reduce_awm_mcp_verb_count). Prefer
  folding new reads into ONE op-discriminated verb (e.g. `graphify_query` with a
  `mode`) OR make them deferred (ToolSearch-loaded). Decide per-item; default to
  not growing the always-on surface.

---

## Definition of happy (stop condition)

The loop is "happy" — stop and hand back for human-gated promotion — when ALL:

1. **Tier 0 + Tier 1 fully shipped**: dev-shadow works, CLI pinned, and a
   consumer can trust the graph (knows when it's stale / it self-refreshes) and
   act on structured output.
2. **At least one Tier-2 navigation verb** (`refs`/`neighbors` preferred) ships
   and is dogfooded live — the tool answers a real "who calls X" question end to
   end on the awm tree.
3. **Every increment** is: unit-test green (`run-tests.sh graphify`), exercised
   live on :7871 at least once, committed to `feat/svc-graphify`.
4. **No regressions**: full graphify unit suite green; AST-only/no-key invariant
   intact; worktree stays clean (no in-tree graphify-out).
5. **Honest writeup**: this backlog reflects final state; remaining Tier-2/3
   items are clearly marked as deferred-for-human with rationale.

Tier 3 is explicitly **out of scope for "happy"** (it likely needs a human call
on the key invariant) — reaching it means flag-and-stop, not build.

---

## Iteration log

(append one dated line per shipped increment)

- 2026-06-28 — Loop scaffolding authored (LOOP.md + BACKLOG.md). Baseline:
  service committed at b88695e, 14/14 unit tests green, real-binary +
  feat-dag :7861 e2e verified pre-loop. Starting Tier 0 #2.
- 2026-06-28 — Tier 0 #2 shipped (9b1f570): dev-shadow GRAPHIFY_BIN resolution.
  + LOOP.md: delegate heavy work to sonnet subagents to preserve orchestrator
  context. Next: bring up :7871 + live dogfood, then Tier 1.
- 2026-06-28 — Tier 1 #1 shipped (ca300a8): pin graphifyy==0.9.1. Live :7871
  dogfood of #2 dispatched to a sonnet subagent (validates GRAPHIFY_BIN resolves
  live from graphify-spike). Next Tier 1: staleness signal (#4) → auto-rebuild →
  read-lock (#3) → agent-shaped output, shaped by dogfood findings.
