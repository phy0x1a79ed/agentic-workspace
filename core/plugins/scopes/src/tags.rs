//! Tags on projects, scopes and scope groups. A scope's effective tags are those of the
//! nearest level that has any: the scope, then each enclosing group, then the project. A
//! nearer set replaces a farther one; the levels never merge. Tags label and filter only.

use std::collections::{BTreeSet, HashMap};

use awm_pycompat::repr::{repr, str_repr};
use awm_store::rusqlite::{params, Connection};
use serde_json::{json, Value};

use crate::names::{validate_project, validate_scope};
use crate::{App, Fail};

pub const RULE: &str = "a tag is lowercase letters, digits, '.', '_' or '-', starts with a letter or digit, and is at most 32 characters";

/// Every level's own tags, keyed by `(project, scope path)`; `""` is the project level.
pub type Table = HashMap<(String, String), Vec<String>>;

fn valid(tag: &str) -> bool {
    let b = tag.as_bytes();
    !b.is_empty()
        && b.len() <= 32
        && (b[0].is_ascii_lowercase() || b[0].is_ascii_digit())
        && b[1..].iter().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, b'.' | b'_' | b'-'))
}

/// The sorted, deduplicated tags in `raw`: a list, a JSON list or a comma string.
pub fn normalise(raw: &Value) -> Result<Vec<String>, Fail> {
    let parsed;
    let items: &[Value] = match raw {
        Value::String(s) => {
            let text = awm_pycompat::pystr::strip(s);
            parsed = if text.starts_with('[') {
                serde_json::from_str::<Value>(text).map_err(|e| Fail::new("JSONDecodeError", e.to_string()))?
            } else {
                Value::Array(text.split(',').map(|t| json!(t)).collect())
            };
            match &parsed {
                Value::Array(a) => a,
                other => return Err(Fail::value(format!("tags must be a list or a comma-separated string, not {}", repr(other)))),
            }
        }
        Value::Array(a) => a,
        other => return Err(Fail::value(format!("tags must be a list or a comma-separated string, not {}", repr(other)))),
    };
    let mut out = BTreeSet::new();
    for item in items {
        let Value::String(s) = item else {
            return Err(Fail::value(format!("tag {} is not a string: {RULE}", repr(item))));
        };
        let tag = awm_pycompat::pystr::lower(awm_pycompat::pystr::strip(s));
        if tag.is_empty() {
            continue;
        }
        if !valid(&tag) {
            return Err(Fail::value(format!("invalid tag {}: {RULE}", str_repr(&tag))));
        }
        out.insert(tag);
    }
    Ok(out.into_iter().collect())
}

pub fn load_from(c: &Connection, project: Option<&str>) -> Result<Table, Fail> {
    let mut sql = "SELECT project, scope, tags FROM tags".to_string();
    let mut args: Vec<&str> = vec![];
    if let Some(p) = project {
        sql.push_str(" WHERE project=?");
        args.push(p);
    }
    let mut stmt = c.prepare(&sql)?;
    let rows = stmt.query_map(awm_store::rusqlite::params_from_iter(args), |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, String>(2)?)))?;
    let mut table = Table::new();
    for row in rows {
        let (p, s, t) = row?;
        let tags: Vec<String> = serde_json::from_str(&t).map_err(|e| Fail::new("JSONDecodeError", e.to_string()))?;
        table.insert((p, s), tags);
    }
    Ok(table)
}

/// The levels a scope inherits from, nearest first, ending with the project's `""`.
pub fn levels(scope: Option<&str>) -> Vec<String> {
    let parts: Vec<&str> = scope.filter(|s| !s.is_empty()).map(|s| s.split('/').collect()).unwrap_or_default();
    let mut out: Vec<String> = (1..=parts.len()).rev().map(|i| parts[..i].join("/")).collect();
    out.push(String::new());
    out
}

/// The nearest level with tags and its tags, or `(None, [])` when no level has any.
pub fn nearest(table: &Table, project: &str, scope: Option<&str>) -> (Option<String>, Vec<String>) {
    for level in levels(scope) {
        if let Some(found) = table.get(&(project.to_string(), level.clone())).filter(|t| !t.is_empty()) {
            return (Some(level), found.clone());
        }
    }
    (None, vec![])
}

/// A scope's effective tags, or the project's when `scope` is empty.
pub fn resolve(table: &Table, project: &str, scope: Option<&str>) -> Vec<String> {
    nearest(table, project, scope).1
}

impl App {
    pub fn load_tags(&self, project: Option<&str>) -> Result<Table, Fail> {
        self.read(|c| load_from(c, project))
    }

    pub fn effective_tags(&self, project: &str, scope: Option<&str>) -> Result<Vec<String>, Fail> {
        Ok(resolve(&self.load_tags(Some(project))?, project, scope))
    }

    /// A level's own tags beside the effective tags and the level they come from.
    pub fn describe_tags(&self, project: &str, scope: Option<&str>) -> Result<Value, Fail> {
        let table = self.load_tags(Some(project))?;
        let (level, found) = nearest(&table, project, scope);
        let source = level.map(|l| if l.is_empty() { project.to_string() } else { format!("{project}/{l}") });
        let scope = scope.filter(|s| !s.is_empty());
        let own = table.get(&(project.to_string(), scope.unwrap_or("").to_string())).cloned().unwrap_or_default();
        Ok(json!({"project": project, "scope": scope, "tags": own, "effective": found, "effective_from": source}))
    }

    /// Replace a level's own tags; an empty set clears the level so it inherits.
    pub fn set_tags(&self, project: &str, scope: Option<&str>, tags: &Value) -> Result<Value, Fail> {
        validate_project(project)?;
        if let Some(s) = scope.filter(|s| !s.is_empty()) {
            validate_scope(s)?;
        }
        let clean = normalise(tags)?;
        let level = scope.unwrap_or("");
        self.write(|c| {
            if clean.is_empty() {
                c.execute("DELETE FROM tags WHERE project=? AND scope=?", [project, level])?;
            } else {
                c.execute(
                    "INSERT INTO tags (project, scope, tags, updated_at) VALUES (?, ?, ?, ?) \
                     ON CONFLICT(project, scope) DO UPDATE SET tags=excluded.tags, updated_at=excluded.updated_at",
                    params![project, level, awm_pycompat::json::dumps(&json!(clean)), crate::db::now_ms()],
                )?;
            }
            Ok(())
        })?;
        self.describe_tags(project, scope)
    }

    /// Forget a scope's own tags, leaving its groups' and project's.
    pub fn drop_tags(&self, project: &str, scope: &str) -> Result<(), Fail> {
        self.write(|c| {
            c.execute("DELETE FROM tags WHERE project=? AND scope=?", [project, scope])?;
            Ok(())
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalise_takes_lists_json_and_commas() {
        assert_eq!(normalise(&json!(["B", " a ", "a", ""])).unwrap(), vec!["a", "b"]);
        assert_eq!(normalise(&json!("x, y,,x")).unwrap(), vec!["x", "y"]);
        assert_eq!(normalise(&json!(" [\"q\", \"p\"]")).unwrap(), vec!["p", "q"]);
        assert_eq!(normalise(&json!("")).unwrap(), Vec::<String>::new());
        assert_eq!(normalise(&json!(["-x"])).unwrap_err().message, format!("invalid tag '-x': {RULE}"));
        assert_eq!(normalise(&json!([1])).unwrap_err().message, format!("tag 1 is not a string: {RULE}"));
        assert_eq!(normalise(&json!(3)).unwrap_err().message, "tags must be a list or a comma-separated string, not 3");
        assert!(normalise(&json!(["a".repeat(33)])).is_err());
        assert!(normalise(&json!(["a".repeat(32)])).is_ok());
    }
}
