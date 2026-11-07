# Port plan: auth (absorbs httpsfront and tlsfront; moves claude-science behind it)

## Purpose & Contents

This file holds the approved port plan for auth (absorbs httpsfront and tlsfront; moves claude-science behind it), from the ops group's plan (task CWPYAtyb). Tony approved it on 2026-11-06. It is the plan as approved: later changes go in the port commits and the journal.

## Rules common to every ops port

- Each port commit adds `core/plugins/<name>/` and deletes the Python folder in the same commit. That folder holds `awm-plugin.toml`, the crate `awm-plugin-<name>` with binary `<name>`, and any pages.
- Each port removes the plugin's rows from `scripts/run-tests.sh` and adds, in the same commit, a `run.sh` that is one line, `exec` of the binary, and the binary's `[artifacts]` pin line in `deploy.toml`, in the pin format of PLAN.md T2.
- Build and test only through `scripts/rust-build.sh` with `AWM_CARGO_TARGET_DIR=$HOME/.cache/awm-target/port-ops`.
- Submit with `scripts/land.sh submit --brief <file> --restart <plugin>`. Then run the live check on mizar and on one other node.
- SQLite uses `rusqlite` with `bundled`, through the knowledge group's store crate (`awm-store`) when it has landed. Otherwise the plugin opens the database directly with the same path rule and switches to the store crate later.
- The SDK needs listed per plugin go to plugin-redux/interview. No plugin builds a private copy.

## Plan

**Replaces.**

| Path | Src | Test |
|---|---|---|
| `awm/services/httpsfront/` (board, hub_adapter, pages, policy, proxy 927, slices, store, tether, vault) | 2693 | 3251 (191 tests) |
| `awm/services/auth/` (config, hub_adapter, service, store) | 561 | 464 |
| `awm/service_components/tlsfront/` (auth, certs, portal, proxy) | 1402 | 733 |
| `awm/service_components/config/awm/config/tokens.py`, `usertoken.py` | 175 | none |

tlsfront, tokens and usertoken stay in Python until dsh and claude-science both move behind auth (C2, C2b).

**What the code shows.**
- The edge runs no WebAuthn. operations' approver runs the passkey ceremony. The edge checks alcor's Ed25519 `portal_login` grant offline.
- The edge does no ACME, SNI or PROXY. On deneb, nginx and Cloudflare handle those. On mizar and alcor, Tailscale serve ends TLS on 12100. capella is reached directly on 12100.
- Leaves come from the private remote-audio CA.

**Kept as is.** The edge stays on port 12100 and keeps reading its env vars: `AWM_HTTPS_PORT`, `AWM_EDGE_PROFILE`, `AWM_EDGE_TLS`, `AWM_EDGE_VAULT`, `AWM_EDGE_TETHER`, `AWM_EDGE_BOARD`, the port variables, `AWM_EDGE_CERT_DIR`, `AWM_TLS_EXTRA_SANS`, `REMOTE_AUDIO_CA_DIR`, `AWM_APPROVER_GRANT_PUBKEY` and `AWM_APPROVER_ORIGIN`.

**Tool surface.**

Tool `auth`, major 1, tier on. Every verb is on surfaces cli and http.

| Verb | Params | Purpose |
|---|---|---|
| edge_material | device_used:object, token_scopes:array | The secret, the device-token digests and the TTLs. Operator only. |
| status | none | `session_ttl_hours, max_session_days, device_tokens` |
| device_mint | label*, scope | Mint `awmut1.<id>.<secret>` once. Operator only. |
| device_list | none | Tokens, revoked ones included. Operator only. |
| device_revoke | id* | Set `revoked_at`. Operator only. |
| front (new) | service* | One front declaration's state: `{listener_port, upstream, tls, san, serving, error, url}` |

"Operator only" means a call that carries `as_` is refused. The hidden `config_get` and `config_set` and the ready api's `config` block ("Authentication": `session_ttl_hours` 24, `max_session_days` 30) are kept. They feed the dashboard's settings card. The card's key stays `auth`, because the service is named `auth`.

Tool `httpsfront`, major 1, tier off, now a second tool of the auth plugin. It keeps one verb, `status`, with its full field set.

**Edge routes**, kept byte for byte:
- `/ca.crt` and `/ca.pem`.
- `/__auth/portal`, `/__auth/logout` and `/__auth/whoami`.
- `/`, the landing page, and the `/__landing/*` routes.
- `/trilium/*` to the vault on 12511, with its refusal list.
- `/slice/<token>/*`, resolved through a plain call to `trilium slice_resolve`.
- `/tether` and `/room/...`, keeping tether's room and slot grammar, on 12520.
- `/board/...`, with its own 64-connection pool and body caps, on 12521.
- On the public profile, `/invoke` admits only a narrow trilium body.
- Every other path relays to the hub with `X-Awm-As: user:<sub>`, after the public profile's allow-list.

Sessions:
- An `awm_session` HMAC token slides until `max_session_seconds`. `awm_as` carries the user.
- A device bearer is settled on its own branch.
- A scoped `avatar-stage` token admits only its one socket.
- `X-Forwarded-*` is trusted only from loopback, as uvicorn does.

**Changes to the surface.**
1. The `login` page is removed. It describes retired one-shot links, and nothing links to it.
2. The plugin and service `httpsfront` disappear. The plugin `auth` runs one service, `auth`, that serves both tools. Restart lists name `auth`.
3. A new verb `auth front {service}` returns the state of that service's front declaration (see C2).
4. The default cert dir moves to `<state>/certs`, and `.sans` moves to `<state>/sans`. The env overrides keep their meaning.
5. New leaves get a P-256 key. Existing RSA roots and leaves are read as they are.

**State.**
- `services/auth/auth.db` (schema 7) is read in place. The 1→7 migration chain is kept, and a version above 7 refuses the start.
- `services/httpsfront/httpsfront.db` (landing tags and names) is copied once by `VACUUM INTO` to `services/auth/landing.db`. The original is the backup.
- The legacy cert dir and `.sans` are copied, never moved, to the new paths, because dsh still borrows them.
- The CA dir is read in place. No root is minted while either half exists.
- Spent grants stay in memory, as today.

**Dependencies.**
- `secrets = ["AWM_APPROVER_GRANT_PUBKEY"]`, `network = []`, `platform = ["linux"]`.
- No `[[needs.tool]]`. The `auth.edge_material` need goes because both halves now share one process. The `trilium.slice_resolve` need goes because trilium is not core. With trilium absent, every slice path is a 404.
- The gateway's `/hub/plugins` feeds the landing page and the front declarations.

**Consolidations.**
- **C1. One plugin, one process.** httpsfront, auth and tlsfront become one service. The edge reads `auth.db` directly, and `edge_material` remains for out-of-process fronts. The session and device-token codecs become one module.
- **C2. Every core TLS front runs in auth, through front declarations.** This is the cross-group interface with dsh, accepted from plugin-redux/interview's proposal.
  - A plugin declares `[[service.front]]` with these keys. `${VAR:-default}` is expanded from the node env.
    - `port`, required.
    - `upstream`, required: a loopback `http://` URL. dsh's upstream is its harness port (12311), not its gateway mount.
    - `rewrite_origin`, default false.
    - `name`, default `gui`.
    - `paths`, optional: `[{prefix, upstream}]` overrides that send one path prefix to another loopback upstream on the same origin.
  - The gateway passes the tables through raw, as it passes `[[service.archive]]`. `ServiceSpec` gains `front`.
  - auth reads the declarations from `/hub/plugins` at start and on each table change, or every 15 s without a change notice.
  - For each declaration, auth opens a TLS listener with the same session gate, `/__auth/*`, the CA routes, and a streaming single-upstream relay with WebSocket.
  - A declared front reuses the node's edge leaf (`cert.pem`, `key.pem` from the cert dir) and never mints its own, because a trust-consumer node cannot mint.
  - The relay keeps tlsfront's header rules:
    - it drops the browser's `Host`, so the upstream sees its own address;
    - it sends `X-Forwarded-Host` and `X-Forwarded-For`;
    - with `rewrite_origin`, it rewrites a sent `Origin` to the upstream's own origin.
    dsh's Origin=Host fence depends on that pair.
  - `auth front`'s `url` uses the node's mesh address. On WSL, where the interface list misses it, `url` falls back to the mesh address in the `sans` file, as dsh's `_declared_mesh_address` does today.
  - A port collision or a non-loopback upstream is refused, with the reason in `auth front`.
  - The dsh manifest change belongs to harness's dsh land, which comes after T9. dsh declares port 12130 with `rewrite_origin=true`, then deletes its own listener, borrowed leaf, tlsfront and grantlib, its grant-key secret and its auth need. auth opens no port before dsh declares it, and dsh's own front holds 12130 until then.
- **C2b. claude-science moves behind auth.** claude-science is not core and stays Python, but its own TLS front goes, so R115's "no other plugin runs a TLS front" holds.
  - It declares two fronts: `gui` on `${CLAUDE_SCIENCE_FRONT_PORT:-12201}` and `sandbox` on `${CLAUDE_SCIENCE_SANDBOX_FRONT_PORT:-12202}`, two ports for two browser origins.
  - Its `/_signin` route, which mints the workbench nonce, moves from the tlsfront app to a small loopback listener in its own service. The `gui` front sends it there through `paths`. The nonce form then posts same-origin, as today.
  - It deletes `front.py`'s listeners, its `.certs`, its tlsfront and grantlib imports, its grant-key secret and its auth need.
  - This task lands that change, with or after T9. tlsfront then has no importer once dsh has moved, and the last of the two lands deletes it.
- **C3. Kept built in.** The vault, slice, tether and board mounts stay in auth as reviewed constants with their tests. A byte-for-byte port of the security boundary keeps the review tractable.
- **C4. Left outside.** alcor's nginx and lego front for `auth.tony-xy-liu.com` serves operations' approver. It is cloud system config, not a plugin.

**Rust crates added.**
- `rustls` and `tokio-rustls` with the ring provider. aws-lc-rs is ruled out because it needs cmake.
- `hyper` 1, `hyper-util` and `http-body-util`, for raw-path forwarding and streaming.
- `tokio-tungstenite`.
- `rcgen`, `x509-parser`, `rustls-webpki` and `rustls-pki-types`. They replace the `openssl` CLI.
- `hmac`, `sha2`, `subtle`, `percent-encoding` and `regex`.
- `libc` `getifaddrs`, which replaces `hostname -I`.
- No webauthn-rs and no ACME crate.

**SDK needs.**
- Hidden handlers plus a `config: {title, schema}` block in the ready api.
- A `/hub/plugins` accessor and a change notice.
- Background listeners bound to the service lifetime, whose crash does not take the verbs down.
- Default surfaces when a verb sets none.

**Test plan.**
1. Port all tests: tlsfront's 39, httpsfront's 191 across its 14 files, and auth's 27. The cert tests use rcgen fixtures plus two committed openssl-made RSA PEM pairs.
2. Add tests for the declaration parser (expansion, `paths`, refusals, port collision), the leaf reuse, the header rules of a declared front, and the landing migration.
3. Before deletion, a Python script writes committed vectors:
   - session tokens both ways, with the slide and ceiling;
   - the device-token table;
   - `policy.classify`, `allows` and `invoke_allows` over about 200 paths;
   - the mount `owns`, `refused` and `upstream_raw_path`;
   - `portal` and `login_url` quoting;
   - the SAN normaliser.
4. Live parity on mizar. A sandbox gateway (port 7821) runs the Rust auth against a copy of `auth.db`, beside the live Python edge. Compare:
   - the describe diffs;
   - `status`, `device_list` and `config_get`/`config_set`;
   - a curl matrix with and without a cookie: the routes, the refused paths, device and scoped tokens, good, spent, wrong-host and expired grants, a WebSocket with a subprotocol, and the `X-Forwarded-For` cases;
   - the same matrix again with the public profile and TLS off, standing in for deneb.

**Landing.**
1. The awm-trust additions (T1) land first.
2. Gateway: `ServiceSpec.front` passes through, and `plugin-contract.md` gains § Front declarations. This rides the PLAN.md T2 gateway change, or one small commit.
3. Commit the vector script and its JSON.
4. One port commit: add `core/plugins/auth` with two tools and delete both Python services. Update `run-tests.sh` and the gateway tree test's deneb list. The cloud `host_vars/deneb.yml` `awm_plugins` and the restart lists drop `httpsfront` in the same batch.
5. Land in a quiet window. Restart `auth`. Roll out to mizar first, then alcor, then capella, and deneb last.
6. Check on each node:
   - `awm services` shows `auth` ready and no `httpsfront`;
   - `httpsfront status` shows `serving` and the same `tls` and `san`;
   - the portal opens with the existing cookie;
   - a sign-out, then a passkey sign-in, sets both cookies;
   - `/ca.crt` matches;
   - the dashboard updates over its hub WebSocket;
   - mic gets the microphone.
7. On deneb, also check:
   - nexus lands on `/trilium/` with live updates;
   - a slice link opens with no session;
   - `/tether` serves;
   - `/board/stream` streams with a board bearer;
   - a path off the list is a 404.
8. On capella, the avatar stage socket authenticates with its scoped token.
9. Then the claude-science commit (C2b). Restart `auth,claude-science`. Check that both workbench ports serve through auth, `/_signin` signs in, and previews open on the sandbox origin.
10. Rollback is a revert on `release` plus a deploy. The Python env still exists until PLAN.md T8, the formats did not change, and cookies still verify.

Gotchas:
- The edge is the only browser door on every node. ssh and Tailscale remain the recovery path.
- Compare the SAN set as a set, or `getifaddrs` order triggers a re-mint loop.
- A trust-consumer node must read its RSA leaf as it is and never re-mint it.
- The board pool needs an explicit semaphore, because a hyper pool has no connection cap.
- Gigabyte file-hop bodies must stream.
- No log ever holds `?grant=`.
- pydantic's lax coercion in `config_set` must be mirrored. The parity check covers it.
