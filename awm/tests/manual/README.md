# Manual smoke tests — three-peer mesh

These are the human-driven validation recipes for the phases of the
decentralization arc that depend on a real multi-host setup. No
automated multi-process harness exists today; these scripts are the
authoritative end-to-end check.

The mesh under test is `capella`, `xps`, `mira` (xps replaced crux on
2026-05-20). See `~/agentic_workspace/awm_peer_tunnel_topology.md` for
the SSH tunnel layout; in short, capella↔xps is asymmetric (xps loops
back to capella) and mira is symmetric with both.

Each recipe assumes:

- All three peers have `awm/.bare` worktrees at the current `main` HEAD.
- `awm core` is running on each peer (systemd unit
  `awm-core.service`).
- `awm serve-exposed` is running on each peer (systemd unit
  `awm-exposed.service`), with `exposed.json` cross-installed via the
  peer-token bootstrap.
- A clean `awm/.dev/db.sqlite` is acceptable; the recipes do not
  preserve pre-existing rows.

If a step fails, capture the timing in the recipe's "Findings" section
and file an issue rather than continuing.

---

## Recipe A — Failover (Phase 3 validation)

Goal: confirm STANDBY/ACTIVE election converges on leader failure
within ≤9 s (3 probe rounds × 3 s default interval) and that the
STANDBY middleware returns 503+Location for user routes during the
window.

### Prep

1. Set priorities (lower wins):

   ```sh
   awm peer set-priority capella 10
   awm peer set-priority xps     20
   awm peer set-priority mira    30
   ```

2. Wait one probe round (~3 s) and confirm:

   ```sh
   awm status | jq '.leader, .role'
   ```

   On capella: `"leader": "capella"`, `"role": "ACTIVE"`.
   On xps + mira: same `leader`, `"role": "STANDBY"`.

3. Hit `/ui/` directly on xps in a browser; expect HTTP 503 with a
   `Location:` header pointing at `https://capella…`.

### Test

1. From xps, in two terminals:

   ```sh
   # Terminal 1 — poll leader on xps.
   while true; do
     date +%H:%M:%S.%3N
     curl -sk https://localhost:18443/status \
       | jq -r '.current_leader + " " + .role'
     sleep 1
   done
   ```

   ```sh
   # Terminal 2 — poll leader on mira (same shape).
   ssh mira "while true; do
     date +%H:%M:%S.%3N
     curl -sk https://localhost:18443/status | jq -r '.current_leader + \" \" + .role'
     sleep 1
   done"
   ```

2. On capella, kill the exposed listener:

   ```sh
   sudo systemctl stop awm-exposed.service
   ```

   Note the wall-clock time.

3. Watch both terminals. Record the first timestamp at which:
   - `current_leader` changes to `xps` (xps + mira should agree).
   - xps's `role` flips to `ACTIVE`.

   **Pass criterion**: ≤9 s after the kill.

4. While xps is ACTIVE and capella is down, from a fourth shell hit
   `/ui/` on mira. Confirm 503+Location pointing at the new leader
   (`xps`).

5. Restart capella:

   ```sh
   sudo systemctl start awm-exposed.service
   ```

6. Within one probe round, xps should demote and capella reclaim
   ACTIVE (lower priority wins). Record the time.

### Findings template

```
Recipe A — <date>, operator <name>

  T+0     capella exposed stopped
  T+?s    xps observed current_leader=xps (target ≤9s)   PASS/FAIL
  T+?s    mira  observed current_leader=xps               PASS/FAIL
  T+?s    /ui/ on mira returned 503+Location=https://xps  PASS/FAIL
  T+?s    capella started
  T+?s    capella reclaimed ACTIVE                        PASS/FAIL

Notes:
```

---

## Recipe B — Replication convergence (Phase 4 + Phase 5)

Goal: confirm each of the five newly CRR-registered tables converges
across capella → xps and capella → mira within ≤10 s.

The five tables (in PR order): `room_posts`, `scopes`, `session_logs`,
`messages`, `artifacts`.

### Prep

1. All three peers up, capella ACTIVE.
2. From capella: `awm replication status` should show `peers: 2,
   tables: 9` (the four phase-4 tables + the five phase-5 tables).
3. On each peer, set an env shortcut for the next steps:

   ```sh
   export AWM_PEER_TOKEN=$(cat $WORKSPACE_ROOT/.awm/peers/<peer>.token)
   ```

### Test

For each row creation below, capture the wall clock when the row is
*created on capella* and the wall clock when it's *first observed on
xps and mira* (via the listed read command). Target: ≤10 s.

#### B1 — `room_posts` (v30)

```sh
# capella
ROOM=$(awm room create --project awm --scope web-ui | jq -r .id)
awm room post "$ROOM" --body "phase8-B1" --kind text

# xps + mira (each peer)
awm room history "$ROOM" | jq '.posts[] | select(.body == "phase8-B1")'
```

#### B2 — `scopes` (v31)

```sh
# capella
awm scope create --project awm --scope phase8-B2-scope

# xps + mira
awm scope list | jq '.scopes[] | select(.scope == "phase8-B2-scope")'
```

#### B3 — `session_logs` (v32)

```sh
# capella
awm session log --project awm --scope web-ui \
  --agent-id phase8-B3 --kind milestone \
  --summary "convergence check"

# xps + mira
awm session search --query "convergence check" \
  | jq '.results[] | select(.agent_id == "phase8-B3")'
```

#### B4 — `messages` (v33)

```sh
# capella
awm inbox send --to "scope:awm/web-ui@xps" \
  --body "phase8-B4" --from "user:operator"

# xps (recipient mints id)
awm inbox fetch --scope "scope:awm/web-ui" \
  | jq '.messages[] | select(.body == "phase8-B4")'

# mira (must observe row replicated *back* from xps)
awm inbox fetch --scope "scope:awm/web-ui@xps" \
  | jq '.messages[] | select(.body == "phase8-B4")'
```

Note: B4 specifically exercises the "recipient peer mints id" rule. The
new row originates on xps; capella sees the same row via CRR.

#### B5 — `artifacts` (v34, also gates Phase 6)

```sh
# capella
echo "phase8-B5 payload" > /tmp/B5.txt
awm artifact register --project awm --scope web-ui \
  --name phase8-B5 --type doc --path /tmp/B5.txt \
  | jq -r '.id, .origin_peer'    # note ID + origin_peer=capella

# xps + mira: metadata visible
awm artifact list --project awm --scope web-ui \
  | jq '.artifacts[] | select(.name == "phase8-B5")'

# xps + mira: content federates back to capella (Phase 6)
ID=<noted>
curl -sk -H "Authorization: Bearer $(cat $WORKSPACE_ROOT/.awm/auth.token)" \
  https://localhost:18443/artifacts/${ID}/content
# expected output: phase8-B5 payload
```

### Findings template

```
Recipe B — <date>, operator <name>

  Test  Create T  xps T+s  mira T+s  Pass?
  B1    HH:MM:SS  ____.__  ____.__   ___
  B2    HH:MM:SS  ____.__  ____.__   ___
  B3    HH:MM:SS  ____.__  ____.__   ___
  B4    HH:MM:SS  ____.__  ____.__   ___
  B5    HH:MM:SS  ____.__  ____.__   ___
  B5 content fetch from xps: bytes match? ___
  B5 content fetch from mira: bytes match? ___

Notes:
```

---

## Recipe C — Lock federation smoke (Phase 7)

Optional, but worth running once after Phase 7 lands. The unit suite
(`awm/tests/test_locks_federation.py`) exercises the routing, but the
real httpx hop is only covered here.

### Test

1. From capella, create a scope:

   ```sh
   awm scope create --project awm --scope phase8-C
   ```

   Wait ~5 s for CRR convergence.

2. From xps, acquire the lock on the (capella-owned) scope:

   ```sh
   awm lock acquire \
     --resource "scope:awm/phase8-C" \
     --holder-id "xps-test-agent"
   ```

   Expected: `Lock acquired`. Behind the scenes, locks service on xps
   should resolve `origin_peer=capella` and `POST /peer/lock/acquire`
   to capella. Confirm by checking capella's exposed log:

   ```sh
   ssh capella "journalctl -u awm-exposed.service --since '1 min ago' \
     | grep -E 'POST /peer/lock/acquire'"
   ```

3. From mira, try to acquire the same lock with a different holder:

   ```sh
   awm lock acquire \
     --resource "scope:awm/phase8-C" \
     --holder-id "mira-test-agent"
   ```

   Expected: `Conflict: resource locked by xps-test-agent (...)`. Both
   mira and xps's calls should have landed on capella (where the lock
   row actually lives).

4. Release from xps:

   ```sh
   awm lock release \
     --resource "scope:awm/phase8-C" \
     --holder-id "xps-test-agent"
   ```

5. **Unreachable-peer path**: stop `awm-exposed.service` on capella,
   then re-run step 2 from xps. Expected: `federation_unreachable: ...`
   error message (not a Python traceback).

### Findings template

```
Recipe C — <date>, operator <name>

  Step 2 acquire from xps                                 PASS/FAIL
  Step 2 visible in capella exposed log                   PASS/FAIL
  Step 3 conflict reported from mira                      PASS/FAIL
  Step 4 release succeeds                                 PASS/FAIL
  Step 5 federation_unreachable message (no traceback)    PASS/FAIL

Notes:
```

---

## After running the recipes

- Commit the filled-in Findings sections under
  `awm/tests/manual/runs/<YYYY-MM-DD>-<recipe>.md` (or just paste them
  into the room post that wraps up the validation).
- If anything fails, capture the relevant log spans (`journalctl -u
  awm-exposed.service`, `journalctl -u awm-core.service`) before
  restarting.
