# redux-fleet plan

## Context

The redux build runs one lane per area overnight. This lane builds the machine side of the fleet: a plugin that wakes the home boxes and hands Tony to a standby agent while mizar or alcor restarts, and the Ansible tree that the one-pass fleet config run (fleet.md R108) applies later. fleet.md R140 splits the work in two. The wake plugin is awm product code on awm's `feat/redux-fleet`. The Ansible tree, the node config and the plugin placement are Tony's machines, so they land in the cloud project's scope `cloud/redux-fleet` under `homelab/`, also on a branch `feat/redux-fleet`. No host changes tonight.

## What you said

> Your lane: three fleet features, as code and config in the tree, applied to no host.

> a wake plugin. mizar and alcor wake each other with Wake-on-LAN, and either wakes capella. alcor is mizar's standby: design and build how alcor takes over answering requests through an agent while mizar restarts, and how pinned jobs come back after a reboot.

> Write alcor's firewall change (port 22 admits mizar, capella and lyra) as config for the fleet's config pass (fleet.md R108), but do not apply it.

> These run on plugins under the contract redux-core (job redux-build/core) is building; agree the interface with it, and keep them runnable under today's Python gateway until it exists.

Coordinator, on the lane change:

> You keep R139 wake and standby, R98 item 1, and gain deneb hardening config (fleet.md R79 to R84) and the config pass (R108); redux-build/trust (job 563d3f5a) writes its ssh, firewall, users and sudo config into your pass.

Coordinator, on R140 (2026-11-03):

> Move the R108 config pass out of awm's fleet/ tree into the cloud project. Create a scope there with scope create, project cloud, scope redux-fleet. It is a local branch with local commits only. Put the pass under homelab/, next to VMs/.

> deneb's machine setup stays in the cloud project. Ansible there replaces its provision.sh. Keep the cloud project's README rule: main means what is on the box. Write the new setup, but do not retire the old scripts or apply anything.

> Keep only product code in awm/redux-fleet: the wake plugin and the standby code.

## Issues

- I1. No awm code wakes a machine. Tony or an agent runs `wakeonlan` by hand from mizar (`~/handoff/wol-run.sh`).
- I2. Nothing answers Tony through an agent while mizar restarts. On 2026-10-29 Tony watched the reboot from a hand-started session on alcor.
- I3. Pinned jobs do not come back after a reboot (`adopted=0 respawned=0 dead=2`).
- I4. alcor's firewall admits port 22 from all of `100.64.0.0/10`, `172.16.0.0/24` and `10.0.0.0/24`. Only the authorized keys limit logins to mizar.
- I5. No Ansible tree for the swarm exists. The 2026-11-02 proof of concept left only an inventory and one read-only check in a job tmp folder.
- I7. No file says which plugins each node runs. Each node's install picks its own set by hand, and deneb's `install-awm.sh` holds its own list.
- I6. deneb runs agent sessions, holds an ssh key into altair, runs Penpot and the game server, syncs Zotero on a timer, and posts the day's login password into the Ops note.

## High-level goals

- G1. Any home box that sleeps or powers off can be started from its partner without a human at the switch.
- G2. Tony always has an agent to talk to while mizar or alcor restarts.
- G3. The sessions Tony pinned come back on their own after a reboot.
- G4. The new pieces fit the redux plugin contract and still run under today's gateway.
- G5. The fleet has one Ansible tree that the config pass can apply in one run, with sibling lanes adding their roles to it.
- G6. deneb's hardening decisions exist as config, ready for that run.
- G7. One place in the cloud project says which plugins each node runs.

## Acceptance criteria

- `awm/plugins/wake/` holds `awm-plugin.toml` (contract draft v1, commit 581717e9 on `feat/redux-core`) and a `run.sh` that the Python gateway loads as today's services.
- The `wake` tool's `wake` verb sends a magic packet only along the R139 matrix: mizar to alcor, alcor to mizar, and either to capella. Any other pair is refused with the reason.
- `wake` waits for the target's ssh port and returns the seconds it took, or a timeout result.
- A handover verb on the restarting node asks its partner to take the standby, and the partner starts a briefed standby session through `reflection start_other`.
- The partner's watchdog sends Wake-on-LAN when the restarting node stays down past its deadline.
- A boot runner calls the harness's `revive_pinned` over the gateway once the gateway answers, and then releases the partner's standby with the report.
- No code in the plugin imports `awm.claudedaemon` or another plugin.
- Unit tests for the plugin pass under `mamba run -n awm pytest`.
- The cloud scope's `homelab/` holds `ansible.cfg`, `inventory/hosts.yml`, `site.yml`, `requirements.yml` and the roles `awm_config`, `wake_node`, `alcor_ssh_door` and `deneb_station`.
- `awm/redux-fleet` holds no inventory, host name list or role. Only the wake plugin and its tests change there.
- `ansible-playbook --syntax-check homelab/site.yml` passes, and each of my roles converges in check mode and then for real against a throwaway Ubuntu container.
- A test fails when the inventory's hosts and the managed nodes of `homelab/edges.yml` disagree, and when any play targets mira.
- `homelab/inventory/group_vars/all/plugins.yml` names the plugins of every managed node, and a test fails when a plugin dropped by R134 appears in it.
- deneb_station covers each step of `VMs/tencent/deneb/provision.sh`, and the old scripts stay in place unchanged.
- No secret, password or token is in the tree.

## Tasks

- T1. Commit this plan (G4).
- T2. Build the Wake-on-LAN core of the wake plugin (G1, G4).
- T3. Build the boot runner that revives pinned jobs through the harness (G3, G4).
- T4. Build the standby handover between mizar and alcor (G2, G4).
- T5. Write the plugin manifest and the legacy run path (G4).
- T6. Commit a checkpoint and journal the phase (G1, G2, G3).
- T7. Compact.
- T8. Lay out the homelab Ansible tree in the cloud scope (G5).
- T8b. Write the plugin placement and the awm_config role (G5, G7).
- T9. Write the wake_node role (G1, G3, G5).
- T10. Write the alcor_ssh_door role with the ufw defaults (G5).
- T11. Write the deneb_station role that replaces deneb's provision.sh (G6).
- T11b. Build the Zotero Sync now order puller against the approval panel (G6).
- T12. Test the tree in throwaway containers (G5, G6).
- T13. Commit a checkpoint and journal the phase (G5, G6).
- T14. Compact.
- T15. Merge the trust lane's roles when they land and rerun the tree tests (G5).
- T16. Report to the coordinator (G5).
- T17. Debrief.

## Approach by task

### T1. Commit this plan

Commit this file alone on `feat/redux-fleet`.

Gotchas: the area docs under `docs/redux/` stay untouched. Proposed doc changes go in Callouts below.

### T2. Build the Wake-on-LAN core of the wake plugin

A new Python service under `awm/plugins/wake/`, in the shape of today's services (`ServiceAdapter`, `API_MANIFEST`, handlers). The node table comes from a config file on the node (`$AWM_DIR/wake.toml`), which the wake_node role renders from the inventory. It names each target's MAC, broadcast address and ssh probe address, and which nodes may wake it. The plugin itself carries no fleet detail (install.md R119 item 1). The packet goes out on a plain UDP broadcast socket to port 9, with no root. The verbs are `wake` (send, then probe the target's port 22 until it answers or the budget runs out), `status` (probe only) and `nodes` (the table as this node sees it).

Gotchas: send to both the subnet broadcast and `255.255.255.255`, as the hand script does. capella needs about 82 s to WSL ssh, so the default budget is 180 s. A target that answers before the packet goes out reports `already_up` instead of a wake time. Tests use a fake socket and a fake probe, never the LAN.

### T3. Build the boot runner that revives pinned jobs through the harness

The harness lane (job b7312951) owns the revival itself: it adds `revive_pinned` to reflection today and to the hidden `harness-cc` tool after the recut. It reads `pins.json`, gives a pinned job a lineage record when it has none, resumes each pinned job whose process is gone, and returns `revived`, `live` and `failed`. The wake plugin adds a `boot` verb and a one-shot entry point (`python -m awm.wake.boot`), run by a systemd user unit after the gateway and the Claude daemon. It waits for the gateway, calls `revive_pinned` once through `POST /invoke`, then releases the partner's standby (T4) with the report. The tool name comes from `AWM_RESOLVED_TOOLS`, with the legacy name `reflection` when the Python gateway sets no such file.

Gotchas: no code here reads `pins.json` or imports `awm.claudedaemon`. Until harness commits `revive_pinned`, the tests use a fake gateway. The gateway takes a while to answer after boot, so the runner retries with a deadline and still releases the standby on failure, naming what failed.

### T4. Build the standby handover between mizar and alcor

The restarting node runs `handover` with a reason and an expected downtime. It calls its partner's `standby_take` over the gateway with `peer`. The partner records the handover, starts a standby session with `reflection start_other` (Remote Control on, a brief that says which node is down, what Tony may need and how to hand back), and arms a watchdog. The restarting node then runs its reboot command when asked to, or returns the plan when not. The boot runner of T3 calls `standby_release` on the partner with its resume report. The partner messages the standby session, which tells Tony and stops. The watchdog sends Wake-on-LAN once the deadline passes with the node still down, and again at a back-off, and tells the standby session each time. The pairing is symmetric, so mizar is alcor's standby by the same code.

Gotchas: the reboot command needs a polkit rule for the normal account (fleet.md R101 removes sudo), which T9 writes. No reboot runs tonight: tests swap in a fake reboot command. alcor reaches only mizar (R87), so the partner call from alcor goes over alcor's edge to mizar, and the call from mizar to alcor uses mizar's edge. A standby session started across a node keeps the starter's trust level (R104 item 3).

### T5. Write the plugin manifest and the legacy run path

Write `awm-plugin.toml` with `[plugin]`, `[placement] nodes = ["mizar", "alcor"]`, one `[[service]]`, one `[[tool]]` `wake` with a new uuid4, `[needs] network = ["udp:9 broadcast"]` and `[[needs.tool]]` for reflection at `uuid5(NAMESPACE_URL, "awm:tool:reflection")`. Add `run.sh`, `install.sh`, `pyproject.toml` and `service.toml` so the Python gateway loads it today.

Gotchas: today's gateway discovers services only under `awm/services/*`. If it cannot load a folder under `awm/plugins/`, add a symlink `awm/services/wake -> ../plugins/wake` and say so in the commit.

### T6. Commit a checkpoint and journal the phase

Commit T2 to T5 with the tests green. Post a journal entry with the verbs, the open harness question and the test count.

Gotchas: commit only this lane's files.

### T7. Compact

Handoff: the plugin lives at `awm/plugins/wake/` (13ea967a), its 26 tests pass with `PYTHONPATH=<plugin>:<ws>/awm/service_components/gatewayclient mamba run -n awm python -m pytest awm/plugins/wake/tests`, and today's gateway finds it with `AWM_SERVICE_ROOTS=<ws>/awm/plugins`, which wake_node must set in the node's `.awm/env`. Comms with siblings go through SendMessage by session name (build-rules rule 0), never reflection message. The harness lane will message when `revive_pinned` lands on `feat/redux-harness`. and the plan file names the next task. Re-read `build-rules.md` and `lanes.md` in `/home/tony/.claude/jobs/8162a704/tmp/` after the compaction. Agreed with trust (job 563d3f5a): `fleet/roles/<role>`, `fleet/site.yml`, `fleet/inventory/hosts.yml` mine, `homelab/edges.yml` theirs, ufw defaults and alcor port 22 mine. Run `reflection(verb="compact", args={followup:"continue with T8 of docs/redux/drafts/redux-fleet-plan.md"})`.

### T8. Lay out the homelab Ansible tree in the cloud scope

The tree lives at `cloud/redux-fleet/homelab/`, next to `VMs/`. Lift the POC's `ansible.cfg` conventions (BatchMode, ControlPersist) into `homelab/ansible.cfg`. The inventory names mizar (local), alcor, mira, capella, lyra, deneb and shogun, with mizar's ssh aliases and `ansible_python_interpreter=/usr/bin/python3` pinned everywhere (the POC found alcor's discovery picks the awm env). Groups: `swarm`, `home_lan`, `stations`, `wsl`. `requirements.yml` pins `ansible.posix` 2.2.2 and `community.general` 13.2.0, as arbutus-infra does. `site.yml` holds one play per role, with host limits. mira is in the inventory for facts only, and no play targets it before the trial week ends (R89 item 1). The inventory takes each node's ssh address from `homelab/edges.yml`, so the addresses live in one file. A Python test checks the inventory against it.

Gotchas: shogun manages altair (R91), so altair is not in this inventory. The test skips with a clear reason until trust's `homelab/edges.yml` is merged. Trust commits it on its own scope `cloud/redux-trust`, and T15 merges it.

### T8b. Write the plugin placement and the awm_config role

R140 puts the install config that places plugins on nodes in the cloud project. `homelab/inventory/group_vars/all/plugins.yml` lists the plugins every node runs, and host_vars add or remove per node: rlm-factorio on mizar only, engcyc on altair for dev only, and the singletons homed by `edges.yml`. The `awm_config` role writes the node's plugin list and copies `edges.yml` to `{{ awm_dir }}/edges.yml`, where awm reads it. It also removes the plugins R134 drops and the compute Claude Code hook, gated behind `awm_sweep_drop`, false by default.

Gotchas: the plugins lane names showtime and sirius, which `edges.yml` does not list, so their placement waits on trust adding them. altair is managed by shogun, so its placement is a file for shogun's run, not a play here.

### T9. Write the wake_node role

On mizar and alcor: render `wake.toml` from host_vars, install the boot-resume user unit, add the polkit rule that lets the normal account reboot, and make Wake-on-LAN survive a reboot with a systemd `.link` file that sets `WakeOnLan=magic` on the wired NIC. On capella the role does nothing in WSL, because the Windows NIC setting decides its wake.

Gotchas: both NICs read `Wake-on: g` today, and the BIOS setting on mizar was never read, so a check task reports it and does not fail. MACs sit in host_vars, never in plugin code.

### T10. Write the alcor_ssh_door role with the ufw defaults

On alcor: set ufw defaults (deny incoming, allow outgoing), admit 22/tcp only from the LAN and Tailscale addresses of the nodes that hold an edge into alcor in `homelab/edges.yml` (mizar, capella, lyra), and delete the three blanket 22/tcp rules. Keep the gateway port rules as they are, because the trust lane decides them.

Gotchas: lyra is not on the tailnet yet and capella's LAN address comes from DHCP, so the role asserts that each source address is set and refuses to run otherwise. Add the new rules before deleting the old ones, so a run cut in the middle never locks mizar out. **WARNING** for the later live run: the rule set must admit the controller's own address.

### T11. Write the deneb_station role that replaces deneb's provision.sh

Port each step of `VMs/tencent/deneb/provision.sh` and `install-awm.sh` into tasks, read from the cloud tree with no login to deneb. The old scripts stay, and the README rule holds: `main` means what is on the box, so this role reaches `main` only after it runs against deneb with no changes. Then layer the hardening decisions on top.

1. R79: drop `cx`, `reflection` and `keep` from deneb's service set, stop the awm account's Claude daemon unit, remove the `awm-client.conf` key route into altair, and add an nftables rule that drops new outbound tcp/22. All of it is behind `deneb_agents_moved`, false by default, because the Discord seats move to mizar first (users.md R129).
2. R80: keep `trilium` in the service set.
3. R81: turn off the login-password push into the Ops note, and render the Ops note body as static portal links from the inventory.
4. R82: drop `penpot-view`, `penpot-plugins` and `rlm-factorio`, and stop the Penpot compose project.
5. R83: set `ZOTERO_SYNC_ENABLED=0` and `ZOTERO_STREAM_ENABLED=0`, and check that `awm zotero status` reports a key that cannot write.
6. R84: nothing yet. It needs the passkey of queue.md R64.

Gotchas: deneb's live service list may differ from the cloud tree, so the role states the wanted set and the play diffs it in check mode. Removing the altair key route touches F6, which the trust lane may also claim, so check with it before T13. A step of provision.sh that needs a secret reads it on deneb at run time, never from this tree.

### T11b. Build the Zotero Sync now order puller against the approval panel

The coordinator chose the R65 panel for R83 (2026-11-03). The queue lane's panel interface v1: the bibliography note embeds `https://<approver-origin>/panel/button?kind=zotero_sync&node=deneb&label=Sync%20now`. A press becomes an order envelope `{"order": {order_id, kind, node, payload, not_after}, "sig", "key_id"}` signed by alcor's grant key. The Zotero service on deneb long-polls `approver order_pull(node:"deneb", kinds:["zotero_sync"], wait_s<=55)`, checks each envelope with `verify_order` from `awm/plugins/approver/grantlib/` and alcor's public key, checks `not_after`, keeps a seen set of `order_id`, runs the sync, and calls `order_done`. The deneb_station role adds the approver origin to the vault's CSP `frame-src` and installs alcor's grant public key.

Gotchas: the puller is off until its config names the approver and the key, so today's deneb keeps working. Tests use a fake approver and a locally made Ed25519 key. If `grantlib` is not committed yet, import it lazily and test against a stub that holds the same signature.

### T12. Test the tree in throwaway containers

Run `ansible-playbook --syntax-check`, then run each of my roles against a throwaway `ubuntu:24.04` container through the `community.docker` connection: check mode first, then a real run inside the container, then a second run that must report no changes. ufw and nftables need `NET_ADMIN` in the container. Systemd units are rendered and checked, not started.

Gotchas: the containers run under the local Docker daemon and touch no host config. Remove every container at the end of the run.

### T13. Commit a checkpoint and journal the phase

Commit T8 to T12. Journal the tree layout and the test results.

### T14. Compact

Handoff: the tree under `homelab/` passes its tests, and the next step is merging trust's roles. Run `reflection(verb="compact", args={followup:"continue with T15 of docs/redux/drafts/redux-fleet-plan.md"})`.

### T15. Merge the trust lane's roles when they land and rerun the tree tests

Merge `feat/redux-trust` into `feat/redux-fleet` locally once trust says its roles are ready. Rerun the syntax check and the inventory test. A merge is local only, by build rule 1.

Gotchas: if trust is not ready by the end of the run, skip this task and name it in the report.

### T16. Report to the coordinator

Send one line to job 8162a704 with `reflection(verb="message")`: what landed, the commit, and the open questions.

### T17. Debrief

Run the `debrief` skill. Then stop the session.

## Callouts

- alcor's live ufw admits port 22 from the whole LAN and tailnet. The gap entry "firewall admits port 22 from mizar only" in fleet.md § Gap describes the authorized keys, not the firewall. Proposed doc change: "alcor's firewall admits port 22 from the whole LAN and tailnet. Only its authorized keys hold logins to mizar."
- lyra is not on the tailnet, so the alcor_ssh_door role cannot run until lyra has a fixed source address.
- R120 is parked by the lanes file. This plan does nothing for it.
- The sweep must fill in values this tree leaves empty: `approver_grant_pub` (from `awm approver keys`, which also gives `awm_ssh_user_ca`), `wsl_windows_user` for capella (capella was asleep, and the wsl_bridge role refuses to run without it), and lyra's addresses in `homelab/edges.yml`.
- The approver origin `https://approve.tony-xy-liu.com` is the queue lane's proposal. Its DNS and TLS are a host change Tony has not confirmed.
- The sweep must check that `https://nexus.tony-xy-liu.com/trilium/etapi/app-info` reaches Trilium, not nginx's auth wall. The queue's board writer on alcor calls that path. If it hits the wall, set `approver_trilium_url` to a tailnet URL.
- deneb sends no Content-Security-Policy today, so the approver's iframe needs no `frame-src`. A CSP added later must list the approver origin.
- showtime and sirius are not in `edges.yml`, so engcyc's home on showtime and the Penpot stack on sirius have no play.
- The cloud branch `feat/redux-fleet` stays off `main` until each role has run against its box with no changes, per the cloud README. Proposed cloud README change: add `homelab/` to the Purpose & Contents table as "the fleet's Ansible tree: inventory, edge list and roles for every managed node".

