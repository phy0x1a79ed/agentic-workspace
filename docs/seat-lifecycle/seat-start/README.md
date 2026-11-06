# seat-start

## Tony's words

> how to seat starts work? do they go through the reflection, cch, etc and use the warm seats? so they start fast?

## What exists today (survey, 2026-11-06)

- A seat agent starts through `reflection start_other`. reflection forwards to cch `start` (`services/cch/awm/cch/lifecycle.py:226`), and cch asks the Claude daemon for a background job.
- The daemon hands over a pre-started warm spare instead of booting a new process. Its log shows `bg claimed-spare <job>` for every seat started today (e831ba99, 72d5a63f, 9e22f611, a1406294).
- Patrik's agent (a1406294): spare claimed 21:27:37.151Z, brief delivered 21:27:38.058Z, first tool call 21:27:44.108Z. That is about 7 s from start to work.
- The new-seat routine calls the same `start_other`, so it uses the same warm spares. Its extra cost is the event hop from social and one script run, a few seconds.
