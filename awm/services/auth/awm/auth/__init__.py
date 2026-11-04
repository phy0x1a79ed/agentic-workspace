"""awm.auth — the credential authority service.

Mints peer-credential generations on a 12h cadence with 24h validity
(overlapping windows), holds the secret the edge signs sliding session tokens
with, and mirrors the current peer credential to the ``$AWM_PEER_CRED`` file for
the SSH peer-auth channel. The ``httpsfront`` edge
enforces auth using material this service hands it; this service is the sole
authority for credential state.
"""
