"""Convo subsystem — the in-tree inner loop that turns raw silence-cut STT into
a faithfully cleaned, auto-submittable composer message via the LLM backend.

Well-separated from the STT registry (it only receives finalized utterances and
returns a :class:`ConvoResult`), but not liftable: it is coupled to the ptt
service's silence-cut by design. The generic LLM call lives in the sibling
``agent`` package; this package owns the voice-cleanup domain logic.
"""

from .manager import ConvoManager, get_convo_manager
from .session import ConvoResult, ConvoSession

__all__ = [
    "ConvoManager",
    "get_convo_manager",
    "ConvoResult",
    "ConvoSession",
]
