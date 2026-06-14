"""PTT (STT-only) feature service — package ``awm.stt``.

Discovered by the gateway's filesystem scan of ``awm/services/*`` for
``run.sh`` and started (and respawned) via ``bash run.sh``, which execs the
control-WS adapter (``python -m awm.stt.hub_adapter`` on
:class:`awm.gatewayclient.ServiceAdapter`). The gateway injects exactly three
env vars — ``AWM_HUB_URL`` / ``AWM_SERVICE_NAME`` / ``AWM_SERVICE_ID`` — and no
auth, ever. Browser-side, calls go to ``/svc/stt/{fn,session}/*`` and the hub
relays them across the control + bridge WSes.

Surface: ``transcribe`` (HTTP-fallback STT) + the direct ``stream`` session
(faster-whisper PTT/continuous dictation, with the convo cleanup loop). The
convo cleanup runs through ``awm.agentcore.run_once`` (opencode one-shot).
"""
