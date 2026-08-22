"""Convo subsystem — the in-tree inner loop that accumulates raw silence-cut STT
into a single auto-submittable composer message.

Well-separated from the STT registry (it only receives finalized utterances and
returns the composed text), but not liftable: it is coupled to the stt service's
silence-cut by design. There is no LLM — the text is the raw whisper transcript,
re-passed accurately on each cut; submit timing is owned by the registry.
"""

from .session import ConvoSession

__all__ = ["ConvoSession"]
