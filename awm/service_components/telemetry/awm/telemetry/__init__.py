"""awm.telemetry — a reusable idempotent event-sync substrate.

Multiple services need to publish an ordered event stream a third-party client
can sync — by direct poll OR live stream — with stable ids + ordering so the
two paths dedupe. This is that substrate, factored out of the proven agents
transcript pattern (durable store + in-proc bus + backfill-then-live session).

Server side:
    from awm.telemetry import Telemetry
    tel = Telemetry("orchestrator")
    tel.emit(stream, kind, body, meta)          # append + live fan-out
    tel.read_after(stream, after_seq)           # poll sync
    session_handlers={"telemetry": tel.session_handler}   # live WS

The client half is ``@awm/telemetry`` (ui_components/telemetry): the same
``(seq, id)`` cursor, mirrored.
"""

from awm.telemetry.bus import TelemetryBus
from awm.telemetry.service import Telemetry
from awm.telemetry.store import TelemetryStore

__all__ = ["Telemetry", "TelemetryStore", "TelemetryBus"]
