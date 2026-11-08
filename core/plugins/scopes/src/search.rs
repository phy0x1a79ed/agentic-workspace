//! `scope search`: scopes whose name holds the query first, then the scopes the journal's
//! brief posts rank for it, and with no project the projects and the tags in use.
//!
//! scopes keeps no index. The journal answers `journal_query` for posts tagged `brief`, and
//! each hit names its scope by its `project` and `scope`. A project ranks by the best hit
//! among its scopes.

use std::collections::{BTreeMap, BTreeSet};
use std::time::Duration;

use awm_plugin_sdk::privacy;
use awm_store::rusqlite::{Connection, OptionalExtension, Row};
use serde_json::{json, Map, Value};

use crate::lifecycle::ScopeInfo;
use crate::tags::{normalise, resolve, Table};
use crate::{Fail, Op};

/// The (project, scope) of each hit, and the journal's `degraded` block.
pub type Briefs = (Vec<(String, String)>, Option<Value>);

const SCOPE_ROW_SQL: &str = "SELECT a.id, a.scope, a.status, a.branch, a.worktree, a.created_at, p.name AS project_name, \
     (SELECT COUNT(*) FROM agents a2 JOIN projects p2 ON p2.id = a2.project_id \
      WHERE p2.name = p.name AND a2.scope = a.scope AND a2.created_at <= a.created_at) AS session \
     FROM agents a JOIN projects p ON p.id = a.project_id WHERE 1=1";

/// How many brief hits to ask the journal for: enough to fill a page past superseded
/// versions of the same brief and a tag filter.
const MIN_ASK: usize = 50;
// journal_query answers at most awm-index's `MAX_MAX` hits.
const MAX_ASK: usize = 100;

/// What `search` was asked.
#[derive(Debug, Clone, Default)]
pub struct Ask {
    pub query: Option<String>,
    pub status: Option<String>,
    pub project: Option<String>,
    pub limit: i64,
    pub offset: i64,
    pub tag: Option<String>,
}

fn render(r: &Row<'_>) -> awm_store::rusqlite::Result<ScopeInfo> {
    let status: String = r.get("status")?;
    let shown = match status.as_str() {
        "allocated" | "active" => "active".to_string(),
        "retired" => "completed".to_string(),
        _ => status,
    };
    let worktree: String = r.get("worktree")?;
    let session: Option<i64> = r.get("session")?;
    Ok(ScopeInfo {
        project: r.get("project_name")?,
        scope: r.get("scope")?,
        status: shown,
        branch: r.get("branch")?,
        worktree: worktree.clone(),
        repo_path: Some(worktree),
        session: session.filter(|s| *s != 0).unwrap_or(1),
        private: false,
        tags: vec![],
    })
}

fn query_rows(c: &Connection, sql: &str, params: &[String]) -> Result<Vec<ScopeInfo>, Fail> {
    let mut stmt = c.prepare(sql)?;
    let it = stmt.query_map(awm_store::rusqlite::params_from_iter(params), render)?;
    Ok(it.collect::<Result<_, _>>()?)
}

/// Python's slice `[start:end]` on a list, with non-negative or negative bounds.
fn slice<T: Clone>(items: &[T], start: i64, end: Option<i64>) -> Vec<T> {
    let n = items.len() as i64;
    let norm = |i: i64| if i < 0 { (n + i).max(0) } else { i.min(n) };
    let (s, e) = (norm(start), end.map(norm).unwrap_or(n));
    if s >= e {
        return vec![];
    }
    items[s as usize..e as usize].to_vec()
}

/// The journal's brief hits as distinct `(project, scope)` in rank order, with its
/// `degraded` block when it sent one.
pub fn brief_hits(answer: &Value) -> Briefs {
    let mut hits: Vec<&Value> = vec![];
    fn section(v: Option<&Value>) -> Vec<&Value> {
        match v {
            Some(Value::Array(a)) => a.iter().collect(),
            Some(Value::Object(m)) => m.values().filter_map(Value::as_array).flatten().collect(),
            _ => vec![],
        }
    }
    if answer.get("exact").is_some() || answer.get("related").is_some() {
        hits.extend(section(answer.get("exact")));
        hits.extend(section(answer.get("related")));
    } else {
        hits.extend(section(answer.get("hits")));
    }
    let mut seen = BTreeSet::new();
    let mut out = vec![];
    for h in hits {
        let (Some(p), Some(s)) = (h.get("project").and_then(Value::as_str), h.get("scope").and_then(Value::as_str)) else { continue };
        if p.is_empty() || s.is_empty() {
            continue;
        }
        if seen.insert((p.to_string(), s.to_string())) {
            out.push((p.to_string(), s.to_string()));
        }
    }
    (out, answer.get("degraded").filter(|d| !d.is_null()).cloned())
}

impl Op {
    /// Ask the journal which scopes' briefs match `query`.
    fn ask_journal(&self, query: &str, project: Option<&str>, want: usize) -> Result<Briefs, String> {
        let mut filter = json!({"tag": "brief"});
        if let Some(p) = project {
            filter["project"] = json!(p);
        }
        let args = json!({"query": query, "filter": filter, "max": want.clamp(MIN_ASK, MAX_ASK)});
        let answer = self.out.call("journal", "journal_query", args, Duration::from_secs(30))?;
        Ok(brief_hits(&answer))
    }

    /// `ScopeListResponse` as the handler dumps it: `degraded`, `projects` and `tags` only
    /// when set.
    pub fn search_scopes(&self, ask: &Ask, as_: Option<&str>) -> Result<Value, Fail> {
        let peer = privacy::from_peer(as_);
        let project = ask.project.as_deref().filter(|p| !p.is_empty());
        let table = self.load_tags(project)?;
        let tag = match &ask.tag {
            Some(t) => Some(normalise(&json!([t]))?.into_iter().next().ok_or_else(|| Fail::new("IndexError", "list index out of range"))?),
            None => None,
        };
        let visible = |info: &ScopeInfo| !(peer && (privacy::hidden(&info.project, Some(&info.scope)) || privacy::hidden_path(&info.worktree)));
        let shown = |info: &ScopeInfo, table: &Table| visible(info) && tag.as_ref().is_none_or(|t| resolve(table, &info.project, Some(&info.scope)).contains(t));

        let status = ask.status.as_deref();
        let mut wh = String::new();
        let mut params: Vec<String> = vec![];
        match status {
            Some(s) if !s.is_empty() && s != "all" => match s {
                "active" => wh.push_str(" AND a.status IN ('allocated','active')"),
                "completed" => wh.push_str(" AND a.status='retired'"),
                "deleted" => return Ok(json!({"scopes": [], "total": 0})),
                other => {
                    wh.push_str(" AND a.status = ?");
                    params.push(other.to_string());
                }
            },
            _ => {}
        }
        if let Some(p) = project {
            wh.push_str(" AND p.name = ?");
            params.push(p.to_string());
        }
        let with_mains = matches!(status, None | Some("") | Some("all") | Some("active"));
        let query = ask.query.as_deref().filter(|q| !q.is_empty());

        let mut name_sql = wh.clone();
        let mut name_params = params.clone();
        if let Some(q) = query {
            name_sql.push_str(" AND a.scope LIKE ?");
            name_params.push(format!("%{q}%"));
        }
        let mut found = self.read(|c| query_rows(c, &format!("{SCOPE_ROW_SQL}{name_sql} ORDER BY p.name, a.scope"), &name_params))?;
        if with_mains {
            let ql = query.map(awm_pycompat::pystr::lower);
            found.extend(self.main_checkouts(project)?.into_values().filter(|m| ql.as_ref().is_none_or(|q| awm_pycompat::pystr::lower(&m.scope).contains(q))));
        }
        found.retain(|s| shown(s, &table));
        found.sort_by(|a, b| (&a.project, &a.scope).cmp(&(&b.project, &b.scope)));
        let limit = ask.limit;
        let mut merged = slice(&found, ask.offset, Some(ask.offset + limit));

        let mut degraded: Option<Value> = None;
        let mut ranked_projects: Option<Vec<String>> = None;
        if let Some(q) = query {
            let want = ((ask.offset + limit).max(1) as usize).saturating_mul(5);
            match self.ask_journal(q, project, want) {
                Ok((hits, deg)) => {
                    degraded = deg;
                    let mut order: Vec<String> = vec![];
                    for (p, _) in &hits {
                        if !order.contains(p) {
                            order.push(p.clone());
                        }
                    }
                    ranked_projects = Some(order);
                    let seen: BTreeSet<String> = merged.iter().map(|s| format!("{}/{}", s.project, s.scope)).collect();
                    // The hits a row of the searched status and project stands behind.
                    let mut allowed = vec![];
                    for (p, s) in &hits {
                        let sql = format!("{SCOPE_ROW_SQL} AND p.name = ? AND a.scope = ?{wh} ORDER BY a.created_at DESC");
                        let mut args = vec![p.clone(), s.clone()];
                        args.extend(params.iter().cloned());
                        let row = self.read(|c| {
                            let mut stmt = c.prepare(&sql)?;
                            Ok(stmt.query_row(awm_store::rusqlite::params_from_iter(&args), render).optional()?)
                        })?;
                        if let Some(info) = row {
                            allowed.push(info);
                        }
                    }
                    for info in slice(&allowed, ask.offset, None) {
                        if merged.len() as i64 >= limit {
                            break;
                        }
                        if seen.contains(&format!("{}/{}", info.project, info.scope)) {
                            continue;
                        }
                        if shown(&info, &table) {
                            merged.push(info);
                        }
                    }
                }
                Err(e) => {
                    let text = format!("RuntimeError({})", awm_pycompat::repr::str_repr(&e));
                    degraded = Some(json!({"semantic": "error", "error": awm_pycompat::pystr::head(&text, 300), "fallback": "keyword"}));
                }
            }
        }

        for info in merged.iter_mut() {
            info.private = privacy::hidden(&info.project, Some(&info.scope));
            info.tags = resolve(&table, &info.project, Some(&info.scope));
        }
        let mut out = Map::new();
        out.insert("scopes".into(), Value::Array(merged.iter().map(ScopeInfo::to_json).collect()));
        out.insert("total".into(), json!(merged.len()));
        if project.is_none() {
            let plimit = query.map(|_| limit.max(0) as usize);
            let mut listing = self.search_projects(query, plimit, ranked_projects.as_deref())?;
            if peer {
                listing.retain(|p| !privacy::hidden(&p.name, None));
            }
            for p in listing.iter_mut() {
                p.tags = resolve(&table, &p.name, None);
            }
            let in_use = self.tags_in_use(&table, &wh, &params, with_mains, &visible)?;
            if let Some(d) = degraded {
                out.insert("degraded".into(), d);
            }
            out.insert("projects".into(), Value::Array(listing.iter().map(|p| p.to_json()).collect()));
            out.insert("tags".into(), json!(in_use));
        } else if let Some(d) = degraded {
            out.insert("degraded".into(), d);
        }
        Ok(Value::Object(out))
    }

    /// Each tag the searched status's scopes carry, with how many carry it.
    fn tags_in_use(&self, table: &Table, wh: &str, params: &[String], with_mains: bool, visible: &dyn Fn(&ScopeInfo) -> bool) -> Result<BTreeMap<String, i64>, Fail> {
        let rows = self.read(|c| query_rows(c, &format!("{SCOPE_ROW_SQL}{wh}"), params))?;
        let mut scopes: Vec<((String, String), ScopeInfo)> = vec![];
        let mut put = |info: ScopeInfo| {
            let key = (info.project.clone(), info.scope.clone());
            match scopes.iter_mut().find(|(k, _)| *k == key) {
                Some(slot) => slot.1 = info,
                None => scopes.push((key, info)),
            }
        };
        for r in rows {
            put(r);
        }
        if with_mains {
            for m in self.main_checkouts(None)?.into_values() {
                put(m);
            }
        }
        let mut counts = BTreeMap::new();
        for ((p, s), info) in &scopes {
            if !visible(info) {
                continue;
            }
            for t in resolve(table, p, Some(s)) {
                *counts.entry(t).or_insert(0) += 1;
            }
        }
        Ok(counts)
    }
}
