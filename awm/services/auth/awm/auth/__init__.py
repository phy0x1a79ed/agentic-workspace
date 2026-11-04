"""awm.auth — the credential authority service.

Mints login-password generations on a 12h cadence with 24h validity
(overlapping windows), signs sliding session tokens, and pushes the day's login
password to Discord. The ``httpsfront`` edge
enforces auth using material this service hands it; this service is the sole
authority for credential state.
"""
