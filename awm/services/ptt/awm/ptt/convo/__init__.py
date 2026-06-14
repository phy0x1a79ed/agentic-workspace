"""Convo subsystem — the in-tree inner loop that turns raw silence-cut STT into
a faithfully cleaned, auto-submittable composer message via an LLM.

Well-separated from the STT registry (it only receives finalized utterances and
returns a :class:`ConvoResult`), but not liftable: it is coupled to the ptt
service's silence-cut by design. The LLM call runs through the shared
``awm.agentcore`` harness layer (opencode one-shot per cut) behind the
:class:`CleanupAgent` seam (``cleanup.py``); this package owns the
voice-cleanup domain logic.
"""

from .cleanup import CleanupAgent, CleanupError
from .manager import ConvoManager, get_convo_manager
from .session import ConvoResult, ConvoSession

__all__ = [
    "CleanupAgent",
    "CleanupError",
    "ConvoManager",
    "get_convo_manager",
    "ConvoResult",
    "ConvoSession",
]
