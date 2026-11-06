"""awm.auth — the session secret and the device tokens.

Holds the secret the edge signs sliding session tokens with, and the operator's
device tokens. The ``httpsfront`` edge enforces auth using material this service
hands it; this service is the sole authority for that state. No login lives
here: a person signs in with the passkey at alcor's approver (fleet.md R84).
"""
