---
name: goal-alignment
description: Interview the user for the terminal objective behind the work — what they want at the end, what counts as done — and record it as a journal post in the scope. Trigger on "goal-alignment", or on setting, revising, or asking after the goal.
---

# goal-alignment

Elicit the user's terminal objective and write it down where the next session reads it. This is a conversation, not a form: ask few questions, in their language, and stop asking once you can restate what they want in a sentence they would sign.

A goal is an ordinary journal post in its scope, tagged `journal`, with a title that starts `Goal:`. No goal store exists. Tony's standing frame for every session lives in `~/.claude/CLAUDE.md` § The standing frame, not in a post.

## Protocol

**1. Read what is already in force. Always first, before any question.**

Run `journal fetch {project, scope, query:"Goal:"}` and read every hit whose title starts `Goal:` and that does not carry the `superseded` or `closed` tag. Read `~/.claude/CLAUDE.md` § The standing frame too. Keep the id and text of each goal — you need them in step 3. Do not skip this because "the user is clearly stating something new" — that judgement is step 3's, and it needs the list.

**2. Interview.**

Ask about, roughly in this order, and only for what is not already clear:

- **The terminal objective** — what they are actually after *behind* this task. Push one level past the request: if they say "make the tests pass", ask what passing tests buys them. Stop pushing when the next "why" would be philosophy.
- **Definition of success / acceptance criteria** — what has to be observably true for them to call it done.
- **Progress** — what tells them it is moving, when it is not finished yet.
- **Fallback** — what you should do when the ideal turns out to be unreachable. What is the acceptable partial?
- **Stop line** — what separates *incomplete* (fine, just say so) from *wrong* (stop and surface it).
- **Noise** — what genuinely does not matter here; caveats not worth raising.

Keep the user's own words. A goal in your phrasing is a goal they will not recognise later. Ask in one or two batched rounds, not one question per turn.

**3. Duplicate / close-relative check. Mandatory, before anything is written.**

Compare the objective you just elicited against every goal from step 1 — same intent restated, a narrower slice of an existing goal, or the same goal at a different altitude all count as close relatives, not just word overlap. If anything is close, **put it to the user before recording** — quote the existing goal and its id, and ask which it is:

- **a revision of that goal** → post the new goal with `reply_to:[<old id>]`, then run `journal retag {post_id:<old id>, add:["superseded"]}`;
- **genuinely separate** → a new post, and say plainly in it how it differs from the neighbour;
- **that goal is dead** → `journal retag {post_id:<old id>, add:["closed"]}`, then record the new one.

Never add a second overlapping goal on your own judgement. Two half-true goals in force is worse than none — the next session reads both and picks wrong.

**4. Record.**

    journal(verb="post", args={project:"<project>", scope:"<scope>", author:"user:<name>",
      tags:["journal"], meta:{title:"Goal: <the objective in a few words>"},
      body:"<objective>\n\nDone when: …\nFallback: …\nStop line: …\nNoise: …"})

- `author` is `user:<name>` — a goal is the user's statement, not yours.
- Pass `body` last.
- Write acceptance criteria and progress signals into the body. Concrete and checkable; this is the text a future session holds its work against.
- A goal that reaches past the scope belongs in the project's `AGENTS.md`, or for every session in `~/.claude/CLAUDE.md` § The standing frame. Edit those only when the user asks.
- Record a partial goal rather than interrogating the user into a complete form, then offer to fill the rest.

**5. Confirm.** Show the user the recorded goal and its post id, note what is still missing, and stop. Do not roll straight into executing the goal unless they ask.

## Notes

- The thread under a goal is its history: each revision replies to the one before. Read it with `journal fetch {post_id}` when the user asks how a goal drifted.
- Nothing is ever deleted. Superseded and closed goals stay readable; that is what makes it safe to write a revision rather than argue about the old wording.
- If the user is only *asking* what the goal is, step 1 alone answers them. Do not run the interview to produce a post nobody asked for.
