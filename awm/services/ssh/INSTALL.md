# SSH Service

Manages headless ControlMaster SSH connections to managed hosts (sockeye,
fir, chamois, micb0). Orchestrates VPN and 2FA burst automatically before
connecting, so the caller gets a seamless single-verb API.

## API

| Verb | Args | Description |
|------|------|-------------|
| `connect` | `host` (string, required) | Open ControlMaster connection to host; blocks until socket is live |
| `disconnect` | `host` (string, required) | Close ControlMaster connection to host |
| `status` | none | List all managed hosts with connection state |

## Managed hosts

| Host | VPN required | 2FA device | SSH user |
|------|-------------|------------|----------|
| sockeye, sockeye1-3 | ubc | cwl | txyliu |
| fir | — | alliance | phyberos |
| chamois | ubc | cwl | tliu |
| micb0 | ubc | cwl | tliu |

## Dependencies

- `awm-config`, `awm-persistence`, `awm-gatewayclient` (shared components)
- System `ssh` (from PATH)
- `~/.ssh/awm-duo-askpass` (SSH_ASKPASS helper for Duo auto-approval)
- The `vpn` and `2fa` services must be running on the gateway

## ControlMaster behaviour

The SSH config at `~/.ssh/config` must have:

```
host sockeye* fir chamois shamwow
    ControlMaster auto
    ControlPath ~/.ssh/live_connections/%h_%p_%r
    ServerAliveInterval 60s
```

The service opens connections with `ssh -f -N -M <host>`, which creates a
ControlMaster socket at the configured path. Subsequent `ssh`/`scp`/`rsync`
calls to the same host reuse this socket with no re-authentication.
