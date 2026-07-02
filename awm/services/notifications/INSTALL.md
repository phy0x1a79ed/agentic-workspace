# awm-notifications — install notes

The agent-attention notifier: harness-level producers report agent lifecycle
events; this service classifies them into attention items (question / idle /
error) and streams deltas to the board page (`/ui/notifications`) over the
`feed` emitter.

## Install

```bash
bash install.sh              # editable install into the `awm` env
AWM_ENV=other bash install.sh
```

Installs `config` + `persistence` + `gatewayclient` (no-deps) then this
service, and writes the `.runtime-env` sidecar (`AWM_PYTHON`, `AWM_ENV_BIN`)
so the hub supervisor can respawn `run.sh` under systemd's minimal PATH.

You never run the service by hand — the gateway discovers `run.sh`, spawns it,
and injects `AWM_HUB_URL` / `AWM_SERVICE_NAME` / `AWM_SERVICE_ID`. The service
owns its DB at `AWM_DIR/services/notifications/notifications.db`.

## Producers (one-time, per machine)

The service only *receives* — detection lives in the harnesses:

- **Claude Code**: merge the hook entries into the `hooks` block of
  `~/.claude/settings.json`, mapping `Stop`, `Notification`,
  `UserPromptSubmit`, `SessionStart`, `SessionEnd` to
  `hooks/hook.py` (see `hooks/claude-settings-fragment.json` for the exact
  block). The hook is pure stdlib, applies a cwd walk-up `.mcp.json` scope
  filter, and POSTs `report` fire-and-forget (`$AWM_HUB_URL` else
  `127.0.0.1:7819`).
- **OpenCode**: symlink/copy `hooks/awm-notify.js` into
  `~/.config/opencode/plugin/`.

## Tuning

| Env (service) | Default | Meaning |
|---|---|---|
| `AWM_NOTIFY_IDLE_GRACE_S` | `45` | Delay before an `idle` item desktop-pushes (question/error push immediately). |
| `AWM_NOTIFY_STALE_TTL_S` | `43200` | Open items whose session went silent this long are auto-expired on read. |

## Iterating

```bash
awm dev shadow --port 7821 awm/services/notifications pages/notifications
```
