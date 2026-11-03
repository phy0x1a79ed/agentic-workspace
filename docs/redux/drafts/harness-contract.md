# Harness contract, draft v1

## Purpose & Contents

This file is the contract between the `reflection` tool and a harness plugin (`install.md` R135). A harness plugin integrates one coding agent harness: `cch` for Claude Code, `dsh` for the DeepSeek harness. The file holds the contract UUID, the verbs a harness plugin serves with their arguments and results, the events it sends back to reflection, and what each harness serves today. The redux-harness lane owns it. The plugin manifest and the call path are in `plugin-contract.md` (redux-core lane).

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
> uuid = "<the plugin's own tool UUID>"
> major = 1
> service = "cch"
> tier = "off"
> implements = ["8ca45e2d-96cb-497f-8e00-e4cb25ba0fd3@1"]
> ```

reflection names the contract and reads its providers from `AWM_RESOLVED_TOOLS` under `contracts`:

> ```
> [[needs.contract]]
> uuid = "8ca45e2d-96cb-497f-8e00-e4cb25ba0fd3"
> major = 1
> ```

The contract UUID never changes. A breaking change to a verb below raises the major version (`install.md` R117 item 4).

## Addressing

1. A session is named by its R9 id. Its first two characters are the platform code, and they pick the harness plugin. Each plugin answers `platform` with its code: `cc` for cch, `ds` for dsh.
2. A caller arrives at reflection as a pid that the gateway stamped. reflection asks each provider `resolve {pid}`. A provider that does not own the pid answers `{mine: false}`. Exactly one provider may answer `{mine: true}`.
3. A job id, a name or a unique prefix of an R9 id is a lookup. reflection asks each provider `find {ref}`, and refuses a ref that two sessions match.

## Verbs

Every verb takes JSON and returns JSON with `ok`. A refusal is `{ok: false, error}`. A verb the harness cannot serve answers `{ok: false, error, unsupported: true}`, never a guess.

| Verb | Arguments | Result |
|---|---|---|
| `platform` | none | `{code, name, capabilities: [verb]}`. `capabilities` lists the optional verbs it serves. |
| `resolve` | `pid` | `{mine, session_id, name, hosting, job, cwd}`. `hosting` is `terminal`, `background` or `web`. |
| `find` | `ref` | `{matches: [{session_id, name, job, live}]}` |
| `status` | `session_id` | `{live, state, cwd, name, model, effort, mode}`. `state` is `idle`, `busy`, `waiting` (a dialog holds the session) or `stopped`. |
| `list` | `project?`, `scope?`, `include_stopped?` | `{sessions: [status + {session_id, job, parent, pinned, started_by}]}` |
| `send` | `session_id`, `text`, `submit?`, `confirm?` | `{submitted, confirmed}`. `confirmed` says what the submit rests on, such as `record` or `enqueued`. Plain text and a slash command alike. |
| `interrupt` | `session_id` | `{interrupted}` |
| `start` | `project`, `scope`, `prompt?`, `name?`, `model?`, `effort?`, `trust?`, `task_id?`, `role?`, `started_by?`, `pin?`, `isolated?` | `{session_id, job, name, cwd}` |
| `stop` | `session_id`, `keep?` | `{stopped}`. The session stays resumable by its id (`sessions.md` R10). |
| `resume` | `session_id`, `prompt?` | `{session_id, job, live}`. Brings back a stopped session from its transcript. |
| `set` | `session_id`, `model?`, `effort?`, `name?`, `cwd?` | `{applied: {key: outcome}}` |
| `compact` | `session_id`, `followup` | `{accepted, ticket}`. The plugin runs the compaction, then delivers `followup`, and signals the end with a `harness_event` (§ Events). |

Optional verbs, listed in `capabilities` when served:

| Verb | Harness | Use |
|---|---|---|
| `mode` | cc | Put a session back into bypass permissions. |
| `remote_control` | cc | Turn Remote Control on or off. |
| `link` | cc | Read a session's claude.ai link. |
| `revive_pinned` | cc | Resume each pinned session whose process is gone, after a reboot. |
| `daemon_status`, `daemon_ensure`, `daemon_hold`, `daemon_release` | cc | The Claude Code daemon unit. |
| `pool_claim`, `pool_seed`, `pool_remove` | cc | The warm session that the `cx` command attaches to. |

## Events

A harness plugin reports the end of a `compact` or of any command with a follow-up by calling `reflection` fn `harness_event`:

> ```
> {"session_id": "cc0e38…", "ticket": "…", "kind": "done" | "lost",
>  "command": "/compact", "followup": "…", "reason": "…"}
> ```

`done` means the session took the follow-up up after the command finished. `lost` means the plugin gave up, and `reason` says why. reflection records the outcome in its ledger. On `lost` it raises the alarm: an ERROR log line, the `lost` list of `reflection pending`, and a journal message to the session's scope. A ticket that gets no event within its deadline is lost too.

**CAUTION** "Typed" is not "delivered". A Claude Code worker holds its prompt queue in memory, and the daemon can swap the worker for a new process mid-command. cch therefore reports `done` only when the transcript shows the follow-up taken up and, for `/compact`, the `compact_boundary`.

## What each harness serves

| Verb | cch | dsh |
|---|---|---|
| `platform`, `resolve`, `find`, `status`, `list` | yes | see § dsh |
| `send`, `interrupt` | yes | see § dsh |
| `start`, `stop`, `resume` | yes | see § dsh |
| `set` | yes | see § dsh |
| `compact` | yes | see § dsh |

## dsh

To be filled from the read-only survey of the fork on altair and capella.
