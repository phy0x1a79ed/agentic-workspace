"""awm.tlsfront — the TLS front every off-host awm listener is built on.

``certs`` mints and validates the leaf off the shared root CA, ``auth`` and
``portal`` check awm's edge session, and ``proxy`` relays HTTP and WebSockets to
one loopback upstream. ``httpsfront``, ``dsh`` and ``claude-science`` each pin
this library in their own env rather than importing one another.
"""
