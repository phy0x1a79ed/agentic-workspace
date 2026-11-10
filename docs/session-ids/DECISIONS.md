# Session ids and names: decisions

## ids-harmonize (2026-11-10)

**Question.** How should awm identify a session, and how should a caller look one up?

**Your words.**
> one lookup function should accept the canonical ID only. and this should be obvious what is canonical. ID.canonical()? ID.harness_native()?
> the lookup can also do name, but it shouldnt accept different types of hte same ID because that's dangerous.
> do the same translation trick for /rc, but trillium can use the real name

**Design.**
- `SessionId` is one type. `id.canonical()` gives `cc` + 32 hex, which is the only form awm stores, displays, and accepts from a caller.
- Each consumer gets a named converter. `id.harness_native()` gives Claude Code's dashed UUID. `id.job_dir()` gives the 8-hex short id, used only as a path segment under `~/.claude/jobs/`.
- `SessionId::parse` accepts the canonical form only. `SessionId::from_harness_native` is called only where awm reads Claude Code's own files. Nothing accepts a short id or a prefix.
- One resolver takes a typed argument, `{id}` or `{name}`, and never sniffs a string. It searches live sessions and stopped jobs. An ambiguous name is an error that lists the canonical ids. A short id is refused with a pointer to the canonical id. The other five resolvers are deleted.
- A name is display text. `name.display()` feeds Trilium and `/rename`. `name.rc()` maps characters outside `[A-Za-z0-9._-]` to `-` and truncates to 64, for `/rc`. Nothing else parses a name.
- `@node` leaves `to`. The node goes in the existing `peer` key, and `split_to` is deleted. The board keeps its separate node chip.
- A Trilium card stores its session's canonical id as a label. `cardTitles` stops matching session names with `TASK_NAME`.

**Landed.** Spec only. No code yet.

## resume-short-id (2026-11-10)

**Question.** Why does `resume_other` fail on a stopped job's short id?

**Design.** `cch find` searches only the live roster, which drops stopped jobs. The ids-harmonize resolver searches stopped jobs too, and callers pass the canonical id, so the failure goes away. No separate fix.

**Landed.** Covered by ids-harmonize.

## name-is-name (2026-11-10)

**Question.** What rules does a name keep, how does a lookup match one, and what stops the pool from reading names as state?

**Your words.**
> for names we can do a softer search, but with each case not partial matches.
> can we fix the name based matching by the session pool system as well?

**Design.**
- Start and rename keep the 1–80 printable-character rule, because `/rename` is typed into the terminal. The leading `-` rule goes: the name is passed as `--name=<name>`.
- A `{name}` lookup matches the whole current name, ignoring case. It never matches part of a name, and it never matches `seed_name`. If more than one session matches, the lookup errors and lists their canonical ids.
- The pool keeps a ledger keyed by canonical id. It writes `{id: spare}` before it launches a spare with `--session-id`. `is_ours` reads "spare" from the ledger, `was_ours` reads "id is in the ledger", and a claim flips the entry to `claimed`. An entry with no session behind it is safe to drop.
- Telemetry rows carry `pool: spare`, and telemetry and the board filter on that field.
- Deleted: `name_prefix`, `AWM_CX_PREFIX`, `WARM_PREFIX`, the `<warm` name ban, and the noun-stripping in `seed.rs` and `claim.rs`.

**Landed.** Spec only.
