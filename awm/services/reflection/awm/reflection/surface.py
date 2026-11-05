"""The reflection tool's verbs: what MCP, the CLI and HTTP show.

Every verb here holds for any harness. Each verb's work happens in the harness
plugin of the session it acts on (`install.md` R135). A verb only one harness
has, such as Claude Code's daemon and warm pool, is that plugin's own.
"""
from __future__ import annotations

from typing import Any

DESCRIPTION = "Act on Claude Code and other agent sessions: your own (compact, mode, message_self) and others on this node (start_other, list_other, message, stop, ...). reflection finds the session's harness plugin and asks it."

FUNCTIONS: list[dict[str, Any]] = [{'name': 'message_self',
  'tool': 'reflection_message_self',
  'description': 'Type a command/text into YOUR OWN prompt and submit it (no Escape, '
                 'so it QUEUES behind the current turn). Use for self-directed slash '
                 'commands like /compact or /model opus. Works the same whether your '
                 'session runs in a terminal or as a background job — you do not need '
                 'to know which. It types only into your own session; `message` types '
                 'into another. A submitted slash command is auto-trailed by a '
                 'follow-up prompt (override with `followup`) so the session has a '
                 'next turn once the command finishes — a bare command would otherwise '
                 'go idle. The follow-up is DEFERRED (injected after the command '
                 "completes, never queued behind it) so it can't run ahead of the "
                 'command. Modal commands (/status, /model with no arg, …) are '
                 'refused — they trap input and freeze the session. So is every '
                 '/mcp line: use the `mcp` verb.',
  'timeout': 60,
  'params': [{'name': 'text',
              'type': 'string',
              'required': True,
              'description': "The line to inject, e.g. '/compact' or '/model opus'."},
             {'name': 'enter',
              'type': 'boolean',
              'description': 'Press Enter to submit after pasting (default true).'},
             {'name': 'followup',
              'type': 'string',
              'description': 'Prompt injected after a slash command completes to keep '
                             "the session alive (default 'Continue with what you were "
                             "doing.'). Ignored for plain prompts."},
             {'name': 'delay_ms',
              'type': 'integer',
              'description': 'Wait this many ms before injecting (default 0).'},
             {'name': 'confirm',
              'type': 'boolean',
              'description': 'Required to send destructive commands (/clear, /quit, '
                             '/exit).'}]},
 {'name': 'compact',
  'tool': 'reflection_compact',
  'description': 'Compact your own conversation: injects /compact into your own '
                 'prompt; it queues and runs the instant the current turn ends. A '
                 'resume prompt is then injected AFTER compaction completes (a '
                 'detached watcher waits for it — the resume is never queued behind '
                 '/compact, so it always lands on the freshly-compacted context) so '
                 'the session resumes instead of going idle. Returns immediately with '
                 'followup_deferred=true. Call with no arguments; works the same in a '
                 'terminal session and in a background one.',
  'timeout': 60,
  'params': [{'name': 'followup',
              'type': 'string',
              'description': 'Prompt queued after /compact to resume work (default '
                             "'Continue with what you were doing.')."},
             {'name': 'delay_ms',
              'type': 'integer',
              'description': 'Wait this many ms before injecting (default 0).'}]},
 {'name': 'mode',
  'tool': 'reflection_mode',
  'description': 'Put your own session back into bypass-permissions mode. Approving a '
                 'plan drops the session into whatever mode it was in before planning '
                 "— from a phone that is 'auto', where a classifier gates every "
                 'action. This cycles the permission mode (the Shift+Tab cycle) until '
                 'the session reads as bypass permissions, checking after each step '
                 'rather than assuming a starting point. Already in bypass is a no-op. '
                 'Acts only on your own session.',
  'timeout': 60,
  'params': [{'name': 'expect_session',
              'type': 'string',
              'description': 'Refuse unless the resolved session has this sessionId. '
                             'For hooks, which reach us through an ancestry walk; '
                             'agents never need it.'}]},
 {'name': 'remote_control',
  'tool': 'reflection_remote_control',
  'description': 'Turn Remote Control on or off for a session on this node. On types '
                 '`/rc <name>` and returns the claude.ai link once it connects, or '
                 'link=null with a link_reason. Off types `/rc` and selects '
                 "'Disconnect this session' in the modal it opens; it is refused if "
                 'the session is mid-turn or the modal is not the expected one. Your '
                 'own session cannot answer in the same turn: the command runs when '
                 'the turn ends and one follow-up prompt resumes you, carrying the '
                 'link when turning on.',
  'timeout': 60,
  'params': [{'name': 'on',
              'type': 'boolean',
              'required': True,
              'description': 'true to turn Remote Control on, false to turn it off.'},
             {'name': 'name',
              'type': 'string',
              'description': 'The Remote Control name when turning on; a plain label.'},
             {'name': 'job',
              'type': 'string',
              'description': 'A job id, or a session name as `list_other` shows it. '
                             'Omit to act on your own session.'}]},
 {'name': 'model',
  'tool': 'reflection_model',
  'description': "Switch a session's model: types `/model <model>` and submits it. "
                 'Queues behind a running turn. Refused while a modal or permission '
                 "prompt is showing. Claude Code asks 'Switch model?' when the "
                 'conversation already has output; this verb answers that dialog with '
                 "'Yes, switch to <model>' (it closes it with Escape if the cursor is "
                 'on No, and leaves a dialog a PreModelSwitch hook raised alone). '
                 'Claude Code saves the new value as the default for new sessions; '
                 'this verb puts the saved default back in ~/.claude/settings.json, '
                 'and the running session keeps the new value. On your own session the '
                 'command runs when your turn ends, and exactly one follow-up prompt '
                 'then reports whether the change succeeded or did not apply.',
  'timeout': 60,
  'params': [{'name': 'model',
              'type': 'string',
              'required': True,
              'description': 'A model alias or full name, e.g. opus or sonnet[1m].'},
             {'name': 'job',
              'type': 'string',
              'description': 'A job id, or a session name as `list_other` shows it. '
                             'Omit to act on your own session.'}]},
 {'name': 'effort',
  'tool': 'reflection_effort',
  'description': "Set a session's effort: types `/effort <level>` and submits it. "
                 'Queues behind a running turn. Refused while a modal or permission '
                 "prompt is showing. Claude Code asks 'Change effort level?' when the "
                 'conversation already has output; this verb answers that dialog with '
                 "'Yes, switch to <level>' (it closes it with Escape if the cursor is "
                 'on No, and leaves a dialog a PreModelSwitch hook raised alone). '
                 'Claude Code saves the new level as the default for new sessions; '
                 'this verb puts the saved default back in ~/.claude/settings.json, '
                 'and the running session keeps the new level. On your own session the '
                 'command runs when your turn ends, and exactly one follow-up prompt '
                 'then reports whether the change succeeded or did not apply.',
  'timeout': 60,
  'params': [{'name': 'level',
              'type': 'string',
              'required': True,
              'description': 'low, medium, high, xhigh or max.'},
             {'name': 'job',
              'type': 'string',
              'description': 'A job id, or a session name as `list_other` shows it. '
                             'Omit to act on your own session.'}]},
 {'name': 'rename',
  'tool': 'reflection_rename',
  'description': 'Rename a session: types `/rename <name>` and submits it. Queues '
                 'behind a running turn. Refused while a modal or permission prompt is '
                 'showing. On your own session, one follow-up prompt reports the '
                 'rename (and any other settings change still owed a report) when it '
                 'has run.',
  'timeout': 60,
  'params': [{'name': 'name',
              'type': 'string',
              'required': True,
              'description': 'The new name, one short line.'},
             {'name': 'job',
              'type': 'string',
              'description': 'A job id, or a session name as `list_other` shows it. '
                             'Omit to act on your own session.'}]},
 {'name': 'cd',
  'tool': 'reflection_cd',
  'description': 'Move a session to another directory: types `/cd <path>` and submits '
                 "it. Claude Code then runs the session's tools from there, so later "
                 "Bash calls stay in it, and loads that directory's project settings, "
                 'MCP servers, skills and CLAUDE.md. `list_other` and the Swarm board '
                 'show the session under the scope it moved to. Queues behind a '
                 'running turn. Refused for a path that is not a directory, for one '
                 'Claude Code is not trusted in (the trust dialog would wait on a '
                 'person), and while a modal or permission prompt is showing. A '
                 'worktree of an awm project is trusted first, not refused. Claude '
                 "Code drops the session's MCP servers on a move, so the verb "
                 'reconnects awm after it as `mcp` does; reconnect any other server '
                 'with `mcp`. CLAUDE.md files add up across moves: the old one stays '
                 'in context. On your own session, one follow-up prompt reports the '
                 'move when it has run.',
  'timeout': 60,
  'params': [{'name': 'path',
              'type': 'string',
              'required': True,
              'description': 'An absolute directory on this node.'},
             {'name': 'job',
              'type': 'string',
              'description': 'A job id, or a session name as `list_other` shows it. '
                             'Omit to act on your own session.'}]},
 {'name': 'mcp',
  'tool': 'reflection_mcp',
  'description': "Reconnect, enable or disable a session's MCP servers: types "
                 '`/mcp <action> <server>` and submits it. These forms act without '
                 "opening Claude Code's MCP panel; bare `/mcp` opens it and stays "
                 'refused everywhere. Queues behind a running turn. Refused while a '
                 'modal or permission prompt is showing. On your own session the '
                 'command runs when your turn ends, and one follow-up prompt then '
                 'reports it. Claude Code records what a queued command printed '
                 'only when it was submitted to an idle prompt, so that report '
                 'often cannot quote it. Another session between turns answers '
                 'with the printed result as `output`.',
  'timeout': 60,
  'params': [{'name': 'action',
              'type': 'string',
              'required': True,
              'description': 'reconnect, enable or disable.'},
             {'name': 'server',
              'type': 'string',
              'required': True,
              'description': 'An MCP server name, such as awm, or all.'},
             {'name': 'job',
              'type': 'string',
              'description': 'A job id, or a session name as `list_other` shows it. '
                             'Omit to act on your own session.'}]},
 {'name': 'interrupt',
  'tool': 'reflection_interrupt',
  'description': "Interrupt a session's running turn: presses Escape once, as a person "
                 'would. Refused while a modal or permission prompt is showing.',
  'timeout': 60,
  'params': [{'name': 'job',
              'type': 'string',
              'description': 'A job id, or a session name as `list_other` shows it. '
                             'Omit to act on your own session.'}]},
 {'name': 'stop',
  'tool': 'reflection_stop',
  'description': 'Retire a background session on this node: stop it and remove its '
                 'job (`claude stop`, then `claude rm`). The transcript stays, so the '
                 'session is still resumable by its session id. A retire wakes each '
                 'session that waits on it (`wait`), on any node. Retiring is the last '
                 'step of every finished session: one that ends its work and sits idle '
                 'never wakes its waiters. Works on any session, and on a job whose '
                 'session has already exited, by its job id. Your own session is '
                 'refused unless confirm is true, and is then stopped after this call '
                 'returns. A terminal session is refused.',
  'timeout': 60,
  'params': [{'name': 'job',
              'type': 'string',
              'required': True,
              'description': 'A job id, or a session name as `list_other` shows it.'},
             {'name': 'confirm',
              'type': 'boolean',
              'description': 'Required to stop your own session.'}]},
 {'name': 'link',
  'tool': 'reflection_link',
  'description': "Read a session's claude.ai link and whether Remote Control is on, "
                 'from its record. Types nothing.',
  'timeout': 30,
  'params': [{'name': 'job',
              'type': 'string',
              'description': 'A job id, or a session name as `list_other` shows it. '
                             'Omit to act on your own session.'}]},
 {'name': 'message',
  'tool': 'reflection_message',
  'description': "To talk to another agent, use your harness's own channel first: in "
                 'Claude Code, `SendMessage` to a session that `ListAgents` shows, and '
                 'the reply comes back the same way. Use this verb when the harness '
                 "has no such channel, or to drive another session's prompt, such as a "
                 "slash command. Type text into another session's prompt and submit it "
                 '(no Escape, so it queues behind its current turn). Same guards as '
                 '`message_self`: destructive commands need confirm, modal commands '
                 'are refused, and so is a session showing a modal or permission '
                 'prompt. Every /mcp line is refused: reconnect, enable or disable '
                 'servers with the `mcp` verb. No follow-up is added.',
  'timeout': 60,
  'params': [{'name': 'job',
              'type': 'string',
              'required': True,
              'description': 'A job id, or a session name as `list_other` shows it.'},
             {'name': 'text',
              'type': 'string',
              'required': True,
              'description': 'The line to type.'},
             {'name': 'confirm',
              'type': 'boolean',
              'description': 'Required to send destructive commands (/clear, /quit, '
                             '/exit).'}]},
 {'name': 'pending',
  'tool': 'reflection_pending',
  'description': 'List the deferred resumes this service still owes — one per session '
                 'that has a slash command in flight. A session that compacted and '
                 'then went idle forever is the visible symptom of one of these being '
                 'lost; this is how you tell whether the promise is still being '
                 'watched or vanished with a restart. `lost` lists the newest resumes '
                 'reflection gave up on, each with its reason; each was also posted to '
                 "the session's scope journal. Reports on the whole node, not just the "
                 'caller.',
  'timeout': 30,
  'params': []},
 {'name': 'whoami',
  'tool': 'reflection_whoami',
  'description': 'Report which session reflection resolves you to, and how it would '
                 "reach you (a tmux pane, or a background session's PTY). Useful when "
                 'a reflection call refuses and you want to see why.',
  'timeout': 30,
  'params': []},
 {'name': 'start_other',
  'tool': 'reflection_start_other',
  'description': "Start a named background Claude Code session in a scope's worktree, "
                 'creating the worktree if it is absent, and return its job id and the '
                 '`claude attach` command. Refuses on a station, for a caller that is '
                 'not a domestic peer, when no Claude Code daemon runs, when the '
                 'worktree is untrusted, or when a live session already holds the '
                 'name or the scope. The new session claims its scope, and a stop '
                 'ends the claim. A lobby start claims nothing until its `cd`. '
                 'A worktree of an awm project is trusted first, not refused. '
                 'The session defaults to skip-permissions, opus and high effort. '
                 'Every caller gets the same session, whoever it is.',
  'params': [{'name': 'project',
              'type': 'string',
              'required': False,
              'description': 'The project the scope belongs to.'},
             {'name': 'scope',
              'type': 'string',
              'required': False,
              'description': "The scope whose worktree is the session's directory."},
             {'name': 'prompt',
              'type': 'string',
              'description': 'The first task. Without one the session starts empty.'},
             {'name': 'system_prompt',
              'type': 'string',
              'description': 'Text appended to the system prompt (claude '
                             '--append-system-prompt). Unlike the prompt, a compaction '
                             'never rewrites it.'},
             {'name': 'autocompact',
              'type': 'integer',
              'description': 'The auto-compact window in tokens, 100000 to 1000000. '
                             'Claude Code compacts about 33k below it. Omitted means '
                             "the model's own window."},
             {'name': 'name',
              'type': 'string',
              'description': "The session's name; defaults to the scope."},
             {'name': 'model',
              'type': 'string',
              'description': 'A model alias or full name.'},
             {'name': 'effort',
              'type': 'string',
              'description': 'low, medium, high, xhigh or max.'},
             {'name': 'permission',
              'type': 'string',
              'description': 'A permission mode (plan, acceptEdits, auto, manual, '
                             'dontAsk, bypassPermissions). Omitted means '
                             'skip-permissions. The session starts in this mode and '
                             'can always switch to bypass, so a plan-mode start runs '
                             'in bypass once its plan is approved.'},
             {'name': 'mode',
              'type': 'string',
              'description': 'A label recorded with the session, which a gateway gate '
                             'can key on. Defaults to worker.'},
             {'name': 'disallowed_tools',
              'type': 'array',
              'description': 'Tool names the session may not use.'},
             {'name': 'allowed_tools',
              'type': 'array',
              'description': 'Tool names approved without a prompt, which keeps a '
                             'dontAsk session from having them denied (for example '
                             'mcp__awm__*).'},
             {'name': 'tools',
              'type': 'array',
              'description': 'The only built-in tools the session holds (claude '
                             '--tools). Omitted means all.'},
             {'name': 'restricted',
              'type': 'boolean',
              'description': 'Confine file tools to the worktree and ignore user '
                             'settings (claude --restricted).'},
             {'name': 'strict_mcp',
              'type': 'boolean',
              'description': 'Load only the awm MCP server, from the workspace '
                             'config.'},
             {'name': 'remote_control',
              'type': 'boolean',
              'description': 'Remote Control, on unless false (sessions.md R146), '
                             'named for the session with each `/` as `-`. The '
                             "result's `link` is the claude.ai link, null with a "
                             '`link_reason` if it has not connected within a few '
                             'seconds.'},
             {'name': 'pin',
              'type': 'boolean',
              'description': "Pin the job, so the daemon's idle retirement passes it "
                             'over. A pinned job is a kept session: stopping it unpins '
                             'it and removes it with `claude rm`.'},
             {'name': 'brief',
              'type': 'boolean',
              'description': 'Append the worker lifecycle brief to the system prompt: '
                             'work in the right scope, ask Tony through the board, '
                             'sign off and stop when done. Default true. A pinned '
                             'start never gets it.'},
             {'name': 'started_by',
              'type': 'string',
              'description': 'Who the brief says started the session when no session '
                             "called, such as 'Tony from Discord'. A calling session "
                             'is named instead.'},
             {'name': 'replaces',
              'type': 'string',
              'description': 'The job this session replaces. Its last turns are '
                             'appended to the prompt, it is stopped (and removed when '
                             "pinned) if cx started it, and the new job's lineage "
                             'records it.'},
             {'name': 'tags',
              'type': 'array',
              'description': "Tags to set on the scope (scope tag), such as ['lab', "
                             "'<person>'] for work a lab member asked for. A tagging "
                             'failure is returned as a warning and does not stop the '
                             'start.'},
             {'name': 'task_id',
              'type': 'string',
              'description': 'The board task the session works on (sessions.md).'},
             {'name': 'role',
              'type': 'string',
              'description': 'main or worker; the default is worker when a session '
                             'starts it, else main.'},
             {'name': 'trust',
              'type': 'string',
              'description': "The new session's trust level. Never above the "
                             "caller's."},
             {'name': 'suggest',
              'type': 'boolean',
              'description': 'Start in the lobby, outside every repository, and let '
                             'the session route itself.'},
             {'name': 'isolated',
              'type': 'boolean',
              'description': 'Run the session in a rootless container on this '
                             "node's Claude install, with a login copy that cannot "
                             'refresh, no ssh and only the awm verbs it needs inside '
                             'its own scope. It starts untrusted unless trust says '
                             'otherwise.'}],
  'timeout': 1800},
 {'name': 'list_other',
  'tool': 'reflection_list_other',
  'description': 'List the sessions on this node: background jobs with their state, '
                 'parent, caller and mode, interactive terminal sessions with a `tmux '
                 "attach` command, and the warm pool. Each row's `remote_control` is "
                 'whether Remote Control is on now and `link` is its claude.ai link. '
                 'Read this to see what is running; the pool summary says whether a '
                 'Claude Code daemon runs at all, the binary version the pool seeds '
                 'against and when the reconcile loop last ticked.',
  'params': [{'name': 'project',
              'type': 'string',
              'description': 'Only sessions in this project.'},
             {'name': 'scope',
              'type': 'string',
              'description': 'Only sessions in this scope.'}]},
 {'name': 'stop_other',
  'tool': 'reflection_stop_other',
  'description': 'Retire a session that `start_other` created: stop it and remove its '
                 'job, as `stop` does. A pinned job is unpinned first. The retire wakes '
                 'each session that waits on it. Refuses a job that `start_other` did '
                 'not create.',
  'params': [{'name': 'job',
              'type': 'string',
              'required': True,
              'description': 'The job id `start_other` returned.'}],
  'timeout': 30},
 {'name': 'resume_other',
  'tool': 'reflection_resume_other',
  'description': 'Bring back a waiting job that `start_other` created, under the same '
                 'job id and with its own conversation (`claude respawn`), and re-pin '
                 'it if it was pinned. A retired session, whose job is gone, comes back '
                 'in a new job from its transcript under the same session id '
                 '(sessions.md R10). A resume by hand clears the job\'s wait. Refuses '
                 'a running job, a session whose transcript is gone, and a job that '
                 '`start_other` did not create.',
  'params': [{'name': 'job',
              'type': 'string',
              'required': True,
              'description': 'The job id `start_other` returned, or the session id of '
                             'a retired session.'},
             {'name': 'prompt',
              'type': 'string',
              'description': 'The first message the resumed session gets.'}],
  'timeout': 90},
 {'name': 'adopt_other',
  'tool': 'reflection_adopt_other',
  'surfaces': ['cli', 'http'],
  'description': 'Give a job that cx did not start a lineage record, so `resume_other` '
                 'and `stop_other` accept it. For a session started by hand that a '
                 'keeper now keeps.',
  'params': [{'name': 'job',
              'type': 'string',
              'required': True,
              'description': 'The job id.'}],
  'timeout': 30},
 {'name': 'harness_event',
  'tool': 'reflection_harness_event',
  'description': 'A harness plugin reports how a command with a follow-up ended, done '
                 'or lost, or that a session cleared itself, clear (harness-contract.md '
                 '§ Events).',
  'params': [{'name': 'session_id', 'type': 'string', 'required': True},
             {'name': 'kind',
              'type': 'string',
              'required': True,
              'description': 'done, lost or clear'},
             {'name': 'parent',
              'type': 'string',
              'description': 'With clear: the session id from before the clear.'},
             {'name': 'ticket', 'type': 'string'},
             {'name': 'command', 'type': 'string'},
             {'name': 'followup', 'type': 'string'},
             {'name': 'reason', 'type': 'string'},
             {'name': 'name', 'type': 'string'},
             {'name': 'pid', 'type': 'integer'},
             {'name': 'cwd', 'type': 'string'},
             {'name': 'told_session', 'type': 'boolean'},
             {'name': 'injected_at_ms', 'type': 'integer'},
             {'name': 'reissues', 'type': 'integer'},
             {'name': 'at_ms', 'type': 'integer'}],
  'surfaces': ['cli', 'http'],
  'timeout': 30},
 {'name': 'session_of',
  'tool': 'reflection_session_of',
  'description': 'The session that a process on this node belongs to, for a service '
                 'that holds a stamped caller pid. Asks every harness plugin.',
  'params': [{'name': 'pid', 'type': 'integer', 'required': True}],
  'surfaces': ['cli', 'http'],
  'timeout': 30},
 {'name': 'wait',
  'tool': 'reflection_wait',
  'timeout': 60,
  'description': 'Pause your own session until every session in `until` has retired, '
                 'then wake it with a line that says so. A wait works like a retire, in '
                 'place of `stop`: compact first, then call it as the last step of a '
                 'turn, after the sessions you hand work to have started. A background '
                 'session stops and keeps its job, and the wake resumes it. A terminal '
                 'session stays open and gets the line typed in. A listed session that '
                 'itself waits has not finished, so nested waits hold. The wait '
                 'clears when you wake, are resumed by hand, or retire.',
  'params': [{'name': 'until',
              'type': 'array',
              'required': True,
              'description': 'The session ids (R9, as start_other returns them) to '
                             'wait for.'}]},
 {'name': 'waits',
  'tool': 'reflection_waits',
  'surfaces': ['cli', 'http'],
  'description': 'Every recorded wait: who waits, on whom, which of them have '
                 'retired, since when, and in which scope.',
  'params': []},
 {'name': 'retired',
  'tool': 'reflection_retired',
  'surfaces': ['cli', 'http'],
  'description': 'A peer tells this node that a session retired there, so a wait here '
                 'that lists it can wake its waiter.',
  'params': [{'name': 'session_id', 'type': 'string', 'required': True,
              'description': 'The R9 id of the session that retired.'}]},
 {'name': 'retired_among',
  'tool': 'reflection_retired_among',
  'surfaces': ['cli', 'http'],
  'description': 'Which of these sessions retired on this node, and which this node '
                 'still knows as live or stopped.',
  'params': [{'name': 'session_ids', 'type': 'array', 'required': True,
              'description': 'R9 session ids.'}]}]
