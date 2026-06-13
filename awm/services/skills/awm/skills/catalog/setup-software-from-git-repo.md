---
name: setup-software-from-git-repo
type: protocol
tags: [setup, bootstrap, external, github, library, clone, install]
requires: [mamba]
description: Bootstrap a third-party tool from a GitHub repo as a scope library
---

# Setup Software from a Git Repo

Use when the user asks to clone, install, or set up an external GitHub repo as a library in the current scope. This skill covers the **faithful bootstrap** only — getting the tool running per upstream documentation. It does NOT cover fixing upstream bugs, wiring the tool into the local workflow, or registering it globally. Those are follow-up tasks.

## 1. Orient — read the repo before touching it

Before cloning (or immediately after, without installing anything), build a mental model of what the tool is and how upstream expects it to run. Check:

- `README.md` / `README.rst` — what does the tool do? is there a quickstart?
- `pyproject.toml` / `package.json` / `Cargo.toml` / `go.mod` / `environment.yml` — language, declared deps, required runtime version
- `Dockerfile` / `docker-compose.yml` — does it need containerized services?
- `.env.example` / `.env.sample` / `config/*.example` — required config or secrets
- `INSTALL.md` / `CONTRIBUTING.md` / `docs/install*` — authoritative setup steps
- `Makefile` / `justfile` / `scripts/` — canonical commands upstream provides
- `LICENSE` — flag if restrictive (AGPL, commercial, non-OSS)

Exit this phase with answers to: (a) what is the tool, (b) what runtime does it need, (c) what install path do upstream docs prescribe, (d) how will you know it's working.

## 2. Clone to `.awm/data/lib/`

```bash
mkdir -p .awm/data/lib
git clone <url> .awm/data/lib/<repo-name>
```

Why `.awm/data/lib/` rather than the scope root:

- Keeps third-party code out of the scope's git history
- Lives with project data, so it's cleaned up appropriately with the scope
- Consistent with the symlink layout documented in `.awm/context.md`

If the user wants reproducibility, pin to a specific tag or commit immediately after cloning:

```bash
cd .awm/data/lib/<repo-name> && git checkout <tag-or-sha>
```

## 3. Install dependencies — defer to language-specific skills

Identify the language from the orientation phase, then follow the corresponding reference skill rather than re-inventing install steps here:

| Language | Skill to follow | Notes |
|---|---|---|
| Python | `tools/mamba.md` | **Prefer conda/mamba over uv/pip/poetry** unless upstream explicitly requires otherwise. Create a dedicated env named after the tool, or add to an existing project env via overlay. |
| Node | (no skill yet) | Use `npm ci` / `pnpm install --frozen-lockfile` if a lockfile exists; otherwise `npm install`. |
| Rust | (no skill yet) | `cargo build --release`. |
| Go | (no skill yet) | `go mod download && go build ./...`. |
| Other | — | Follow upstream docs verbatim. |

For Python specifically: if the repo's docs say "use uv" or "use poetry," note the deviation and proceed with conda/mamba only if it can reasonably substitute. If it can't (e.g., the repo ships a lockfile only uv understands, or a build system that requires poetry), follow upstream and report why.

## 4. Bring up runtime infrastructure

If the repo requires services (Docker containers, a database, a daemon), bring them up using upstream's documented commands. Do **not** generalize — follow the README / Makefile / compose-file instructions literally.

Copy `.env.example` to `.env` if present. Fill in only the minimum keys required to reach the smoke test; leave optional keys unset.

## 5. Verify with a minimal smoke test

Use whatever upstream documents as a "hello world" or health check:

- A CLI `--help` or `--version`
- The repo's quickstart example
- A `docker ps` / port check
- A tiny end-to-end call

Record the exact command(s) that proved it works. You'll surface these in the report-back.

## 6. Document issues — do NOT fix them

If the setup hits bugs (hardcoded paths, missing deps in manifest, broken fallbacks, version mismatches, etc.):

- **Do not patch upstream source** during this task. Patching upstream code is a separate, deliberate task with its own accountability (fork, branch, PR). Fixing mid-setup mixes concerns and produces untracked local edits.
- **Record** each issue clearly in the report-back summary:
  - What broke
  - Exact error message
  - Minimum workaround that got past it — e.g. `pip install <missing-dep>` as a one-off is acceptable; editing source files is not

If a bug is fully blocking and no workaround exists, stop, document the blocker, and surface it to the user. Do not start debugging the upstream codebase inside a setup task.

## 7. Do NOT wire up globally

Unless the user explicitly asks for integration:

- Do NOT add entries to parent `.mcp.json`, shell rc files, `PATH`, systemd units, cron, etc.
- If the tool has an integration point (MCP config, CLI alias, launcher script), it's fine to create the file **inside the scope directory** as an artifact, but leave it unreferenced from any parent config.
- Integration is a follow-up task — tell the user the integration file exists and where it is, and wait for explicit instructions before hooking it up.

## 8. Report back

When setup is complete (or blocked), summarize for the user:

- Where the repo is cloned (absolute path)
- How it was installed (env name, language toolchain used, any deviations from upstream docs)
- What runtime infrastructure was brought up (container names, ports, services)
- The exact smoke-test command(s) that verified it works
- Any bugs, workarounds, or deviations encountered (for later triage)
- Any integration files created but left unwired (with full paths)

Debriefing (session logging, artifact registration) is a separate skill the user triggers explicitly — do **not** run it as part of this protocol.
