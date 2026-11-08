# Comms handoff: landing the comms ports

## Purpose & Contents

This file is the handoff for the session that lands the comms group's ports: browser, rlm-browser, channel (relay and tether folded in) and social. It holds the branch heads, the land order, what the cloud change does on each node, and the steps the comms session left for the land, because each one is a land or a node rollout. The per-plugin plans are `browser.md`, `rlm-browser.md`, `channel.md` and `social.md`. Delete this file once the ports have landed and the checks pass.

## Branch heads

| Repo | Branch | Head | Where |
|---|---|---|---|
| awm | `feat/port-comms` | the commit that adds this file, or later | mizar's bare, and github |
| cloud | `feat/rust-singletons` | `b81bc42` | mizar's cloud bare, and `github.com/phy0x1a79ed/cloud` |

- `feat/port-comms` holds all four ports and the seats' Remote Control switch. It merged release `463f1a4a`. The whole Rust workspace passes (63 suites), and so do the `contract`, `kept`, `dev` and `trustpolicy` suites.
- `feat/rust-singletons` branches from cloud dev `6c01e03`, and the cloud tests pass (84 passed, 11 skipped). GitHub's cloud `dev` is `8f79bec`, ahead of mizar's bare. Merge the current cloud dev into the branch before any Ansible run.

## What the cloud change does

- `singleton_rust` in `singletons_sandbox` maps a singleton to its Rust plugin: social, browser, and relay to channel. Each unit gets `WorkingDirectory=<ws>/core/plugins/<plugin>` and a `rust.conf` drop-in. That drop-in binds `<ws>/core` and `<awm_dir>/bin/<plugin>` read-only, sets `AWM_PLUGIN` and `AWM_NODE_NAME`, and runs the binary in place of `run.sh`. ops adds operations to the same map when it ports operations.
- `social_seats` appends the Rust `[seats]` block to alcor's `social.toml`, paused. `mira_copy` writes it again after each place. The keeper's `[seat.*]` and `[keeper]` stay, unread, so a rollback to the Python social still finds them.
- `discord_seats` is retired.
- deneb's zotero keeps its timer sync and gets no orders key. Tony dropped the signed Sync-now order ("zotero_sync can go").
- deneb loads `channel` in place of `relay`, with `profiles = ["relay-host"]` and the exclude `channel/channel` in its `config.toml`. The unit stays `awm-plugin@relay`, and its `[[external]]` entry stays `service = "relay"`.

The `--check --diff` of `ansible-playbook site.yml --check --diff -l alcor,deneb --tags singletons,social_seats` at `6f01b48` changed only these files:
- alcor: `awm-plugin@social` and `awm-plugin@browser` get a new `WorkingDirectory` in `singleton.conf` and a new `rust.conf`. `social.toml` gains the seats block. That diff is suppressed, because the file names secret paths.
- deneb: `awm-plugin@relay` gets `WorkingDirectory=/opt/awm/core/plugins/channel` and a `rust.conf` with `/var/lib/awm/state/bin/channel`.

**CAUTION** The deneb `config.toml` change (`b81bc42`) came after that check, and it sits under the `awm_config` tags. Run the check again with deneb's config tags added, and confirm `config.toml` is its only new change.

The run writes drop-ins, `config.toml` and `social.toml`, and reloads systemd. It restarts no unit.

## Land order

1. Merge release, then the current cloud dev, into the two branches. Re-run the Rust workspace tests, the Python suites and the cloud tests (`mamba run -n ansible pytest tests` in `homelab/`).
2. Get the cloud Ansible slot from plugin-redux/interview. Only one Ansible run happens at a time.
3. Submit `feat/port-comms` with `scripts/land.sh submit`, and act as the batch's merger.
4. When the batch starts, before its merge, run the Ansible above without `--check`.
5. On alcor, stop the Python-era browser seat containers (`awm-browser-seat-*`). The Python seats lived in memory, so these containers have no owner after the cutover. Leave their profile dirs for the reap rule (`browser.md`).
6. Merge `feat/rust-singletons` into cloud dev, and push it at once. Tell plugin-redux/interview that the slot is free.
7. Let the batch merge and refresh, deneb first (`channel.md`). The refresh puts the binaries in `<awm_dir>/bin/` and restarts the three units on the Rust binary.

**CAUTION** Between steps 4 and 7, a Python unit that restarts finds no `run.sh`. It retries every 5 s until the refresh puts its binary in place. Keep that gap short.

## Checks after the land

1. `systemctl status awm-plugin@social awm-plugin@browser` on alcor and `awm-plugin@relay` on deneb run the binaries under `<awm_dir>/bin/`.
2. `awm-statecheck check` passes after each first Rust start, as each plugin's `state.toml` names it. Each chat's `social_history` row count did not drop.
3. social:
   - `social seats`: the live mounts are adopted. Each occupied adopted row shows `remote_control: true`, and its session is untouched. Each empty row shows `false`.
   - `social accounts` lists the accounts. mira-sourced accounts are skipped with a log line.
   - A DM round trip through Tony's adopted seat: the line reaches exactly one job, and the reply comes back. `awm services listen social seats` shows the seat's events.
   - A `seat_remote_control` from the CLI is refused.
4. browser: `seat_open` from mizar and from alcor, a tab, a screenshot, and close.
5. channel: a room with a guest, `encrypt`, a machine session and `drain`. deneb's rooms end at the cutover, and each holder gets the room-ended notice.
6. rlm-browser: enable `gamebot` on mizar for one run.
7. Only the comms plugins restarted.

The seats then stay paused until Tony turns them on (`social.md`, "Turn the seats on"). That is its own checked step.

## Known gaps

- reflection's `transcript` verb and `rc_url` in `list_other` rows come with the harness port. Until then, a next agent's brief carries no transcript path, and `rc_url` stays empty. Nothing fails.
- `awm/service_components/kept` stays. cch still imports `awm.kept`, and port-harness ports kept into awm-harness. Delete it with cch's port.
- Other nodes' `exclude` still names `relay`, as the name of a singleton they do not host. No plugin has that name now, so the entry is inert. They never run channel's relay half, because they lack the `relay-host` profile.
- `projects/agentic-commons` and its `seats/tony` scope now exist on mizar. Check that `seats/tony` and `seats/hallam-hive-general` exist on altair before the seats turn on.
