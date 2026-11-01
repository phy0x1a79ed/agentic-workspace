# Fleet

## Purpose & Contents

This doc holds the nodes of the fleet, the role of each, and how each node is hardened. Thread 1r produces the hardening of deneb, the public host. Communication between nodes belongs here once its thread runs.

## Requirements

- **R79.** deneb runs no agent session and opens no outbound ssh. Tony: "Agents and outbound ssh should stop though" (2026-11-01).
- **R80.** deneb keeps the vault. Tony: "Deneb needs trillium." (2026-11-01).
- **R81.** The Ops page holds a static link to each box's portal and no password. Tony: "Ops links then just become static links." (2026-11-01). Tony: "The ops pages and passwords i dont know what to do with, having a centralized link to the different box portals is nice, but the login diesnt need to be there" (2026-11-01).
- **R82.** deneb runs no Penpot and no game server. Tony: "We can turn off deneb though.", "Turn off penpot*" and "Lets drop the game server." (2026-11-01).
- **R83.** deneb keeps the Zotero sync, which writes the library into the vault. The sync runs on no timer. It runs when Tony presses a Sync now button on the bibliography note. Its key reads the library and cannot write it, so a stolen key exposes only the list of papers. Tony: "Zotero can stay", "Zotero is needed to update the notes though..." and "Lets make the zotero sync off a timer and make an active sync now button for it in the biblio page/note" (2026-11-01).
- **R84.** The vault and each node's gateway portal log in with the passkey (`queue.md` R64). No password login remains. Tony: "Login to vault and peer gateways (2) both switch to r64." (2026-11-01).

## Decisions

- 2026-10-30: mizar is homebase. Tony: "mizar is homebase now." Precedence `f1ec0a5c37ace7b6`.
- 2026-10-30: alcor is the services box. mizar is the always-on worker and the dedicated dev box. Compute-bound work spreads across mizar, alcor and deneb, and mizar takes dev work first. Precedence `c6bea06ada542338`.
- 2026-11-01: deneb is a station. ssh runs one way, inbound to deneb. deneb holds no key or token for any other node. Tony: "Ssh runs one way inbound to deneb. Deneb goes back to being a station" and "deneb viewed as an untrusted channel seems good." See `queue.md` R64.

## Gap from today

Surveyed read only on 2026-11-01, from the cloud project's `VMs/tencent/` READMEs and scripts and the `feat/redux` source. Nobody logged in to deneb for it.

- **deneb is a Tencent VM behind Cloudflare.** The Tencent firewall admits ports 22, 80 and 443 from anywhere. ufw narrows 80 and 443 to Cloudflare's ranges. Cloudflare ends the public TLS and sees every request and response in plaintext (`VMs/tencent/README.md`). The Tencent account and an API key on capella can reset or snapshot the box.
- **The public edge has five doors with no session.** These are the login page, the tether relay, the federation board, vault slices and the placeholder page (`httpsfront/policy.py`). Behind a login sit the vault, Penpot, draw.io and the file viewer. A login is a static password per user, rate limited, with a lockout (`auth/INSTALL.md`). A private node's gateway also takes a daily shared password. R84 missing.
- **deneb holds a key into altair.** The `awm` account's ssh config logs in to altair as `tony` with its own key (`deneb/etc/ssh/awm-client.conf`). The tether's operator bearer is the same value on deneb and altair. Both break the station decision above and R79.
- **Agents that read untrusted text run on deneb.** Tony's Discord DM and the unimatrix0 #bots seat run as Claude Code sessions of the `awm` account. That account holds the altair key, the Claude Code login and the box's service credentials in `/etc/awm/env` (`deneb/README.md` § The relay seats). R79 broken.
- **deneb is the vault's sync hub.** altair and capella sync through it, and a sync link carries no authentication, so deneb can rewrite every note on every copy (`trilium/INSTALL.md`). Trilium runs a note's scripts in the browser of whoever reads it. The day's shared login password also lives in the Ops note (`auth/INSTALL.md`). R81 broken.
- **deneb runs more than a relay and the vault.** It also runs Penpot in Docker, the Zotero sync timer, the reporting service and a game server (`install-awm.sh` `PUBLIC_SERVICES`). R82 broken. The Zotero timer runs every 1200 s (`deneb/README.md` § The bibliography), so R83 is broken. The Zotero docs call its key read only, and `awm zotero status` reports whether it can write (`zotero/INSTALL.md`). The live key is not checked.
- **Logins over ssh are narrow.** sshd admits `tony` by key only, with passwordless sudo, and `avatar-relay` for one reverse port. fail2ban guards port 22 (`deneb/etc/ssh/90-hardening.conf`).
- **R64 is not compatible with the Cloudflare proxy.** For deneb to forward mizar's TLS unopened, mizar's name must skip the Cloudflare proxy. Today every public name goes through it.
