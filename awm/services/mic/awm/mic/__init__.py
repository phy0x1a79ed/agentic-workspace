"""awm.mic — the remote microphone service.

Gives a machine with no capture device a microphone fed from a phone's browser:
the page at ``/ui/mic`` captures the phone's mic and streams s16le PCM over a
direct hub session to ``bridge``, which pipes it into the ``virtmic`` null-sink
whose ``.monitor`` is the default capture source. ``hub_adapter`` registers the
service and exposes a ``status`` / ``ensure_sink`` surface alongside the stream.

The secure context ``getUserMedia`` requires comes from ``httpsfront``, the one
off-host listener in awm; mic itself binds nothing and mints nothing.
"""
