# Web UI (SvelteKit)

Operator console SPA. Serves at `https://<host>/ui/` via uvicorn — the
production build is emitted to `../awm/static/` by `adapter-static`.

## Stack

- **SvelteKit 2** + **Svelte 5** (runes: `$state`, `$effect`, `$derived`,
  `$props`, `$bindable`)
- `@sveltejs/adapter-static` with `fallback: 'index.html'`
- **Tailwind CSS v4** via `@tailwindcss/postcss`
- **bits-ui** for headless primitives
- **TypeScript** strict
- `marked` + `dompurify` for transcript markdown (when needed)

Runes in non-component files require the `.svelte.ts` filename suffix
(see `src/lib/api/ws.svelte.ts`, `src/lib/state/*.svelte.ts`). A plain
`.ts` file using `$state` will throw at runtime.

## Layout

```
src/
├── app.html       # HTML shell + DM Sans/Mono preconnects + theme-color
├── app.css        # design tokens (--bg, --atomizer, --recording, ...) + global styles
├── app.d.ts
├── lib/
│   ├── api/
│   │   ├── client.ts          # fetch wrapper + typed endpoints (peer, rooms, etc.)
│   │   ├── config.ts          # wsUrl() helper, feature flags
│   │   └── ws.svelte.ts       # RoomWs class — connect/reconnect with backoff
│   ├── components/
│   │   ├── Header.svelte           # 44px top bar
│   │   ├── BottomNav.svelte         # mobile only (≤1024px)
│   │   ├── Sheet.svelte             # left/right slide-in overlay
│   │   ├── UnifiedSidebar.svelte    # /focus rooms rail
│   │   ├── DetailsPanel.svelte      # /focus right rail (agents + recipients + voice)
│   │   ├── Transcript.svelte
│   │   ├── Composer.svelte
│   │   ├── PttButton.svelte         # visual + spacebar binding only (voice STT deferred)
│   │   ├── RecipientChips.svelte
│   │   ├── AgentList.svelte
│   │   ├── RoomCard.svelte          # mobile rooms list item
│   │   ├── StatusTag.svelte         # uppercase mono pill with status colour
│   │   └── WsDot.svelte             # pulsing connection indicator
│   ├── state/
│   │   ├── ui.svelte.ts             # sheet open-state, leader badge, ws kind
│   │   └── recipients.svelte.ts     # per-room recipient selection (localStorage)
│   ├── theme/
│   │   └── colors.ts                # statusColor() → CSS variable
│   ├── utils/
│   │   └── cn.ts                    # clsx + tailwind-merge
│   └── voice/                       # (reserved for STT port)
└── routes/
    ├── +layout.svelte               # shell: Header + slot + BottomNav
    ├── +layout.ts                   # ssr: false, prerender: false
    ├── +page.svelte                 # → /focus redirect
    ├── status/+page.svelte
    ├── focus/[[room]]/+page.svelte  # optional param — both /focus and /focus/<id>
    ├── rooms/+page.svelte
    └── room/[id]/+page.svelte       # → /focus/<id> redirect (legacy bookmark)

static/
├── favicon.svg
└── mic-worklet.js                   # MUST be unbundled (AudioWorklet.addModule URL)
```

## Design flavour

Adopted from `phy0x1a79ed/spark/lib/roma-ui`. Operator-terminal aesthetic:

- Dark only. `--bg #0e0e10`, `--surface #17171a`, `--border #2a2a30`
- `--atomizer #3b82f6` — single primary accent ("active" everywhere)
- `--recording #a855f7` — PTT live / streaming purple
- Status palette: `--ok #10b981`, `--warn #eab308`, `--danger #ef4444`
- Fonts: **DM Sans** body 13px (mobile 14px), **DM Mono** for labels /
  tabs / tags / numeric data
- Micro-labels: uppercase, 9–10px, 1–2px letter-spacing
- Thin 1px borders, no rounded panels (3–4px radius on inputs/badges only)
- Use `color-mix(in oklab, var(--X) 10%, transparent)` for status-tinted
  backgrounds — preserves the colour identity across themes

When adding new colours, add a CSS variable in `app.css` rather than hex
literals in components, and a mapping in `lib/theme/colors.ts` if it's
tied to a status string.

## Mobile breakpoints

- **≤ 720px** — phone. Bottom nav, sheet drawers for rails, 16px inputs
  (iOS zoom guard), 44px min touch targets, "Hold to talk" replaces SPACE
  hint on PTT.
- **721–1024px** — tablet / narrow desktop. Right rail collapses to a
  sheet; rooms rail stays.
- **≥ 1025px** — desktop. Full `.chrome` grid (rooms / chat / details).

The `<Sheet>` component slides in from left or right and is automatically
hidden at ≥ 1025px via media query.

## Dev workflow

```bash
# From ../dev/
./run.sh start                    # uvicorn (the API + WS backend)
./run.sh frontend                 # Vite dev on :12103 with proxy to backend
./run.sh build                    # production build → ../awm/static/
```

Dev mode is HTTP-only (12103), so session cookies (`Secure=True`) won't
flow. For authed feature work, mint a token via `./run.sh login` and visit
the production URL once, OR work against the production build (`build`
then reload).

The Vite proxy at the bottom of `vite.config.ts` forwards `/auth/*`,
`/invoke`, `/peer*`, `/projects`, `/scopes`, `/status`, `/rooms`, `/ws`
to the uvicorn at `127.0.0.1:12101`. Add new top-level backend paths to
the proxy list when porting new endpoints.

## API client conventions

- Bearer is an HttpOnly cookie set by `/auth/bootstrap`. Always send
  `credentials: 'include'`.
- The `X-Awm-As` header carries `user:<name>` identity (read from the
  `awm_as` cookie at boot).
- 401 in **production** redirects to `/ui/login.html`; in **dev**
  (`import.meta.env.DEV`) the 401 propagates so the layout can show an
  error rather than bouncing to a URL that doesn't exist on :12103.
- Backend field names: `Post` uses `author` + `body` + `to_scope` (not
  `from`, `text`, `to`). Same for `postToRoom({ body, to_scope })`.

## Deferred

- Full STT/PTT port — voice-panel.js is a 276-line DOM-driven IIFE that
  needs proper componentization (WS to /voice/ws, mic-worklet pipeline,
  auto-send into Composer state). `mic-worklet.js` already ships in
  `static/` so the port doesn't need to re-add it.
- Slash command picker — the `/` button currently toggles `ui.slashOpen`
  but no picker UI is rendered yet. Wire bits-ui Popover (≥721) / Drawer
  (≤720) and `getSlashCommands(roomId, scope)`.
- Federated peer subscriber lists in DetailsPanel (legacy had Subscribers
  and Federated Peers panels — folded into Agents for now).
