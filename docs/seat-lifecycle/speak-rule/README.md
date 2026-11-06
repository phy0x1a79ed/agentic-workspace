# speak-rule

## Tony's words

> steven chen contacted the client without first being asked, on a stale request.

Rule given to this session: "Tony speaks first to lab members, never a seat on its own initiative."

## What exists today (survey, 2026-11-06)

- Redux `users.md` R126 item 1, decided 2026-11-02: "Opal never messages a person first. The Slack send refuses a DM where the person has never written, in code." The send code on altair has no such check (`awm/social/hub_adapter.py` send handler).
- Precedence d6654d87 (2026-10-30): never message a lab member first. Once they engage, report progress and results proactively.
- The 2026-11-06 incident had two kinds of unasked speech:
  1. Restarted agents answered backlog messages (Steven 18:41Z, Kalen 18:46Z). The lifecycle decision removes this: only a new message starts an agent.
  2. Kalen's agent also sent "the follow-up I promised" on his colonies from 10-30, a thread he had not raised again. Precedence d6654d87 allows that reading ("report progress and results proactively").
