# Port plan: mic

mic feeds a phone's browser audio into the PulseAudio null sink `virtmic` and keeps that sink alive (R134 folded virtmic in). It keeps shelling out to `pactl` and `pacat` and links no audio library. The Svelte page moves unchanged to `core/plugins/mic/pages/mic/`.

Keep exactly:
- the `pacat` argv
- the `stream` session frames and error texts
- the order: bridge open before sink repair, because of the gateway's 5 s attach limit
- **CAUTION** the three marker-fenced stanzas mic appends to `~/.config/pulse/daemon.conf`, `default.pa` and `~/.asoundrc`, byte for byte, or every host gets a duplicate. mic only appends, as today.

Changes: the health loop logs one line per state change, not a traceback every 20 s. mic stays loaded on hosts with no sound server. No needs. At its land, rotate mizar's 17 MB `mic.log` aside, keeping it.

Tests: port the 29 tests with a `Runner` trait in place of `_run`. Add session tests with a fake `pacat` covering supersede, ping, the 30 s deadline and exit. The dual-run diffs the three written files from temp homes. Live check on capella from a phone.
