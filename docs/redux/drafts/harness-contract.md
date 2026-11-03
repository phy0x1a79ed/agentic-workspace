# Harness contract, draft v1

## Purpose & Contents

This file is the contract between the `reflection` tool and a harness plugin (`install.md` R135). A harness plugin integrates one coding agent harness: `cch` for Claude Code, `dsh` for the DeepSeek harness. The file holds the contract name, the verbs a harness plugin serves with their arguments and results, the events it sends back to reflection, and what each harness serves today. The redux-harness lane owns it. The plugin manifest and the call path are in `plugin-contract.md` (redux-core lane).

## The cut

reflection is the one agent-facing tool. It holds what holds for every harness:

- who is calling, through the harness plugin that claims the caller,
- trust and mode, per session,
- the R9 session id (`sessions.md` R9),
- the ledger of owed resumes and the alarm when one is lost,
- `wait` (`sessions.md` R51).

A harness plugin holds everything specific to its harness. It finds its own sessions, types into them, starts, stops and resumes them, and watches a command through to its end. No service imports a harness plugin's code. Every call goes through the gateway (`install.md` R117 item 1).

## Manifest

Each harness plugin serves one hidden tool and declares that it implements the contract:

> ```
> [[tool]]
> name = "harness-cc"          # dsh: "harness-ds"
> major = 1
> service = "cch"
> tier = "off"
> implements = ["harness@1"]
> ```

reflection names the contract, never a provider. It lists the providers live from `GET /hub/contract/harness@1` and asks each for its platform code with `platform`:

> ```
> [[needs.contract]]
> name = "harness"
> major = 1
> ```

The contract name `harness` never changes. A breaking change to a verb below raises the major version (`install.md` R117 items 2 and 4).

## Addressing

1. A session is named by its R9 id. Its first two characters are the platform code, and they pick the harness plugin. Each plugin answers `platform` with its code: `cc` for cch, `ds` for dsh.
   The harness plugin mints the R9 ids of its sessions and maps them back to its harness's own ids. reflection reads only the platform code.
2. A caller arrives at reflection as a pid that the gateway stamped, or, from an isolated session, as the R9 id the gateway's isolated door stamped (`_caller_session_id`). An id picks its provider by platform code. For a pid, reflection asks each provider `resolve {pid}`. A provider that does not own the pid answers `{mine: false}`. Exactly one provider may answer `{mine: true}`.
3. A job id, a name or a unique prefix of an R9 id is a lookup. reflection asks each provider `find {ref}`, and refuses a ref that two sessions match.
4. A verb that acts on a session names it with `session_id` or `ref`. With neither, it acts on the session at `pid`, the caller pid that the gateway stamped on reflection's own call. reflection passes `pid` on every such verb, so the plugin can also tell when a session acts on itself.

## Verbs

Every verb takes JSON and returns JSON with `ok`. *target* below is `session_id`, `ref` or `pid` (§ Addressing, item 4). A refusal is `{ok: false, error}`. A verb the harness cannot serve answers `{ok: false, error, unsupported: true}`, never a guess.

| Verb | Arguments | Result |
|---|---|---|
| `platform` | none | `{code, name, capabilities: [verb]}`. `capabilities` lists the optional verbs it serves. |
| `resolve` | `pid` | `{mine, session_id, name, hosting, job, cwd}`. `hosting` is `terminal`, `background` or `web`. A plugin that owns the pid but cannot tell which session called answers `session_id: null` with an `error`. |
| `find` | `ref` | `{matches: [{session_id, name, job, live}]}` |
| `status` | target | `{live, state, cwd, name, model, effort, mode}`. `state` is `idle`, `busy`, `waiting` (a dialog holds the session) or `stopped`. |
| `list` | `project?`, `scope?`, `include_stopped?` | `{sessions: [status + {session_id, job, parent, pinned, started_by}]}` |
| `send` | target, `text`, `submit?`, `confirm?`, `followup?`, `delay_ms?` | `{submitted, confirmed, ticket?}`. `confirmed` says what the submit rests on, such as `record` or `enqueued`. Plain text and a slash command alike. A `followup` is typed once the command ends, and the answer carries its `ticket` (§ Events). |
| `interrupt` | target | `{interrupted}` |
| `start` | `project?`, `scope?`, `prompt?`, `name?`, `model?`, `effort?`, `trust?`, `task_id?`, `role?`, `started_by?`, `pin?`, `suggest?`, `isolated?`, `pid?` | `{session_id, job, name, cwd}` |
| `stop` | target, `keep?`, `confirm?` | `{stopped}`. With `keep` (the default) the session stays resumable by its id (`sessions.md` R10). Without it, the plugin removes the session's job. The caller's own session needs `confirm`. |
| `resume` | target, `prompt?` | `{session_id, job, live}`. Brings back a stopped session from its transcript. |
| `set` | target, `model?`, `effort?`, `name?`, `cwd?` | `{applied: {key: outcome}}` |
| `compact` | target, `followup?`, `delay_ms?` | `{submitted, ticket?}`. The plugin runs the compaction, then delivers `followup`, and signals the end with a `harness_event` (§ Events). |

Optional verbs, listed in `capabilities` when served:

| Verb | Harness | Use |
|---|---|---|
| `pending` | cc | The follow-ups the plugin still owes, each with its `ticket`. |
| `mode` | cc | Put a session back into bypass permissions. |
| `remote_control` | cc | Turn Remote Control on or off. |
| `link` | cc | Read a session's claude.ai link. |
| `adopt` | cc | Take on a job that `start` did not create. |
| `revive_pinned` | cc | Resume each pinned session whose process is gone, after a reboot. |
| `daemon_status`, `daemon_ensure`, `daemon_hold`, `daemon_release` | cc | The Claude Code daemon unit. |
| `pool_claim`, `pool_seed`, `pool_remove` | cc | The warm session that the `cx` command attaches to. |

## Events

A harness plugin reports the end of a `compact` or of any command with a follow-up by calling `reflection` fn `harness_event`:

> ```
> {"session_id": "cc0e38…", "ticket": "…", "kind": "done" | "lost",
>  "command": "/compact", "followup": "…", "reason": "…",
>  "name": "…", "cwd": "…", "told_session": true}
> ```

`done` means the session took the follow-up up after the command finished. `lost` means the plugin gave up, and `reason` says why. The plugin types a notice into a live session before it sends `lost`, and says so in `told_session`. reflection records the outcome in its ledger. On `lost` it raises the alarm: an ERROR log line, the `lost` list of `reflection pending`, and a journal message to the scope that `cwd` names.

A plugin that cannot reach reflection keeps the event and sends it again when it next starts. reflection asks each plugin's `pending` for the tickets it still holds. A settled ticket that its plugin no longer holds and that got no event is lost too.

**CAUTION** "Typed" is not "delivered". A Claude Code worker holds its prompt queue in memory, and the daemon can swap the worker for a new process mid-command. cch therefore reports `done` only when the transcript shows the follow-up taken up and, for `/compact`, the `compact_boundary`.

## What each harness serves

| Verb | cch | dsh |
|---|---|---|
| `platform`, `find`, `list` | yes | yes |
| `resolve` | yes | partly: a CLI caller, or an MCP caller while it is the only session with a turn running (§ dsh, item 2) |
| `status` | yes | yes. `waiting` comes from the event stream. |
| `send`, `interrupt` | yes | yes |
| `start`, `resume` | yes | yes. `pin` and `isolated` are unsupported. |
| `stop` | yes | partly: cancels the turn only (§ dsh, item 5) |
| `set` | yes | model, effort and name. `cwd` is unsupported. |
| `compact` | yes | yes, for a preset that mounts compaction |
| optional verbs | all in the table above | none |

## dsh

dsh drives the deepseek-harness web fork. The fork serves one RPC API on `127.0.0.1:12311` (`DSH_UPSTREAM_PORT`). It has no authentication. A call passes when it comes from loopback, sends `Content-Type: application/json` and sends no cross-site `Origin`. The dsh plugin must therefore run on the node that runs the fork. Every unary call is `POST /api/<method>` with the body `{type: "client-request", rpcId, method, payload}`. The answer is HTTP 200 with `result: {ok, value | error}`.

1. **Session id.** The fork mints `session-<uuid4>`. The R9 id is `ds` plus the 32 hex digits of that UUID. dsh maps an R9 id back to the fork's id by matching the hex in `session.list`. A fork id that holds no UUID gets no R9 id, and dsh leaves that session out of `list`. dsh creates its own sessions with a preallocated `session-<uuid4>` id, so `start` knows the R9 id before the fork answers.
2. **Caller identity.** The fork runs one `awm-mcp` client per fork process, not one per session. That client's environment and its `tools/call` requests carry no session id. A gateway call from an MCP tool therefore arrives as the pid of the fork's node process. A tool call runs inside a turn, so when exactly one session has a turn running, dsh answers `resolve` for that pid with that session. With none or several running, it answers `{mine: true, session_id: null, error}`, and reflection refuses every verb that acts on the caller's own session. A process started by the fork's bash tool carries `DSH_SESSION_ID` in its environment. dsh answers `resolve` for such a pid from `/proc/<pid>/environ`, walking up the parents to the fork's node process.
3. **Status.** The fork knows only `idle` and `running`, read from `running` in `session.list`. dsh reports `running` as `busy`. A pending approval or question is not a fork state. dsh reads it from the `GET /api/events.mux` WebSocket, which replays pending approvals when it opens, and reports `waiting`. The fork keeps no loaded-or-unloaded state that `session.list` shows, so dsh reports `stopped` only while the fork itself is down.
4. **Send and interrupt.** `send` calls `session.prompt` with `mode: "queue"`, which queues the text behind a busy turn and wakes an idle session. `confirmed` is `enqueued`. Text that starts with `/` goes to `POST /api/commands/execute {agentId, line}` instead, because `session.prompt` does not run commands. `interrupt` calls `session.cancel`, which keeps the queue.
5. **Start, stop and resume.** `start` calls `session.create {cwd, sessionId, agentPreset}`, then `session.rename` for `name`, `session.selectModel` for `model` and `effort`, and `session.prompt` for `prompt`. The fork has no route that closes or unloads a session. dsh `stop` calls `session.cancel` and answers `{stopped: true, unloaded: false}`. A session on disk resumes on the next call that needs its agent. dsh `resume` therefore sends `prompt` when there is one, and answers `live: true`.
6. **Set.** `model` and `effort` go to one `session.selectModel {provider, model, reasoningEffort}` call. `name` goes to `session.rename`. A session's folder is fixed when it is created, so `cwd` answers `unsupported`.
7. **Compact.** dsh runs `/compact` through `commands/execute`. The fork writes the durable session events `compaction/start`, `compaction/summary` and `compaction/end {error?}`. dsh waits for `compaction/end` through `session.history` or the event stream, then queues `followup`. It sends `harness_event` `done` when the history shows the follow-up as a user turn after `compaction/end`. It sends `lost` when `compaction/end` carries an error or the deadline passes. Compaction is mounted per agent preset: `standard`, `code` and `cordis` have it, and `minimal` has none. dsh answers `compact` for a session with no compaction as `unsupported`.

### Open

- **Per-session identity for MCP calls.** Item 2 names the caller only while one session runs a turn. A fork patch that adds the session id to `tools/call` `_meta` and an `awm-mcp` that forwards it as a header would close it. The patch and the header both need agreement with redux-build/core, which owns the gateway and `awm-mcp`.
- **`commands/execute` payload.** The shape `{agentId, line}` comes from the fork's command descriptor. Nobody saw a call. Test it against the live fork before dsh relies on it for `send` and `compact`.
- **No unload.** A long-lived fork holds every agent it ever resumed. A `stop` that frees memory needs a fork route.
