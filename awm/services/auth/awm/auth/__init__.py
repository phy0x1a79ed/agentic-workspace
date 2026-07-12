"""awm.auth — the credential authority service.

Mints paired (login-password, peer-credential) generations on a 12h cadence with
24h validity (overlapping windows), signs sliding session tokens, pushes the
day's login password to Discord, and mirrors the current peer credential to the
``$AWM_PEER_CRED`` file for the SSH peer-auth channel. The ``httpsfront`` edge
enforces auth using material this service hands it; this service is the sole
authority for credential state.
"""
