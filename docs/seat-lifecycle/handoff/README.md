# handoff

## Split with plugin-redux (2026-11-06)

plugin-redux/interview takes DESIGN.md seat items 1-8 into the Rust social port. Its comms worker builds them: plugin-redux/comms, job e44d901a, scope awm/port-comms. keeper.py goes.

This session keeps the seat side: the `agentic-commons` project and its `seats/<slug>` scopes, the SOP and server-brief text, retiring the `opal-seat-holder` skill, and the config values (sent 2026-11-06). Discord `/new-agent` lived in the reporting service, which the redux already removed. social's Discord connector registers only `/approve`, so nothing is left to remove in code.

Brief paths given to the port: `docs/seat-sop.md` and `docs/briefs/<platform>-<server-slug>.md` in the agentic-commons checkout.
