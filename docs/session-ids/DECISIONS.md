# Session ids and names: decisions

## ids-harmonize (2026-11-10)

**Question.** How should awm identify a session, and how should a caller look one up?

**Your words.**
> one lookup function should accept the canonical ID only. and this should be obvious what is canonical. ID.canonical()? ID.harness_native()?
> the lookup can also do name, but it shouldnt accept different types of hte same ID because that's dangerous.
> do the same translation trick for /rc, but trillium can use the real name

**Design.**
- `SessionId` is one type. `id.canonical()` gives `cc` + 32 hex, which is the only form awm stores and accepts from a caller. Agents see it. Tony never does: every human surface (the board, the Control panel, Discord) shows the name.
- Each consumer gets a named converter. `id.harness_native()` gives Claude Code's dashed UUID. `id.job_dir()` gives the 8-hex short id, used only as a path segment under `~/.claude/jobs/`.
- `SessionId::parse` accepts the canonical form only. `SessionId::from_harness_native` is called only where awm reads Claude Code's own files. Nothing accepts a short id or a prefix.
- One resolver takes a typed argument, `{id}` or `{name}`, and never sniffs a string. It searches live sessions and stopped jobs. An ambiguous name is an error that lists the canonical ids. A short id is refused with a pointer to the canonical id. The other five resolvers are deleted.
- A name is display text. `name.display()` feeds Trilium and `/rename`. `name.rc()` maps characters outside `[A-Za-z0-9._-]` to `-` and truncates to 64, for `/rc`. Nothing else parses a name.
- `@node` leaves `to`. The node goes in the existing `peer` key, and `split_to` is deleted. The board keeps its separate node chip.
- The board links a session to its card by the row's `task_id`, not by a hidden id label. `cardTitles` stops matching session names with `TASK_NAME`.

**Landed.** 7f63d36d (types and resolver), 8a82a31e (board by `task_id`), d16730e4 (simplify). Live in release ce40bb82.

## resume-short-id (2026-11-10)

**Question.** Why does `resume_other` fail on a stopped job's short id?

**Design.** `cch find` searches only the live roster, which drops stopped jobs. The ids-harmonize resolver searches stopped jobs too, and callers pass the canonical id, so the failure goes away. No separate fix.

**Landed.** Covered by ids-harmonize. Checked live: a retired session resumes by canonical id, and a short id is refused.

## name-is-name (2026-11-10)

**Question.** What rules does a name keep, how does a lookup match one, and what stops the pool from reading names as state?

**Your words.**
> for names we can do a softer search, but with each case not partial matches.
> can we fix the name based matching by the session pool system as well?

**Design.**
- Start and rename keep the 1–80 printable-character rule, because `/rename` is typed into the terminal. The leading `-` rule goes: the name is passed as `--name=<name>`.
- A `{name}` lookup matches the whole current name, ignoring case. It never matches part of a name, and it never matches `seed_name`. If more than one session matches, the lookup errors and lists their canonical ids.
- The pool keeps a ledger keyed by canonical id. It writes `{id: spare}` once the launch finds the new job by its name. `claude --bg` ignores `--session-id` and picks the id itself (found live, 2026-11-10). `is_ours` reads "spare" from the ledger, `was_ours` reads "id is in the ledger", and a claim flips the entry to `claimed`. An entry with no session behind it is safe to drop.
- Telemetry rows carry `pool: spare`, and telemetry and the board filter on that field.
- Deleted: `name_prefix`, `AWM_CX_PREFIX`, `WARM_PREFIX`, the `<warm` name ban, and the noun-stripping in `seed.rs` and `claim.rs`.

**Landed.** 8a82a31e (ledger), 2a791193 (find the launched job by name). Live in release 0f1fcda9.

## stored-keys (2026-11-10)

**Question.** How do the stores and verbs that key on a short id or a name move to the canonical id?

**Your words.**
> migration, do it once and cut over services live
> agent facing sure. but I will not know or want to deal with uuids

**Design.**
- Every verb argument and output named `job` becomes `session_id`, holding the canonical id. Only reflection's `to: {id}|{name}` says `id`. This covers reflection, cch, routine, operations, gateway claims, seat events and the telemetry fallback. `list_other` rows show the canonical id.
- Where a record holds both the canonical id and a short `job`, the `job` field is dropped: trust, waits, `channels.job`, operations `sessions.job` and its `rc:<node>:<job>` relay keys, `scope_waiters.job`.
- Where the short id is the only key, a one-shot migration maps it to the canonical id through `~/.claude/jobs/<short>/state.json`, then the lineage file. It drops rows that map to no job. This covers seats (`seat_registry`, `seat_lines`), social (mounts, keepers, mirrored seat tables), the wake standby hold, and lineage filenames.
- The migration runs once at deploy, and the services cut over live. Its code then leaves the tree.
- cch's pending record waits under a random token, not a digest of the name. The launch finds the job by its name, and the lineage record is filed under the job's own session id. A late job is adopted by its name.
- Operations asks the seats registry whether a session holds a seat. `SEAT_PREFIX` is deleted, and the `seat:` text is display only.

**Landed.** 710c5961 (no `job` on the wire or in stores), 2a791193 (pending record by token). Migrations ran in release ce40bb82 and left the tree in ae9a51b4 (release 3717557f), social's last once mira retired.
