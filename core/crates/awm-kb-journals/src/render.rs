//! The history render: a scope's journal entries and its project's, as markdown. Every
//! session's startup ritual reads it, so its layout is a contract, byte for byte.

use std::sync::LazyLock;

use regex::Regex;
use serde_json::{json, Map, Value};

use crate::caller::Exclusion;
use crate::posts::{and, eq, Post};
use crate::schema::JOURNAL;
use crate::{py_str, py_repr, truthy, Fail, Journals, Result};

static TAG_RUN_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"<[^>\n]*>").unwrap());
static SPACE_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\s+").unwrap());

/// How many of a scope's own journal entries always show, and how many of its project's.
const OWN_JOURNAL_FLOOR: usize = 10;
const PROJECT_JOURNAL_WINDOW: usize = 15;

/// A string safe to show as a one-line markdown title: no tag-like runs, no line breaks.
pub fn neutralise_title(text: &str) -> String {
    let t = TAG_RUN_RE.replace_all(text, "");
    let t = SPACE_RE.replace_all(&t, " ");
    let t = awm_pycompat::pystr::strip(&t);
    if t.chars().count() > 80 {
        format!("{}…", t.chars().take(80).collect::<String>())
    } else {
        t.to_string()
    }
}

fn rs(v: &str) -> String {
    awm_pycompat::repr::str_repr(v)
}

/// `refs.validate_project`.
pub fn validate_project(name: &str) -> Result<()> {
    if name.is_empty() {
        return Err(Fail::value("project name must be a non-empty string"));
    }
    if name == "." || name == ".." || name.starts_with('.') {
        return Err(Fail::value(format!("project name cannot start with '.' (got {})", rs(name))));
    }
    for ch in ['/', '\\', '\0'] {
        if name.contains(ch) {
            return Err(Fail::value(format!("project name cannot contain {} (got {})", rs(&ch.to_string()), rs(name))));
        }
    }
    Ok(())
}

/// `refs.validate_scope`.
pub fn validate_scope(scope: &str) -> Result<()> {
    if scope.is_empty() {
        return Err(Fail::value("scope name must be a non-empty string"));
    }
    if scope.starts_with('/') || scope.ends_with('/') {
        return Err(Fail::value(format!("scope name cannot start or end with '/' (got {})", rs(scope))));
    }
    for segment in scope.split('/') {
        if segment.is_empty() || segment.starts_with('.') {
            return Err(Fail::value(format!("scope name has an empty or dot-leading segment (got {})", rs(scope))));
        }
        for ch in ['\\', '\0'] {
            if segment.contains(ch) {
                return Err(Fail::value(format!("scope name cannot contain {} (got {})", rs(&ch.to_string()), rs(scope))));
            }
        }
    }
    Ok(())
}

fn shown(meta: &Map<String, Value>, key: &str) -> Option<String> {
    meta.get(key).filter(|v| truthy(v)).map(py_str)
}

fn entry(p: &Post, tag_scope: bool) -> Vec<String> {
    let meta = p.meta();
    let raw = match meta.get("title").filter(|v| truthy(v)) {
        Some(Value::String(s)) => s.clone(),
        Some(v) => py_str(v),
        None => p.body.clone(),
    };
    let title = neutralise_title(&raw);
    let outcome = shown(&meta, "outcome").map(|o| format!(" [{o}]")).unwrap_or_default();
    let tag = if tag_scope { format!(" ({})", p.scope) } else { String::new() };
    let mut out = vec![format!("**[{}] {title}**{outcome}{tag}", p.id)];
    if let Some(d) = shown(&meta, "deviations") {
        out.push(format!("- Deviations: {d}"));
    }
    if let Some(s) = shown(&meta, "suggestions") {
        out.push(format!("- Suggestions: {s}"));
    }
    out.push(String::new());
    out
}

impl Journals {
    /// `render.generate_history_md`: the scope's own journal entries, then its project's.
    pub async fn history_md(&self, project: &str, scope: &str, exclude: &Exclusion) -> Result<String> {
        let mut preface = format!("# Project History: {project}\n\n");
        let unread = self.unread_count(project, scope, None, exclude).await?;
        if unread > 0 {
            preface.push_str(&format!(
                "**Inbox:** {unread} unread message{} at render time. Read the digest with `journal(verb=\"inbox\", args={{project:\"{project}\", scope:\"{scope}\"}})`.\n\n",
                if unread != 1 { "s" } else { "" }
            ));
        }
        let names = self.tag_names()?;
        let mut journals: Vec<Post> = self
            .scan(&and(&[eq("owner_project", project), crate::posts::has_tag(JOURNAL)]))
            .await?
            .into_iter()
            .filter(|p| !exclude.hides(p, &names))
            .collect();
        journals.sort_by(|a, b| (b.ms, b.seq).cmp(&(a.ms, a.seq)));
        let (own, others): (Vec<&Post>, Vec<&Post>) = journals.iter().partition(|p| p.scope == scope);
        let omitted = |shown: usize, total: usize| -> Option<String> {
            (total > shown).then(|| {
                format!(
                    "*+{} older not shown — `journal(verb=\"fetch\")` with project={project} tags=journal to read them.*\n",
                    total - shown
                )
            })
        };
        let mut sections = Vec::new();
        if !own.is_empty() {
            let mut lines = vec![format!("## This Scope's Journal ({scope})\n")];
            for p in own.iter().take(OWN_JOURNAL_FLOOR) {
                lines.extend(entry(p, false));
            }
            lines.extend(omitted(own.len().min(OWN_JOURNAL_FLOOR), own.len()));
            sections.push(lines.join("\n"));
        }
        if !others.is_empty() {
            let mut lines = vec!["## Sibling Scopes' Journal\n".to_string()];
            for p in others.iter().take(PROJECT_JOURNAL_WINDOW) {
                lines.extend(entry(p, true));
            }
            lines.extend(omitted(others.len().min(PROJECT_JOURNAL_WINDOW), others.len()));
            sections.push(lines.join("\n"));
        }
        if own.is_empty() && others.is_empty() {
            sections.push(
                "*No journal entries yet. They appear here after agents post them via `journal(verb=\"post\")` with tags=[journal].*\n"
                    .to_string(),
            );
        }
        Ok(format!("{preface}{}", sections.join("\n")))
    }

    /// `render.render`: the history as the caller `as_` may see it.
    pub async fn render(&self, project: &Value, scope: &Value, as_: Option<&str>) -> Result<Value> {
        let (Value::String(project), Value::String(scope)) = (project, scope) else {
            let bad = if project.is_string() { scope } else { project };
            let which = if project.is_string() { "scope" } else { "project" };
            let _ = py_repr(bad);
            return Err(Fail::value(format!("{which} name must be a non-empty string")));
        };
        validate_project(project)?;
        validate_scope(scope)?;
        let content = self.history_md(project, scope, &Exclusion::of(as_)).await?;
        let lines = awm_pycompat::pystr::splitlines(&content, false).len();
        Ok(json!({"content": content, "history_lines": lines}))
    }
}
