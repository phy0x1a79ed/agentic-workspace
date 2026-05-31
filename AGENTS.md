# AWM Scope Agent

You are working in a scope worktree of the `awm` project. This file is the tracked, branch-shared orientation for scope agents. The per-scope override layer lives in `.awm/context.md` (gitignored).

For the full architectural reference, see `WORKSPACE.md` — especially § "Component Dev Architecture" and § "Scope Naming Convention".

## Web-UI Dev

The frontend has two complementary seams that contain UI complexity and turn composition bugs into autonomous test failures. **Do this work in a worktree that has `feat/infra-dev-components` merged in** (every `comp-*` and `infra-typed-seams` branch carries the doc but not the runtime — see § "Where to run" below).

### Per-component dev surface (`infra-dev-components`)

Each component owns a sibling `<Name>.fixtures.ts` file declaring variants. No central registry — Vite globs them at build time.

```ts
// frontend/src/lib/components/StatusTag.fixtures.ts
import type { ComponentProps } from 'svelte';
import Component from './StatusTag.svelte';

const fixtures: Record<string, ComponentProps<typeof Component>> = {
  active: { status: 'active' },
  failed: { status: 'failed' },
};
export { Component as component };
export default fixtures;
```

Dev surface routes:

- `/dev/components` — auto-generated index of every `*.fixtures.ts` under `src/lib/components/`.
- `/dev/components/[slug]?v=<variant>` — single-component view with variant switcher.
- Root `+layout.svelte` skips app chrome and the backend bootstrap on `/dev/*`, so dev pages never call `/voice`, `/rooms`, `/peers`, or `/vagrant`.

`npm run test` runs `vitest` + `jsdom`. A single generic runner (`src/lib/dev/fixtures.test.ts`) globs the same fixture set and mounts every variant — **crash-on-mount bugs surface in CI without anyone opening a browser**. Adding a fixture requires zero changes to the runner.

### Bind-prop wrapper pattern

For `$bindable` props whose bug lives at the parent's bind direction, the fixture points at a thin wrapper Svelte file that wires the bind from local state. For `AgentList`, the parent itself is the wrapper, so no extra file is needed — the failing variants in `AgentList.fixtures.ts` reproduce the composition-seam crash autonomously.

### Typed seam (`infra-typed-seams`)

`npm run gen-types` spawns a one-shot Python process in the `awm` mamba env that imports `awm.exposed:app` and calls `app.openapi()` directly. No live uvicorn required, no auth wall. Output goes to `frontend/src/lib/api/generated.ts` (committed). Spawn cwd + `sys.path` are pinned to the worktree root so `import awm` resolves to the worktree's source, not the editable install.

Hand-written interfaces in `client.ts` get progressively replaced by re-exports from `generated.ts`. The first proof-of-seam is `VagrantSessionResponse`. The migration is intentionally narrow — types that match 1:1 swap immediately; types that diverge in shape stay hand-written until the backend tightens its `response_model` declarations.

Engine `CONFIG_SCHEMA` JSON Schemas escape this pipeline (FastAPI types their envelope as `dict[str, Any]` → `unknown`). Fixtures for engine forms hand-shape JSON Schema blobs.

## Workflow

`node`/`npm` live in the `awm` mamba env, not on the default PATH. Prefix as shown:

```bash
cd frontend
PATH=/home/tony/lib/miniforge3/envs/awm/bin:$PATH npm install

# Visual: see fixtures in the browser
PATH=/home/tony/lib/miniforge3/envs/awm/bin:$PATH npm run dev   # http://localhost:12103/ui/dev

# Autonomous: fail CI on crash-on-mount bugs
PATH=/home/tony/lib/miniforge3/envs/awm/bin:$PATH npm run test

# Regenerate types after Pydantic model changes
PATH=/home/tony/lib/miniforge3/envs/awm/bin:$PATH npm run gen-types
PATH=/home/tony/lib/miniforge3/envs/awm/bin:$PATH npm run check
```

### Where to run

- **`feat/infra-dev-components`** has the dev routes, vitest config, and the generic runner. Anything you add a fixture for shows up here.
- **`feat/infra-typed-seams`** has the `gen-types` script and `generated.ts`.
- **`feat/comp-*`** branches carry only the fixture file for their component. To verify a `comp-*` fixture, either merge `feat/infra-dev-components` into the comp branch (and `feat/infra-typed-seams` if the component needs generated types), or use the `verify/integration` branch that octopus-merges all five.
