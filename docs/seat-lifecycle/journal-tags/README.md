# journal-tags

## Tony's words

> Seats should also be instructed to journal often -> we should also add tags for each user, and one that marks seats. otherwise, these journals are debreif journals.

## What exists today (survey, 2026-11-06)

- Journal tags come from one vocabulary that every node shares. `tag_add` adds a name. Names allow lowercase letters, digits and `_ . : + / -`. The vocabulary has no `seat` tag and no per-person tag.
- A post with `journal` is a scope's debrief. `fetch` filters by tags joined with `and`, such as `seat and user:steven-chen`.
- Seat agents today journal only at debrief or handoff. Rajan's scope holds 10 journals over two weeks of work.

## Decision

Tony, 2026-11-06, to the tag scheme and the journal frequency: "yes to both".

1. Every seat journal carries three tags: `journal`, `seat` and `user:<person-slug>`.
2. The new-seat routine adds `user:<person-slug>` to the vocabulary on a person's first contact.
3. A seat agent journals after each result or decision it sends in the DM, and once at retire.
4. `fetch` with `seat and user:<person-slug>` returns one person's seat history across scopes.
