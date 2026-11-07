# rlm-browser

## Purpose & Contents

This file holds what the code does not show about the rlm-browser realm: its runtime needs, its environment, and why an attached Chrome confines a guest.

## Runtime

- The host backend launches the Chrome that `awm_cdp::find_chrome` finds: `RLM_CHROME_BIN`, else `google-chrome`, `google-chrome-stable`, `chromium` or `chromium-browser` on `PATH`.
- The docker backend runs the image that `docker/` builds.
- Keep the profiles at `services/rlm-browser/profiles/<game>/` on a Linux filesystem, never under `/mnt/c`. A Windows mount corrupts Chrome's `SingletonLock`.

| Env var | Default | Meaning |
|---|---|---|
| `RLM_BROWSER_BACKEND` | `host` | The launch backend for `headless` and `rendered`: `host` or `docker`. |
| `RLM_CHROME_BIN` | autodetect | The host backend's Chrome binary. |
| `RLM_ATTACH_CDP_URL` | `http://172.25.176.1:19222` | The existing Chrome that `attached` drives. `opts.attach_url` overrides it per acquire. |
| `RLM_BROWSER_IMAGE` | `rlm-browser:latest` | The docker backend's image. |
| `RLM_BROWSER_SHM` | `2g` | The docker backend's `--shm-size`. The 64 MB default crashes tabs. |
| `AWM_RLM_BROWSER_ATTACH_PEER` | unset | On a node that borrows the attached Chrome, the peer that owns it. |

**CAUTION** `RLM_ATTACH_CDP_URL` names the same Chrome the `chrome-devtools` MCP server targets (`<workspace>/.mcp.json` `--browserUrl`). Update both when that port moves.

## The guest window

An attached Chrome is Tony's, logged in. A session below trusted gets a fresh browser context in it (fleet.md R102 item 6): its own tabs only, page-level CDP methods only, and no `__browser__` target. The context id is stored with the session row, so the confinement survives a restart. A forwarding node passes its caller's level as `opts.level`, which lowers the level the owning peer applies and never raises it.
