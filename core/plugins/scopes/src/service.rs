//! The `scope` tool: every verb with its Python description, params, timeout and surfaces,
//! and the handlers that read their arguments as the Python handlers did.

use std::sync::Arc;
use std::time::Duration;

use awm_plugin_sdk::{privacy, Call, Service, VerbError};
use serde_json::{json, Value};

use crate::args::{flag, get_or, int_or, opt_str, py_str, req_str, required, str_list, str_or_none};
use crate::lifecycle::CreateReq;
use crate::{db, App, Fail, Op};

pub const TOOL: &str = "scope";

/// A param as describe lists it: the type only when it is not a string, the description
/// only when it has one.
fn p(name: &str, ty: &str, required: bool, description: &str) -> Value {
    let mut v = json!({"name": name, "type": ty, "required": required});
    if ty == "string" {
        v.as_object_mut().map(|m| m.remove("type"));
    }
    if !description.is_empty() {
        v["description"] = json!(description);
    }
    v
}

/// Run `f` on the blocking pool with the call's cancel token.
async fn blocking(app: Arc<App>, call: Call, f: impl FnOnce(&Op, &Value, Option<&str>) -> Result<Value, Fail> + Send + 'static) -> Result<Value, VerbError> {
    let op = Op::new(app, call.cancel.clone());
    let (args, as_) = (call.args, call.as_);
    tokio::task::spawn_blocking(move || f(&op, &args, as_.as_deref()))
        .await
        .map_err(|e| VerbError::runtime(e.to_string()))?
        .map_err(Into::into)
}

macro_rules! verb {
    ($svc:expr, $app:expr, $name:literal, $desc:expr, $params:expr, |$op:ident, $a:ident, $as_:ident| $body:expr) => {{
        let app = $app.clone();
        $svc.verb(TOOL, $name, $desc, $params, move |call: Call| {
            blocking(app.clone(), call, move |$op: &Op, $a: &Value, $as_: Option<&str>| {
                let _ = &$as_;
                $body
            })
        })
    }};
}

/// The `create` description: the Python's, and one sentence on the create hook.
pub const CREATE_HOOK_SENTENCE: &str = "A project whose main checkout holds an awm-project.toml naming a [scope] create_hook makes its scopes (those under create_hook_prefix, when set) by running that script with the scope's name, and refuses from_branch, branch_name, clone_url and fork_url for them.";

fn create_description() -> String {
    format!("{} {CREATE_HOOK_SENTENCE}", "Create a new scope (a worktree, its data view and its brief) for a project. The new branch defaults to feat/<scope>; pass branch_name to name it explicitly (e.g. a plain branch like 'release' or 'dev'). A branch that already exists is checked out, not recreated. A branch that only a remote carries becomes a local branch tracking that remote. When several remotes carry it, create refuses and names each one. Pass from_branch='<remote>/<branch>' to pick one. An unknown project is refused unless new_project=true, which first creates the project (bare repo, main checkout, data dirs; clone_url or fork_url to start from a remote); omit scope then to create the project alone. A '/' in scope groups it: the worktree lands in that group's directory and the branch under feat/<group>/. Projects grouped by component (check scope search) expect a grouped name. Examples: project='awm', scope='svc-diplomacy' creates projects/awm/svc-diplomacy on branch feat/svc-diplomacy; project='metasmith', scope='engine/presentation' creates projects/metasmith/engine/presentation on branch feat/engine/presentation.")
}

/// `ScopeSyncRequest`'s pattern check, as pydantic words it.
fn sync_strategy(v: Option<String>) -> Result<String, Fail> {
    match v {
        Some(s) if s == "merge" || s == "rebase" => Ok(s),
        Some(s) => Err(Fail::new(
            "ValidationError",
            format!(
                "1 validation error for ScopeSyncRequest\nstrategy\n  String should match pattern '^(merge|rebase)$' [type=string_pattern_mismatch, input_value={}, input_type=str]",
                awm_pycompat::repr::str_repr(&s)
            ),
        )),
        None => Err(Fail::new(
            "ValidationError",
            "1 validation error for ScopeSyncRequest\nstrategy\n  Input should be a valid string [type=string_type, input_value=None, input_type=NoneType]",
        )),
    }
}

/// `int(args.get("pid", args.get("_caller_pid")))` when it is an int or a digit string.
fn pid_arg(a: &Value) -> Option<i64> {
    let v = a.get("pid").or_else(|| a.get(awm_plugin_sdk::service::PID_KEY))?;
    match v {
        Value::Number(n) => n.as_i64(),
        Value::String(s) if !s.is_empty() && s.chars().all(|c| c.is_ascii_digit()) => s.parse().ok(),
        _ => None,
    }
}

fn resolve_ref(op: &Op, literal: &str) -> Result<Value, Fail> {
    let agent = |proj: &str, sc: &str| -> Result<Value, Fail> {
        Ok(match op.agent_record(proj, sc, false)? {
            Some(_) => json!({"kind": "agent", "project": proj, "scope": sc}),
            None => Value::Null,
        })
    };
    if literal.is_empty() || literal == "system" {
        return Ok(json!({"kind": "system"}));
    }
    for prefix in ["agent:", "scope:"] {
        if let Some(rest) = literal.strip_prefix(prefix) {
            return match rest.split_once('/') {
                Some((proj, sc)) => agent(proj, sc),
                None => Ok(Value::Null),
            };
        }
    }
    if let Some(name) = literal.strip_prefix("user:") {
        return Ok(match op.read(|c| db::user_id(c, name))? {
            Some(_) => json!({"kind": "user", "username": name}),
            None => Value::Null,
        });
    }
    if let Some((proj, sc)) = literal.split_once('/') {
        return agent(proj, sc);
    }
    let Some(uid) = op.read(|c| db::user_id(c, literal))? else { return Ok(Value::Null) };
    let name = op.read(|c| db::username(c, &uid))?.filter(|n| !n.is_empty()).unwrap_or_else(|| literal.to_string());
    Ok(json!({"kind": "user", "username": name}))
}

fn heal(op: &Op, a: &Value, as_: Option<&str>) -> Result<Value, Fail> {
    let dry_run = flag(a, "dry_run");
    let project = str_or_none(a, "project");
    if let Some(p) = &project {
        if privacy::from_peer(as_) && privacy::hidden(p, None) {
            return Ok(json!({"scopes": [], "count": 0, "dry_run": dry_run, "unregistered_worktrees": [], "orphaned_rows": [], "audit_errors": []}));
        }
    }
    let report = privacy::visible_rows(op.heal_scopes(project.as_deref(), dry_run)?, as_);
    let audit = op.audit_worktrees(project.as_deref())?;
    let rows = |k: &str| privacy::visible_rows(audit[k].as_array().cloned().unwrap_or_default(), as_);
    Ok(json!({"scopes": report, "count": report.len(), "dry_run": dry_run,
              "unregistered_worktrees": rows("unregistered"), "orphaned_rows": rows("orphaned_rows"), "audit_errors": rows("errors")}))
}

fn private(op: &Op, a: &Value, as_: Option<&str>) -> Result<Value, Fail> {
    if privacy::from_peer(as_) {
        return Err(Fail::runtime("unknown function 'scope_private'"));
    }
    let raw = required(a, "private")?;
    let flag = match raw {
        Value::Bool(b) => *b,
        Value::String(s) if matches!(awm_pycompat::pystr::lower(awm_pycompat::pystr::strip(s)).as_str(), "true" | "false") => {
            awm_pycompat::pystr::lower(awm_pycompat::pystr::strip(s)) == "true"
        }
        other => return Err(Fail::value(format!("private must be true or false, not {}", awm_pycompat::repr::repr(other)))),
    };
    op.set_private(&req_str(a, "project")?, opt_str(a, "scope").as_deref(), flag)
}

fn tag(op: &Op, a: &Value, as_: Option<&str>) -> Result<Value, Fail> {
    let project = req_str(a, "project")?;
    let scope = str_or_none(a, "scope");
    let tags = a.get("tags").filter(|t| !t.is_null());
    if privacy::from_peer(as_) {
        if tags.is_some() {
            return Err(Fail::value("scope tag: a peer may read tags, not set them"));
        }
        if privacy::hidden(&project, scope.as_deref()) {
            return Err(Fail::not_found(format!("Project '{project}' not found")));
        }
    }
    if !op.known_projects()?.contains(&project) {
        return Err(Fail::not_found(format!("Project '{project}' not found")));
    }
    match tags {
        None => op.describe_tags(&project, scope.as_deref()),
        Some(t) => op.set_tags(&project, scope.as_deref(), t),
    }
}

fn create(op: &Op, a: &Value) -> Result<Value, Fail> {
    let tags = match a.get("tags") {
        None | Some(Value::Null) => None,
        Some(t) => Some(crate::tags::normalise(t)?),
    };
    let req = CreateReq {
        project: req_str(a, "project")?,
        scope: opt_str(a, "scope"),
        from_branch: opt_str(a, "from_branch"),
        branch_name: opt_str(a, "branch_name"),
        context: opt_str(a, "context"),
        new_project: flag(a, "new_project"),
        clone_url: opt_str(a, "clone_url"),
        fork_url: opt_str(a, "fork_url"),
        tags,
    };
    Ok(op.create_scope(&req)?.to_json())
}

fn search(op: &Op, a: &Value, as_: Option<&str>) -> Result<Value, Fail> {
    let ask = crate::search::Ask {
        query: opt_str(a, "query"),
        status: get_or(a, "status", "active"),
        project: opt_str(a, "project"),
        limit: int_or(a, "limit", 50)?,
        offset: int_or(a, "offset", 0)?,
        tag: str_or_none(a, "tag"),
    };
    op.search_scopes(&ask, as_)
}

fn ensure(op: &Op, a: &Value) -> Result<Value, Fail> {
    let project = req_str(a, "project")?;
    let scope = req_str(a, "scope")?;
    let bare = crate::path_str(&op.paths.bare(&project));
    let branch = opt_str(a, "branch");
    let worktree = str_or_none(a, "worktree").unwrap_or_default();
    let agent_cli = str_or_none(a, "agent_cli").unwrap_or_else(|| "claude".into());
    let status = str_or_none(a, "status").unwrap_or_else(|| "allocated".into());
    let display = opt_str(a, "display_name");
    let new_project = flag(a, "new_project");
    op.write(|c| {
        if db::project_id(c, &project)?.is_none() {
            if !new_project {
                return Err(Fail::key(&format!("project {} not found — pass new_project=true to create it", awm_pycompat::repr::str_repr(&project))));
            }
            db::ensure_project(c, &project, &bare, None)?;
        }
        let new = db::NewAgent {
            branch: branch.as_deref(),
            worktree: &worktree,
            agent_cli: &agent_cli,
            status: &status,
            is_vagrant: flag(a, "is_vagrant"),
            display_name: display.as_deref(),
        };
        db::ensure_agent(c, &project, &scope, &new)
    })?;
    Ok(json!({"project": project, "scope": scope}))
}

/// scopes' verbs on `svc`.
pub fn service(svc: Service, app: Arc<App>) -> Service {
    let svc = verb!(svc, app, "resolve", "Validate-only read: does this (project, scope) exist and what is its status? Returns {exists, project, scope, status, private} when found, {exists: false, project, scope} when not found.", json!([p("project", "string", true, ""), p("scope", "string", true, "")]), |op, a, as_| {
        let (project, scope) = (req_str(a, "project")?, req_str(a, "scope")?);
        let rec = op.agent_record(&project, &scope, false)?;
        if rec.is_none() || (privacy::from_peer(as_) && privacy::hidden(&project, Some(&scope))) {
            return Ok(json!({"exists": false, "project": project, "scope": scope}));
        }
        let rec = rec.expect("checked");
        Ok(json!({"exists": true, "project": project, "scope": scope, "status": rec.status,
                  "private": privacy::hidden(&project, Some(&scope)), "tags": op.effective_tags(&project, Some(&scope))?}))
    });
    let svc = verb!(svc, app, "ref_resolve", "Resolve a literal author/recipient string to its natural-key identity. Returns {kind, project?, scope?, username?} or null.", json!([p("literal", "string", true, "")]), |op, a, as_| {
        let found = resolve_ref(op, &req_str(a, "literal")?)?;
        if found.get("kind").and_then(Value::as_str) == Some("agent") && privacy::from_peer(as_) {
            let (p, s) = (py_str(&found["project"]), py_str(&found["scope"]));
            if privacy::hidden(&p, Some(&s)) {
                return Ok(Value::Null);
            }
        }
        Ok(found)
    })
    .hidden();
    let svc = verb!(svc, app, "project_ensure", "Idempotent get-or-create of a project. Returns {project}.", json!([p("project", "string", true, ""), p("repo_path", "string", false, ""), p("url", "string", false, "")]), |op, a, as_| {
        let name = req_str(a, "project")?;
        let repo_path = str_or_none(a, "repo_path").unwrap_or_default();
        let url = opt_str(a, "url");
        op.write(|c| db::ensure_project(c, &name, &repo_path, url.as_deref()))?;
        Ok(json!({"project": name}))
    })
    .hidden();
    let svc = verb!(svc, app, "ensure", "Idempotent get-or-create of an agent/scope record. An unknown project is refused unless new_project=true, which registers the project too. Returns {project, scope}.", json!([p("project", "string", true, ""), p("scope", "string", true, ""), p("branch", "string", false, ""), p("worktree", "string", false, ""), p("agent_cli", "string", false, ""), p("status", "string", false, ""), p("is_vagrant", "boolean", false, ""), p("display_name", "string", false, ""), p("new_project", "boolean", false, "Register the project when it does not exist. Default false: an unknown project is an error.")]), |op, a, as_| ensure(op, a));
    let svc = verb!(svc, app, "create", &create_description(), json!([p("project", "string", true, "Name of the project, the directory under projects/ (e.g. 'awm'); it must exist unless new_project is true. Not the workspace or the MCP namespace."), p("scope", "string", false, "Name for the new scope, unique within the project (e.g. 'svc-diplomacy', or 'engine/presentation' to place it in the engine group). Becomes the worktree path under the project. Required, except with new_project to create the project alone."), p("from_branch", "string", false, "Start point of a new branch. Default: the project's default branch. For a branch that already exists, only '<remote>/<branch>' is accepted, and it sets the upstream."), p("branch_name", "string", false, "Branch to check out. Default feat/<scope>. It may already exist locally or on a remote."), p("context", "string", false, "The scope brief: what the scope is for, in at most two sentences and 400 characters. It goes to the journal as brief_set would write it; see brief_set for what it holds."), p("new_project", "boolean", false, "Create the project first when it does not exist. Default false: an unknown project is an error."), p("clone_url", "string", false, "With new_project: clone this remote as the project's bare repo."), p("fork_url", "string", false, "With new_project: fork this GitHub repo and clone the fork."), p("tags", "array", false, "Tags for the new scope, or for the project when it is created alone (see scope tag).")]), |op, a, as_| create(op, a)).timeout(Duration::from_secs(1800));
    let svc = verb!(svc, app, "search", "Search scopes: name matches first, then scopes ranked by the meaning of their brief and context. Each project's main checkout is a scope named for its default branch. Defaults to status='active'; pass status='all' for the full history. With no project, the reply also lists every project, matched and ranked by the same query, with its scope counts per status, and every tag in use with the number of scopes carrying it. Each scope and project carries its effective tags (see scope tag).", json!([p("query", "string", false, ""), p("status", "string", false, ""), p("project", "string", false, "Only this project's scopes. Omit to search every project and list the projects."), p("limit", "integer", false, ""), p("offset", "integer", false, "Skips scopes only; the project list is never paged."), p("tag", "string", false, "Only scopes whose effective tags hold this tag.")]), |op, a, as_| search(op, a, as_));
    let svc = verb!(svc, app, "complete", "Complete (retire) a scope. Optionally merge, and remove the worktree with cleanup=true. The branch stays unless delete_branch=true, which needs cleanup=true. The reply then names the branch and its last SHA. cleanup refuses as scope_delete does; force=true discards and lists what it threw away.", json!([p("project", "string", true, ""), p("scope", "string", true, ""), p("merge", "boolean", false, ""), p("cleanup", "boolean", false, ""), p("force", "boolean", false, ""), p("delete_branch", "boolean", false, "Also delete the scope's git branch. Default false: the branch is kept.")]), |op, a, as_| {
        let out = op.complete_scope(&req_str(a, "project")?, &req_str(a, "scope")?, flag(a, "merge"), flag(a, "cleanup"), flag(a, "force"), flag(a, "delete_branch"))?;
        Ok(out.to_json())
    });
    let svc = verb!(svc, app, "delete", "Delete a scope and remove its worktree. The branch stays unless delete_branch=true. The reply then names the branch and its last SHA. Refuses when the worktree has uncommitted changes -- which covers data as well as code, since a data chunk is committed via its .dvc pin -- or the branch has commits no other branch or remote holds, even when the branch is kept. force=true discards them and the reply lists each one. A live session's claim always refuses.", json!([p("project", "string", true, ""), p("scope", "string", true, ""), p("force", "boolean", false, ""), p("delete_branch", "boolean", false, "Also delete the scope's git branch. Default false: the branch is kept.")]), |op, a, as_| {
        Ok(op.delete_scope(&req_str(a, "project")?, &req_str(a, "scope")?, flag(a, "force"), flag(a, "delete_branch"))?.to_json())
    })
    .timeout(Duration::from_secs(600));
    let svc = verb!(svc, app, "awm_sweep", "One-shot for the deploy (scopes.md R142): empty each worktree's .awm/ folder on this node. Removes the data and skills links, history.md and mcp-opencode.json, moves the DVC mount list and failure sentinel into the worktree's git dir, and rewrites each DVC project's hooks to match. Leaves context.md for the scope briefs one-shot and reports it. Idempotent.", json!([p("dry_run", "boolean", false, "")]), |op, a, as_| {
        if privacy::from_peer(as_) {
            return Err(Fail::new("PermissionError", "scope_awm_sweep runs on its own node"));
        }
        op.awm_sweep(flag(a, "dry_run"))
    })
    .hidden()
    .timeout(Duration::from_secs(600));
    let svc = verb!(svc, app, "heal", "Idempotent repair pass over active scopes: drop an untracked AGENTS.md / CLAUDE.md, and bring a DVC project's data view to whatever its checkout implies — installing the DVC merge driver + hooks, materialising the pinned chunks and building missing shared copies. Pass dry_run=true to preview. The reply also lists worktrees under projects/<project>/ that no scope row names (unregistered_worktrees) and active or allocated rows whose worktree is gone (orphaned_rows). Heal never registers or retires a row for either list.", json!([p("project", "string", false, ""), p("dry_run", "boolean", false, "")]), |op, a, as_| heal(op, a, as_)).timeout(Duration::from_secs(1800));
    let svc = verb!(svc, app, "private", "Mark a project, or one scope of it, private on this node, or clear the mark. A private project or scope, with every scope nested under it and every path under its directory, is invisible to calls from other nodes; callers on this node see it as before. Refuses a mark whose path would also hide another project's or scope's transcripts (projects 'awm' and 'awm-dev', for example). Returns the project's marks.", json!([p("project", "string", true, ""), p("scope", "string", false, "Mark only this scope. Omit to mark the whole project."), p("private", "boolean", true, "true sets the mark, false clears it.")]), |op, a, as_| private(op, a, as_));
    let svc = verb!(svc, app, "tag", "Read or set the tags of a project, a scope, or a scope group such as 'metasmith/lab' (a group needs no scope of its own yet). A scope's effective tags are those of the nearest level that has any: the scope, then each enclosing group, then the project; a nearer set replaces a farther one. Omit tags to read; tags=[] clears the level so it inherits again. Tags label and filter only. Returns the level's own tags, its effective tags and the level they come from.", json!([p("project", "string", true, ""), p("scope", "string", false, "A scope or scope group path. Omit to tag the project."), p("tags", "array", false, "The level's whole tag set, replacing what it had: a list or a comma-separated string. Each tag is lowercase letters, digits, '.', '_' or '-', starts with a letter or digit, and is at most 32 characters.")]), |op, a, as_| tag(op, a, as_));
    let svc = verb!(svc, app, "repair", "Reconcile an on-disk worktree with a missing agents DB row.", json!([p("project", "string", true, ""), p("scope", "string", true, "")]), |op, a, as_| Ok(op.repair_scope(&req_str(a, "project")?, &req_str(a, "scope")?)?.to_json()));
    let svc = verb!(svc, app, "sync", "Sync a scope's feature branch with a base branch via merge or rebase.", json!([p("project", "string", true, ""), p("scope", "string", true, ""), p("strategy", "string", false, ""), p("from_branch", "string", false, "")]), |op, a, as_| {
        let strategy = sync_strategy(get_or(a, "strategy", "merge"))?;
        Ok(op.sync_scope(&req_str(a, "project")?, &req_str(a, "scope")?, &strategy, opt_str(a, "from_branch").as_deref())?.to_json())
    });
    let svc = verb!(svc, app, "git_gather", "Fan-in: merge each peripheral scope's branch into a hub scope's branch (runs in the hub worktree, which must be clean and on the hub branch). Per-peripheral conflicts are aborted and reported; the batch continues. Local-only — no push. strategy='merge' only. A peripheral's data pins ride its code branch, so this merges data too -- there is no separate data leg any more.", json!([p("project", "string", true, ""), p("hub", "string", true, ""), p("peripherals", "array", true, ""), p("strategy", "string", false, "")]), |op, a, as_| {
        let peripherals = str_list(required(a, "peripherals")?)?;
        let strategy = get_or(a, "strategy", "merge").unwrap_or_else(|| "None".into());
        op.gather(&req_str(a, "project")?, &req_str(a, "hub")?, &peripherals, &strategy)
    })
    .timeout(Duration::from_secs(900));
    let svc = verb!(svc, app, "git_scatter", "Fan-out: merge a hub scope's branch into each peripheral scope's branch (each merge runs in that peripheral's worktree). A dirty or off-branch peripheral is skipped; conflicts are aborted and reported; the batch continues. Local-only — no push. strategy='merge' only. The hub's data pins ride its branch, so this fans out data too -- there is no separate data leg any more.", json!([p("project", "string", true, ""), p("hub", "string", true, ""), p("peripherals", "array", true, ""), p("strategy", "string", false, "")]), |op, a, as_| {
        let peripherals = str_list(required(a, "peripherals")?)?;
        let strategy = get_or(a, "strategy", "merge").unwrap_or_else(|| "None".into());
        op.scatter(&req_str(a, "project")?, &req_str(a, "hub")?, &peripherals, &strategy)
    })
    .timeout(Duration::from_secs(900));
    let svc = verb!(svc, app, "claim", "Claim a scope for a live session (scopes.md R3, R42). reflection cd calls this before it moves a session; agents use cd, not this verb. A scope holds at most one live session, and the claim ends when the session's process ends, unless the session waits on other sessions. A held scope answers ok=false with its holder. queue=true joins the scope's first-come queue and returns queued_position. A terminal session (terminal=true) is warned and never blocked.", json!([p("project", "string", true, ""), p("scope", "string", true, ""), p("session_id", "string", true, "The session's id in R9 form (`cc` + hex of its Claude session UUID)."), p("pid", "integer", false, "The session's process id on this node. Default: the calling process."), p("queue", "boolean", false, ""), p("terminal", "boolean", false, "")]), |op, a, as_| {
        op.claim(&req_str(a, "project")?, &req_str(a, "scope")?, &req_str(a, "session_id")?, pid_arg(a), flag(a, "queue"), flag(a, "terminal"))
    })
    .hidden();
    let svc = verb!(svc, app, "release", "End a session's claim on a scope and notify the first session in its queue.", json!([p("project", "string", true, ""), p("scope", "string", true, ""), p("session_id", "string", true, "The session's id in R9 form (`cc` + hex of its Claude session UUID).")]), |op, a, as_| op.release(&req_str(a, "project")?, &req_str(a, "scope")?, &req_str(a, "session_id")?)).hidden();
    let svc = verb!(svc, app, "withdraw", "Take a session out of a scope's queue (scopes.md R46).", json!([p("project", "string", true, ""), p("scope", "string", true, ""), p("session_id", "string", true, "The session's id in R9 form (`cc` + hex of its Claude session UUID).")]), |op, a, as_| op.withdraw(&req_str(a, "project")?, &req_str(a, "scope")?, &req_str(a, "session_id")?)).hidden();
    verb!(svc, app, "claims", "The live session holding each scope, and each scope's queue.", json!([p("project", "string", false, ""), p("scope", "string", false, "")]), |op, a, as_| {
        let mut out = op.listing(str_or_none(a, "project").as_deref(), str_or_none(a, "scope").as_deref())?;
        let rows = privacy::visible_rows(out["claims"].as_array().cloned().unwrap_or_default(), as_);
        out["count"] = json!(rows.len());
        out["claims"] = Value::Array(rows);
        Ok(out)
    })
}
