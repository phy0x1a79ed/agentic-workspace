---
name: dfs-interview
description: Run a long discussion or complex user-interactive task as a depth-first walk over a written stack of topics. Load it only when asked by the user, including when a compaction followup names it. Do not load it for any other request.
---

# dfs-interview

## Purpose & Contents

This skill is the protocol for a long discussion that spans several related topics. The discussion is a depth-first walk over a stack of topics. The walk takes the top topic to its end, records its decision, and pops the next topic. Each new issue from the user goes onto the stack, so the current topic runs to its end and every issue gets its turn.

Two facts shape the protocol. The user holds less context than the agent, so each message carries one topic. Compaction erases the agent's memory, so two files hold the state. `STACK.md` holds the open state. `DECISIONS.md` holds each closed topic's decision, where the user reads them in one place. One journal post at the end of the discussion points `search` at them.

This skill covers `STACK.md`, `DECISIONS.md`, the walk, new topics, workers, the end of the discussion, and the resume after a compaction.

## Keep STACK.md

Put `STACK.md` at `docs/<slug>/STACK.md` in the scope's repo, with a short slug for the subject. Edit it directly. It has these sections, in this order:

1. **Header.** The goal of the discussion, and the path of its `DECISIONS.md`.
2. **Current.** The current topic.
3. **Waiting on the user.** Each question or step that waits on the user, one line each.
4. **Preferences.** Each preference the user sets for this discussion, with its date.
5. **Stack.** The open topics, top first. A child topic sits indented under its parent. Each topic is a slug and a short phrase from the user. Add a line of state when the topic has some.
6. **Workers.** Each live worker, with its job id, how to reach it, and its task.
7. **Closed.** Each closed topic, one line each. Its decision is in `DECISIONS.md`.

The top topic is the first topic in the file with no open child topic.

Write `STACK.md` only at three points: when a topic closes, when a worker starts or reports, and right before a compaction. Commit it at each of these points.

## Record a decision

Put `DECISIONS.md` beside `STACK.md`. Give it one section per closed topic, in the order the topics close. Head each section with the topic's slug and date. Each section holds:

- the question, in a sentence
- the user's words, quoted only where they are the decision
- the decided design
- where it landed: the commit, the worker, or the spec item

Write a topic's section when the topic closes. Commit `DECISIONS.md` with `STACK.md`. When a later topic changes a decision, rewrite that decision's section. Name the later topic in it. Do not post a journal per topic. Do not write a file per topic. A decision that holds beyond the discussion also goes to `precedent`, only when the user says so.

## Work the stack

1. Take the top topic. Ask only its own questions.
2. Dispatch read-only subagents to survey what already exists for the topic. Keep only their conclusions.
3. Ask the user about each difference between the survey and the user's description that has an unclear answer.
4. Put one question per message, or a small set of closely related questions. Give your recommendation with it.
5. Match each answer to the message it replies to. Ask when a reply could answer more than one message.
6. Close the topic when its questions are answered. Write its section in `DECISIONS.md`.
7. Move the topic to **Closed** in `STACK.md`.
8. Commit both files.
9. Present the next topic in the same turn.

Compact only after a long topic. Before you compact, write the open topic's state into `STACK.md`. Commit it. Run `reflection(verb="compact", args={followup:...})` with a followup that invokes `dfs-interview` by name and names the `STACK.md` path.

## Push new topics

- Put each new issue from the user on the stack, as a slug and a short phrase.
- Tell the user where it went, in one line.
- Keep working the current topic until the user switches.
- Nest a new topic under the current topic when it belongs there. Ask the user when the placement is unclear.
- Put a topic last when the user says it runs later.

## Dispatch workers

Use workers when the discussion drives a build. Decide one task at a time. Do not plan the whole build before the first worker starts.

1. Close a topic when its design settles into a task that one worker can do.
2. Write the worker's brief from the decision: the user's words, the design, the rules and how to report.
3. Start the worker as a top-level session with `reflection(verb="start_other")`. Pick a smaller model for straightforward work.
4. Push each issue a worker sends onto the stack. Tell the user where it went.
5. Tell the worker when its issue is solved, with the commit or the check that proves it.

Keep one contact with the user: this session. A worker talks with the user only when the user asks for it.

## Stay in the tree

- Go top down. Ask about another subtree only when the walk reaches it.
- Ask only questions that trace to the user's words or to a requirement the user set.
- Keep the design simple. Add a feature only when a topic needs it.
- Quote the user from their message as it stands. Never reprint the conversation into the chat.

## End the discussion

The discussion ends when the stack is empty.

1. Reread `DECISIONS.md`. Confirm that no section contradicts a later one.
2. Commit `STACK.md` and `DECISIONS.md`.
3. Post one journal with `journal(verb="post")` in the discussion's scope, with the tags `decision` and `closed`. Give it one line per decision, the path of `DECISIONS.md` and the commit.

## Resume after a compaction

1. Read `STACK.md`.
2. Check each worker against what landed. Tell each worker whose issue is solved.
3. Ask the top question of the current topic.

Read `DECISIONS.md` only when a question needs it.
