# virtmic — install notes

Owns PulseAudio and the `virtmic` null-sink that is WSL's default capture
source. Discovered and supervised by the gateway like any other service; no
manual registration.

```bash
bash awm/services/virtmic/install.sh      # or the composition root:
bash awm/gateway/install.sh
```

## Dependencies

- `pulseaudio` + `pactl` on `PATH` (Debian/Ubuntu: `pulseaudio-utils`).
- `systemctl --user` when PulseAudio is socket-activated by systemd (the usual
  case on Ubuntu-under-WSL) — used to restart the daemon after a config write.

No DB, no third-party Python deps, no listener, no port.

## It writes outside its own directory — on purpose

Unusual for an awm service, and worth knowing before you run it: `virtmic`
writes three files in `$HOME`, because that is where PulseAudio and ALSA read
their configuration from and there is no other way to make the plumbing
durable.

| File | Why |
|---|---|
| `~/.config/pulse/daemon.conf` | `exit-idle-time = -1` — stops the daemon exiting ~20s after the last client disconnects |
| `~/.config/pulse/default.pa` | declares the `virtmic` null-sink so a daemon restart rebuilds it unaided |
| `~/.asoundrc` | routes ALSA's default device through PulseAudio so `arecord` sees the mic |

Each write is fenced by an idempotence marker, so re-running the service never
duplicates a stanza. **If a file does not already exist, it is created with a
`.include` of the system copy first** (`/etc/pulse/daemon.conf`,
`/etc/pulse/default.pa`) — a user-level PulseAudio config file *replaces* the
system one rather than layering onto it, so creating a bare file with only our
settings would silently drop every module the system config loads.

To uninstall the durability layers, delete the marker-fenced stanzas by hand;
the service does not remove them.

## Verifying

```bash
awm virtmic status                 # or: virtmic_status over MCP
pactl get-default-source           # expect: virtmic.monitor
pactl list short sinks | grep virtmic
```

The adversarial check — stop PulseAudio outright and confirm the sink returns
with no human action inside one health interval (default 20s):

```bash
systemctl --user stop pulseaudio.service pulseaudio.socket
sleep 25
pactl get-default-source           # expect: virtmic.monitor again
```

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `VIRTMIC_SINK` | `virtmic` | null-sink name |
| `VIRTMIC_HEALTH_INTERVAL_S` | `20` | reconcile-loop period |

## Relationship to `mic`

`mic` is a **consumer**: it pipes the phone-browser audio into this sink with
`pacat` and calls `virtmic_ensure` before starting a stream, so the sink is
guaranteed present at the moment audio needs it rather than merely at boot
(the gateway guarantees no start order between services). `mic` no longer runs
`pactl` or `pulseaudio` itself.
