"""awm.virtmic — the virtual microphone's PulseAudio plumbing.

Owns the PulseAudio daemon and the ``virtmic`` ``module-null-sink`` whose
``.monitor`` is WSL's default capture source. Every recorder on the host —
``arecord``, ``/voice``, the awm ``stt`` stack, and the ``mic`` service's phone-
browser bridge — reads that monitor, so the sink going missing makes all of
them record silence without erroring.

The sink used to be created once by the ``mic`` service at startup and never
re-checked, which meant every PulseAudio idle-exit destroyed it permanently.
This service replaces that with three independent durability layers — a
``daemon.conf`` that stops the daemon idling out, a ``default.pa`` that
re-declares the sink on every daemon start, and a reconcile loop that catches
whatever those two miss — under the gateway's normal supervision.

Unlike ``mic`` or ``fileviewer`` there is no listener and no transport here:
the gateway registration buys supervision plus a status surface, and the real
work happens in the background loop (``hub_adapter``) driving ``audio``.
"""
