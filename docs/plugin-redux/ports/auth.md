# Port plan: auth

## Purpose & Contents

This file holds the approved port plan for auth, from task T9 of the ops group's plan (task CWPYAtyb), revised and approved on 2026-11-06, and amended on 2026-11-08 from CONTRACT.md b11adebc. It names only what changes. The Python code and its manifests are the reference for everything the port keeps. The rules that hold for every ops port are in [README.md](README.md). Later changes go in the port commits and the journal.

## Port auth, absorbing httpsfront and tlsfront, and move claude-science behind it

**C1.** httpsfront, auth and tlsfront become one plugin and one process, the `auth` service. It serves the tools `auth` and `httpsfront`. The edge keeps:
- port 12100, and every `AWM_EDGE_*`, `AWM_HTTPS_PORT` and CA env var;
- every route and session rule, byte for byte;
- the offline `portal_login` grant check, and the redirect of a browser with no session to `{approver}/signin?next=<the visit>`, which T11 set in the Python edge;
- the 24-hour sliding edge session on each node. The 12-hour session is alcor's sign-in only (CONTRACT.md b11adebc);
- leaves from the private CA, with no ACME.

**C3.** The vault, slice, tether and board mounts stay built in as reviewed constants.

Changes:
1. The stale `login` page goes.
2. The plugin and service `httpsfront` disappear. Restart lists name `auth`.
3. A new verb, `auth front {service}`, returns the state of a front declaration.
4. The leaf and `.sans` are read from `awm/services/httpsfront/.certs`, and copied once to `<state>/certs` and `<state>/sans` before the folder is deleted.
5. New leaves get a P-256 key. Existing RSA keys are read as they are.
6. Caller gate:
   - `edge_material` admits only an operator call with no plugin caller, dsh and claude-science. Those two drop out when they move behind the front.
   - `device_mint`, `device_list` and `device_revoke` admit only operator calls with no plugin caller.

**C2. Front declarations**, accepted by plugin-redux/interview:
- A plugin declares `[[service.front]]` with these keys:
  - `port`, required;
  - `upstream`, required, a loopback URL;
  - `rewrite_origin`;
  - `name`, default `gui`;
  - `paths`, prefix overrides.
- The gateway passes the tables through in `ServiceSpec`.
- For each declaration, auth opens a TLS listener with the node's edge leaf and the same session gate. It follows tlsfront's header rules:
  - it drops `Host`;
  - it sends `X-Forwarded-Host` and `X-Forwarded-For`;
  - it rewrites `Origin` when the declaration asks.
- On WSL, `url` falls back to the mesh address in `sans`.

The dsh change belongs to harness and comes after T9. claude-science (C2b) declares `gui` on 12211 and `sandbox` on 12212, which Tailscale serve publishes as 12201 and 12202, and `/_signin` moves behind `paths`. Its own front and its grant code go.

State:
- `services/auth/auth.db`, schema 7, read in place. Its `awm_settings` table holds the session TTL rows on capella and altair.
- `httpsfront.db`, copied once to `landing.db` with `VACUUM INTO`.
- The CA under `$XDG_CONFIG_HOME/remote-audio/ca`, read in place. mizar, alcor and altair hold `ca.pem` but no CA key. They never mint a root or a leaf, and they refuse to start if the leaf is missing.
- The live `auth.db` is vacuumed once in the land window, so that its free pages, about half the file on four nodes, drop old plaintext.

Dependencies: `secrets = ["AWM_APPROVER_GRANT_PUBKEY"]` and no needs. With trilium absent, every slice path returns 404.

Tests:
- Port the 257 Python tests.
- Add tests for the front declarations, the landing copy, the cert copy and the caller gate.
- Before the Python is deleted, have it write committed vectors for:
  - session and device tokens;
  - the policy over about 200 paths;
  - the SAN normaliser.
- Live parity on mizar: a sandbox gateway on port 7821 runs Rust auth on a copy of `auth.db` beside the live edge. Run a curl matrix with and without cookies, tokens and grants, then run it again on the public profile.

Landing:
- Land in a quiet window, in node order: mizar, alcor, capella, then deneb last.
- On each node, check:
  - `awm services` shows `auth` and no `httpsfront`;
  - the portal opens with the existing cookie;
  - a passkey sign-in sets both cookies;
  - `/ca.crt` matches;
  - the dashboard WebSocket and mic work.
- On deneb, also check the trilium, slice, tether and board paths.
- On capella, also check the avatar socket.
- Then land claude-science with `--restart auth,claude-science`.

Rollback: revert, and keep the live `auth.db`, because a restore revives revoked tokens. Restore it only when `state check` proves damage. After a restore, revoke again every token that the live copy had revoked. The formats are unchanged, so cookies still verify.

Gotchas:
- The edge is every node's only browser door. ssh and Tailscale remain the recovery path.
- Compare SANs as a set, or a re-mint loop starts.
- The board pool needs an explicit semaphore.
- File-hop bodies must stream.
- No log holds `?grant=`.

