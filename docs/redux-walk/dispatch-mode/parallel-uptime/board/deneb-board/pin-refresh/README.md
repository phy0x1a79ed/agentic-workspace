# pin-refresh

## Purpose & Contents

This topic holds how the core and page pins stay current. redux-build/dev raised it.

## Result

The routine deploy rebuilds every stale pin between merge and check, and commits it as `deploy: refresh pins` (pin-refresh d28abc5e, journal e97641ad, live in release 60a1e5f8). awm-deploy.sh still refuses a stale pin as the backstop.
