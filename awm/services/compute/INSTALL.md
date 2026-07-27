# compute — the local-compute watchdog

Keeps one agent from wedging a shared box, without taking the box away from
agents. It watches every process, files it under the Claude Code session that
spawned it, deprioritises CPU hogs, terminates memory hogs that actually
threaten the machine, and tells the responsible agent what happened and that
the work probably belonged on remote compute.

## Install

```bash
awm/services/compute/install.sh          # AWM_ENV=<env> to target another env
```

Pure stdlib beyond the awm components it imports — `/proc`, signals, sqlite.
Nothing to build, no third-party dependency to break.

The gateway discovers the service by filesystem scan and bootstraps it; there
is no registration step. Verify with `awm compute status`.

## What it enforces

Two limits and a gate, all derived from the box's own size — nothing here is a
literal, so the same defaults behave on a laptop and on a 64-core node.

| | CPU | memory |
|---|---|---|
| **hard ceiling** (always live) | `nproc - 2` cores | total − `mem_reserve_gb` |
| **soft cap** (live only under pressure) | half the box | half the box |
| **remedy** | renice to 19, lifted on recovery | SIGTERM → SIGKILL the job's group |

The **pressure gate** decides when the soft cap applies, and also catches the
case no per-session cap can: five sessions at 30% each is the realistic route
to a wedged machine and not one of them is over any cap. Under pressure the
largest contributor is acted on regardless. Both PSI *and* the absolute
available-memory floor must agree before that fires — PSI alone spikes during
ordinary heavy work while 58 GiB is still free.

**CPU never kills.** With 16 cores a saturated box is sluggish and
self-correcting; memory exhaustion driving swap thrash is what actually freezes
it. Every kill in the ledger is therefore a memory decision.

`awm compute status` prints the live numbers; `awm compute sessions` shows
per-session usage and open violations; `awm compute explain <pid>` answers "why
was / wasn't this touched"; `awm compute decisions` is the ledger, including
every judgement that was dropped, refused, or dry-run suppressed.

## Rollout

Three postures. The service ships in **observe** and signals nothing; **shadow**
runs every judgement and records exactly what it *would* have done; **live**
acts. Sit in shadow through a normal working day and read the ledger before
going live. The success criterion is not that it caught something — it is that
it flagged nothing it shouldn't have.

```bash
awm compute arm --mode shadow    # records exactly what it WOULD do; signals nothing
awm compute arm --mode live      # acts
awm compute arm --mode observe   # instant rollback, no restart
awm compute arm                  # read the current posture
```

One string rather than two booleans because the CLI generator renders a
boolean parameter as a bare flag, which can only ever turn a setting *on* —
leaving no way to roll back from the command line, which is the one direction
that has to work under pressure.

`live` only takes effect on the production gateway. Every dev sandbox
bootstraps this service too and sandboxes share one process table, so without
that guard starting a sandbox would put a second armed watchdog on the same
processes. A sandbox's copy reports `arm_eligible: false` and stays
observation-only whatever the setting says.

## Hooks (a separate, global decision)

`install.sh` does **not** touch Claude Code's settings — these entries affect
every session on the machine, not just awm scopes, so they are installed
deliberately:

```bash
ln -s "$PWD/awm/services/compute/hooks/compute_hook.py" \
      ~/.claude/hooks/awm-compute-hook.py
```

then add to both the `PreToolUse` and `PostToolUse` arrays in
`~/.claude/settings.json`, as **additional** entries with `"matcher": "Bash"`:

```json
{ "type": "command",
  "command": "python3 -S /home/tony/.claude/hooks/awm-compute-hook.py 2>/dev/null || true",
  "timeout": 5 }
```

They compose with whatever is already there. Neither emits a permission
decision, so neither can override the existing `tmux kill-server` deny — but
re-check that deny still fires after editing the arrays.

PostToolUse is where a stopped job gets explained, because the agent is almost
always blocked on the very command that was stopped and would otherwise see a
bare `Killed`. PreToolUse covers the detached job nobody was waiting on, and
adds a one-line pressure warning before an agent launches more work.

Removing the two entries is an independent rollback from disarming the
watchdog.

## Escape hatch

An agent that legitimately needs more of the box takes a bounded, logged
exemption rather than being told no:

```bash
awm compute grant --session "$CLAUDE_CODE_SESSION_ID" \
    --mem-gb 50 --ttl-min 45 --reason "ESM-C embedding, no remote GPU free"
```

Size-bounded, at most an hour, reason mandatory. It is CLI/HTTP only and off
the MCP surface on purpose: it needs the caller's own shell to expand its
session id, and over MCP there is no shell and no honest way to know who is
asking.

## Testing

```bash
# Unit + fixture suite (fast). The fixture is a real 304-process snapshot of
# this box — attribution and the protected set can only be wrong against
# reality, and every surprise in this service came from there.
PYTHONPATH=.:../../service_components/{config,persistence,gatewayclient} \
    python -m pytest tests -q

# Synthetic hogs against deliberately LOWERED caps (1 GiB / 2 cores), scoped to
# invented sessions so the tiny caps never apply to real agents. ~1 minute.
PYTHONPATH=... python tests/live_verify.py
```

Never test the pressure trigger by driving the box toward a real freeze; move
its floor near the current value in dry-run instead.

## Gotchas

- **`rss_estimate_gb` is a screening estimate and nothing may act on it.** It
  double-counts shared pages; measured against the truth on live sessions it
  overshot by +40% to +1200%. Only the proportional-plus-swap read taken at
  decision time can trigger anything.
- **Attribution rests on `CLAUDE_CODE_SESSION_ID`**, a harness-internal
  variable. It is stable and the harness's own tooling uses it, but a future
  release could rename it, at which point this service would silently police
  nothing. It logs loudly if the attributed share collapses — watch for that
  rather than assuming silence means health.
- **Protection is enumerated, not inferred.** An MCP server carries a session
  id exactly like a build does. So does the production gateway, when an agent
  happened to start it. So does every detached SSH ControlMaster — and killing
  one of those can burn an MFA attempt toward a cluster account lockout. New
  infrastructure that agents launch needs adding to `PROTECTED` in `action.py`.
- **Restoring priority needs privilege this box does not give us.**
  `RLIMIT_NICE` is `(0, 0)`, so a nice value can be raised but never lowered;
  restore goes through `sudo -n renice`. Where that is unavailable the
  deprioritisation stands for the job's lifetime, which is recorded. The job
  still finishes either way.
- **GPU is not covered.** No pressure signal, no accounting; the card's own
  out-of-memory handling is what you get.
