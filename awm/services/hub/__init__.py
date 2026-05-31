"""Service hub: live-registration routing layer above awm.exposed:app.

A registered service owns a path prefix; the hub forwards matched HTTP and
WS requests to its registered URL. Empty registry → pass-through.
"""

from awm.services.hub.registry import Registry, ServiceRecord, get_registry
from awm.services.hub.lease import LeaseManager, get_lease_manager

__all__ = [
    "Registry",
    "ServiceRecord",
    "get_registry",
    "LeaseManager",
    "get_lease_manager",
]
