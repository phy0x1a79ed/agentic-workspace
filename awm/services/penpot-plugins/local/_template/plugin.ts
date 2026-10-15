// plugin.ts — copy this whole local/_template/ directory, rename it, then
// fill in the blanks below. This file is SOURCE, not served: build it to
// plugin.js before it lands in your new plugin's directory (see the "Build
// step" comment at the bottom). Keep manifest.json (already renamed for you)
// beside it.
//
// Before writing a single real API call, read these two files in your own
// checkout of the Penpot fork (projects/penpot/<your-scope>/ in this
// workspace — pick whichever scope you're actually in):
//
//   docs/plugins/create-a-plugin.md   — the walkthrough this file summarizes
//   plugins/libs/plugin-types/index.d.ts  — the actual API surface, with a
//                                            worked example on every method
//
// Both are also the npm package @penpot/plugin-types if you're outside the
// fork; the public rendered docs are at https://help.penpot.app/plugins/ and
// https://doc.plugins.penpot.app/. Every method below is one you should look
// up there before shipping — this skeleton shows the *shape*, not the full
// contract (required permissions, exact return types, edge cases).
//
// A second, real, working plugin sits right next to this template at
// ../penpot-view-refresh/ (plugin.js + index.html) — copy patterns from it
// too, especially its manual-action / UI-message-loop shape.

// Uncomment when you actually need a named type for an annotation (the
// `penpot` global itself needs no import — it's ambient, declared by
// @penpot/plugin-types as `declare global { const penpot: Penpot }`).
// import type { Shape, Fill, ImageData } from "@penpot/plugin-types";

// ---------------------------------------------------------------------------
// Manifest reminders (see manifest.json beside this file):
//   - "version": 2 is required for "code"/"icon" to resolve as paths relative
//     to wherever the manifest itself is served from (our /penpot-plugins
//     mount). Omit it and Penpot treats the manifest as the older v1 shape.
//   - "permissions" is an allow-list, checked at install AND at each call —
//     e.g. penpot.uploadMediaUrl needs content:write; penpot.on(...) needs
//     content:read. Request only what you use: write implies its own read,
//     so you rarely need to list both.
//   - icon.png: any image format works; Penpot recommends 56x56px.
// ---------------------------------------------------------------------------

// -- Option A: no UI at all --------------------------------------------------
// A plugin can do its whole job the instant it's clicked in the toolbar, with
// no penpot.ui.open() call — see the Palette-color example in
// create-a-plugin.md. Good for a single, no-parameters action on the current
// selection:
//
// const selection = penpot.selection;
// for (const shape of selection) {
//   // ... do something to `shape`, e.g. shape.setPluginData(key, value)
// }

// -- Option B: a UI panel, two-way message loop ------------------------------
// Needed the moment you need user input (a URL, a name, a choice) rather than
// acting on the current selection alone. This is what penpot-view-refresh
// does — copy its plugin.js + index.html wholesale if your plugin's shape is
// "mark something, then trigger an action on it."
//
// penpot.ui.open("Plugin name", "index.html", { width: 320, height: 240 });
//
// // Plugin (this file) → UI iframe:
// penpot.ui.sendMessage({ type: "example", content: "hello" });
//
// // UI iframe → plugin (this file). The UI page's own script receives the
// // message above via `window.addEventListener("message", ...)` and answers
// // with `parent.postMessage(data, "*")` — see create-a-plugin.md § 2.4 for
// // the full two-way contract.
// penpot.ui.onMessage<{ type: string; [key: string]: unknown }>((message) => {
//   if (message.type === "example-action") {
//     // ... handle it
//   }
// });

// -- Reacting to what's happening in the file --------------------------------
// penpot.on(type, callback, props?) — requires content:read. Event types:
// pagechange, shapechange (needs { shapeId } in props), selectionchange,
// themechange, filechange, contentsave, finish. Full semantics of each are in
// index.d.ts's doc comment on Penpot.on — read it before picking one; e.g.
// there is no ambient "window focus" event here (the plugin script itself has
// no DOM) — that would have to be wired from the UI iframe's own `window`,
// with a message back to this file, if a future version of your plugin needs
// it.
//
// penpot.on("selectionchange", () => {
//   const shape = penpot.selection[0];
//   // ...
// });

// -- Reading/writing plugin data ---------------------------------------------
// Per-shape, string-only key/value storage that travels with the file.
// Requires content:read to get, content:write to set.
//
// shape.setPluginData("my-plugin:some-key", "some-value");
// const value = shape.getPluginData("my-plugin:some-key");

// ---------------------------------------------------------------------------
// Build step: this file must become plain JS before it's servable. Penpot's
// own docs (create-a-plugin.md § 2.5, deployment.md § 3.1) show esbuild and
// Vite examples; the simplest is:
//
//   npx esbuild plugin.ts --bundle --minify --outfile=plugin.js
//
// Drop the built plugin.js beside manifest.json here, remove this .ts file's
// directory-of-origin's name from _template (i.e. don't rename _template
// itself — you already copied it to a new directory), and your plugin's
// install URL is /penpot-plugins/<your-plugin-name>/manifest.json once the
// penpot-plugins service is running. See ../../INSTALL.md.
// ---------------------------------------------------------------------------
