# Installing the `mic` plugin (remote microphone)

## Purpose & Contents

This file covers what a node needs before mic runs, what mic writes outside its folder, and how to check it. The code and its tests say how mic works.

mic gives a host with no capture device a microphone fed from a phone. The page at `/p/mic/mic/` captures the phone's microphone and streams s16le PCM over a `stream` session (`/hub/session/mic/stream`). mic plays it into the PulseAudio null-sink `virtmic`, whose `.monitor` is the default capture source, so `/voice` and `arecord` record the phone. `getUserMedia` needs a secure context, which [`httpsfront`](../../../awm/services/httpsfront/INSTALL.md) supplies for every awm page.

mic also keeps PulseAudio resident and the sink loaded, through config files, a pass at start, a health pass every `MIC_HEALTH_INTERVAL_S` seconds and a pass before each stream. `mic_ensure_sink` runs one pass now.

## Install

mic is a core plugin: the deploy places its static binary at `<AWM_DIR>/bin/mic`, and `run.sh` runs it. It has no install step.

Install the system packages:

    sudo apt-get install -y pulseaudio pulseaudio-utils

| Tool | Why |
|---|---|
| `pulseaudio`, `pactl`, `pacat` | keep the `virtmic` sink alive, and pipe the phone's PCM into it |
| `systemctl --user` | restart a socket-activated PulseAudio after a config write |

A host with no sound server keeps mic loaded. `mic_status` reports why the audio path is down, and the log says it once per change.

## It writes outside its own directory

mic appends one stanza to each of three files in `$HOME`, because PulseAudio and ALSA read their configuration only from there.

| File | Why |
|---|---|
| `~/.config/pulse/daemon.conf` | `exit-idle-time = -1` stops the daemon exiting about 20 s after the last client leaves |
| `~/.config/pulse/default.pa` | declares the `virtmic` null-sink, so a daemon restart rebuilds it unaided |
| `~/.asoundrc` | routes ALSA's default device through PulseAudio, so `arecord` sees the mic |

A marker fences each stanza, so a rerun never duplicates it. **CAUTION** The markers keep the text of the Python mic and the virtmic service byte for byte. A changed marker appends a second stanza on every host. A file that does not exist yet starts with an `.include` of the system copy, because a user-level PulseAudio file replaces the system one.

**CAUTION** Install PulseAudio before mic first runs. mic writes the `.include` only when the system file exists, and the marker then stops any rewrite. A run on a node without `pulseaudio` leaves a `default.pa` that loads no modules, so `pactl` hangs. To repair it, prepend the two `.include` lines and restart `pulseaudio.service`.

To remove the durability layers, delete the marker-fenced stanzas by hand.

## Exposure

The page sits behind httpsfront's password like every other awm page. The audio session is reachable by anything that reaches the loopback gateway, that is, by anything already running as this user. A device that does not yet trust the node's CA installs it from the unauthenticated `/ca.crt` on the edge, which the login page and the mic page's footer link.

## Always-on gateway

A host where the mic is permanent infrastructure must not let the gateway idle out. Set `AWM_IDLE_SHUTDOWN=0` in the gateway's `config.toml` `[env]`, then restart `awm.service`.

## Env overrides

| Var | Default | Effect |
|---|---|---|
| `MIC_SINK` | `virtmic` | PulseAudio null-sink name |
| `MIC_HEALTH_INTERVAL_S` | `20` | seconds between health passes |

## Verify

    awm mic status                          # sink, active_streams, audio_path_ok
    curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:7819/p/mic/mic/
    pactl get-default-source                # virtmic.monitor

Open `/p/mic/mic/` on the phone through `https://<host>:$AWM_HTTPS_PORT`, tap **START**, and confirm the badge turns purple. Purple means mic sent `ready`, so `pacat` runs. Then `pactl list sink-inputs | grep -A6 awm-mic` shows the live stream, and `parec -d virtmic.monitor` or Claude Code `/voice` hears the phone.
