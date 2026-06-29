"""awm.mic — the remote microphone bridge service.

Gives WSL a microphone fed from a phone's browser over ZeroTier: the browser
captures its mic, streams s16le PCM over a WebSocket to an off-host HTTPS
listener (``bridge``), which pipes it into a PulseAudio null-sink (``audio``)
whose ``.monitor`` is WSL's default capture source. The awm gateway supervises
the process and exposes a thin ``status`` / ``ensure_sink`` surface
(``hub_adapter``); the audio path itself is the self-contained listener, since
the loopback-only hub can't serve a remote phone over HTTPS.
"""
