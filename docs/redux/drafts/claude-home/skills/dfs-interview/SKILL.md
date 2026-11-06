---
name: dfs-interview
description: Run a long discussion or complex user-interactive task as a depth-first walk over a written stack of topics. Load it only when asked by the user, including when a compaction followup names it. Do not load it for any other request.
---

# dfs-interview

## Purpose & Contents

This skill is the protocol for a long discussion or complex user-interactive task that spans several topics with complex relationships. The discussion is a depth-first walk over a stack of topics. The walk takes the top topic to its end, records its result, and pops the next topic. Each new issue from the user goes onto the stack, so the current topic runs to its end and every issue gets its turn.

The protocol rests on two facts. The user holds less context than the agent, so each message carries one topic. Compaction erases the agent's memory of the discussion, so a discussion folder in the repo holds the whole state. In that folder, `STACK.md` holds the open topics, `PREFERENCES.md` holds the user's preferences for the discussion, and a tree of topic directories holds the results. Two scripts in the skill folder keep these files consistent.

This skill covers the folder layout, `STACK.md`, the tools, the walk, new topics, the scope of questions, and the resume after a compaction.

## Lay out the folder

1. Load the `write-docs` skill. Follow it for each topic result.
2. Pick a short slug for the subject of the discussion.
3. Run `stack.py docs/<slug> init --purpose "<the goal of the discussion>"` in the scope's repo.
4. Commit the folder before the first question.

The folder holds these files:

- `STACK.md` holds the open topics.
- `PREFERENCES.md` holds the preferences the user sets for this discussion, each with its date.
- Each topic has a directory named with a short slug for the topic, such as `end-state`.
- A child topic's directory sits inside its parent's directory, such as `end-state/scopes`. The directory tree mirrors the topic tree.
- Each topic directory holds a `README.md` with the topic's result.

For example:

    docs/fleet-network/
      STACK.md
      PREFERENCES.md
      end-state/
        README.md
        scopes/README.md
        session-lifecycle/README.md
      install/README.md

## Write STACK.md

`STACK.md` has these sections, in this order:

1. **Purpose.** The goal of the discussion and the procedure from § Work the stack. `init` writes both.
2. **Current topic.** The current topic and its parent, in one line.
3. **Stack.** The open topics, top first. Each topic carries its slug, its date and the user's words. A child topic sits indented under its parent. Each topic's question queue sits under it.
4. **Parked.** The inputs that predate the stack or that the user set aside. Each one enters the stack when the walk reaches it.
5. **Closed.** Each closed topic, with its date and the path of its result.

The top topic is the first topic in the file with no open child topic.

Keep the stack flat for a list of independent topics, such as a review list. Write each outcome on the line below its topic, in place of a topic directory.

## Use the tools

The skill folder holds two scripts. Run each one as `mamba run -n awm python ~/.claude/skills/dfs-interview/<script>`. This skill writes `stack.py` and `quote.py` for that command.

- `quote.py` prints the user's messages from the current session's transcript. Filter them with `--grep` and `--since`. Copy each quote from its output.
- `stack.py <folder> <verb>` edits the discussion folder. Its `--help` holds the format of `STACK.md`.

Edit the Stack, Current topic and Closed sections only through `stack.py`. The `stack.py` verbs are:

- `init` creates the folder, `STACK.md` and `PREFERENCES.md`.
- `push` puts a new topic on top of the stack. `--under` nests it under a parent. `--bottom` puts it last.
- `ask` queues a question under a topic.
- `answer` ticks a queued question.
- `current` makes a topic current and creates its directory and `README.md`.
- `close` moves a topic to **Closed** and makes the next top topic current. It refuses a topic with open questions or open child topics.
- `prefer` records a preference in `PREFERENCES.md`.
- `show` prints the current topic, the top topic and the open topics.

## Work the stack

1. Run `stack.py <folder> current` for the top topic. Ask only its own questions.
2. Dispatch read-only subagents to survey what already exists for the topic.
3. Record each difference between the survey and the user's description.
4. Ask the user about each difference with an unclear answer.
5. Put one question per message to the user, or a small set of closely related questions. Give your recommendation with it.
6. Queue the other questions with `stack.py <folder> ask`.
7. Match each answer to the message it replies to. Ask when a reply could answer more than one message.
8. Record each answer in the topic's `README.md`. Copy the user's words from `quote.py`.
9. Write the result as the current design.
10. Tick each answered question with `stack.py <folder> answer`.
11. Run `stack.py <folder> close` when the topic's question queue is empty.
12. Commit.
13. Compact only when the closed topic ran long, with many exchanges or large reads. After a short topic, present the next topic in the same turn.
14. To compact, run `reflection(verb="compact", args={followup:...})`. Write a followup that invokes `dfs-interview` by name. Name the discussion folder, the closed topic, its commit and the new top topic in it.

## Push new topics

- Push each new issue from the user onto the top of the stack with `stack.py <folder> push`. Copy the user's words from `quote.py`.
- Tell the user where the new topic went.
- Keep working the current topic until the user switches.
- Nest a new topic under the current topic with `--under` when it belongs there. Ask the user when the placement is unclear.
- Put a topic last with `--bottom` when the user says it runs later. Tell the user about the exception, with its reason.
- Move a question that depends on an undiscussed topic into that topic's queue.
- Push a finished background result, such as a subagent survey, under the topic it serves. Keep working the current topic.

## Stay in the tree

- Go top down. Ask about another subtree only when the walk reaches it.
- Ask only questions that trace to the user's words or to a requirement the user set. Keep survey findings inside the current topic.
- Keep the design simple. Add a feature only when a topic needs it.
- Record each preference the user sets for this discussion with `stack.py <folder> prefer`.
- Post a decision as a precedent (`journal post` with `tags: ["precedent"]`) only when the user says it holds beyond this discussion. Keep every other decision in the discussion folder.

## Resume after a compaction

1. Run `stack.py <folder> show`.
2. Read `PREFERENCES.md`.
3. Read the `README.md` of the current topic and of its parent.
4. Ask the top question of the current topic's queue.

Read other files only when a question needs them.
