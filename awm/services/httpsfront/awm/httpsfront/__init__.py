"""awm.httpsfront — whole-gateway HTTPS front (TLS reverse proxy).

Terminates TLS on an off-host ``0.0.0.0:<port>`` listener and transparently
reverse-proxies every request (HTTP + WebSocket) to the loopback awm gateway, so
the entire awm surface is reachable over one HTTPS origin — the secure context
that browser APIs like ``getUserMedia`` require off-localhost.
"""
