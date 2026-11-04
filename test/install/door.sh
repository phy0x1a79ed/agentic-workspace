#!/usr/bin/env bash
# door.sh <token|-> <json-rpc body>: POST the body to the isolated door at 127.0.0.1:7829/mcp
# and print the reply. The bare node has bash but no HTTP client.
tok="$1"
body="$2"
exec 3<>/dev/tcp/127.0.0.1/7829
{
    printf 'POST /mcp HTTP/1.1\r\nHost: door\r\nContent-Type: application/json\r\n'
    printf 'Accept: application/json, text/event-stream\r\nConnection: close\r\n'
    [ "$tok" = - ] || printf 'X-Awm-Caller-Token: %s\r\n' "$tok"
    printf 'Content-Length: %d\r\n\r\n%s' "${#body}" "$body"
} >&3
cat <&3
