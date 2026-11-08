//! Edits to a post, kept as versions, and the addendum sweep.
//!
//! An edit writes its `post_versions` row first, then the post row: a crash between the two
//! leaves a version ahead of its post, which the startup reconcile applies. Every earlier body
//! stays with its difflib diff from the one before.

use std::collections::HashMap;
use std::sync::LazyLock;

use awm_pycompat::difflib::unified_diff_text;
use awm_pycompat::time::{ms_to_iso, now_ms};
use awm_store::rusqlite::params;
use regex::Regex;
use serde_json::{json, Value};

use crate::posts::{author_display, Post};
use crate::schema::FOLDED;
use crate::{Fail, Journals, Result};

static ADDENDUM_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)^\s*(?:#+\s*)?(addendum|correction|erratum|update)\b[^\n]*?\(?\b([0-9a-f]{8})(?:[0-9a-f-]{0,28})\b").unwrap()
});

fn diff(old: &str, new: &str, post_id: &str, version: i64) -> String {
    unified_diff_text(old, new, &format!("{post_id}@{}", version - 1), &format!("{post_id}@{version}"))
}

impl Journals {
    /// Store a new version of a post: a whole `body`, or `old` replaced by `new` once.
    pub async fn edit(&self, post_id: &str, editor: &str, body: Option<&str>, old: Option<&str>, new: Option<&str>, reason: &str) -> Result<Value> {
        let _w = self.write.lock().await;
        self.edit_locked(post_id, editor, body, old, new, reason, false).await
    }

    /// The edit, with `crash_before_post` stopping it between its two writes (tests only).
    #[doc(hidden)]
    pub async fn edit_locked(
        &self,
        post_id: &str,
        editor: &str,
        body: Option<&str>,
        old: Option<&str>,
        new: Option<&str>,
        reason: &str,
        crash_before_post: bool,
    ) -> Result<Value> {
        let mut p = self.get_post(post_id).await?.ok_or_else(|| Fail::value(format!("no post {post_id}")))?;
        let current = p.body.clone();
        let body = match body {
            Some(b) => b.to_string(),
            None => {
                let (Some(old), Some(new)) = (old, new) else { return Err(Fail::value("give body, or old and new")) };
                if current.matches(old).count() != 1 {
                    return Err(Fail::value("old must occur exactly once in the post"));
                }
                current.replacen(old, new, 1)
            }
        };
        if body == current {
            return Err(Fail::value("the edit changes nothing"));
        }
        let version = p.version + 1;
        let now = now_ms();
        let d = diff(&current, &body, post_id, version);
        let (id, cur, author, ms, b, ed, rs) = (post_id.to_string(), current.clone(), p.author.clone(), p.ms, body.clone(), editor.to_string(), reason.to_string());
        self.db.blocking_write(move |tx| {
            if version == 2 {
                tx.execute(
                    "INSERT OR IGNORE INTO post_versions (post_id, version, body, diff, editor, reason, ts) VALUES (?, 1, ?, '', ?, '', ?)",
                    params![id, cur, author, ms],
                )?;
            }
            tx.execute(
                "INSERT INTO post_versions (post_id, version, body, diff, editor, reason, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
                params![id, version, b, d, ed, rs, now],
            )?;
            Ok::<_, Fail>(())
        })?;
        if crash_before_post {
            return Err(Fail::runtime("stopped between the version and the post"));
        }
        p.body = body;
        p.version = version;
        self.write_posts(std::slice::from_ref(&p)).await?;
        Ok(json!({"post": p.to_dict(&self.tag_names()?), "version": version}))
    }

    /// Every version of a post, oldest first, each with its diff from the one before.
    pub async fn history(&self, post_id: &str) -> Result<Value> {
        let p = self.get_post(post_id).await?.ok_or_else(|| Fail::value(format!("no post {post_id}")))?;
        let id = post_id.to_string();
        let rows: Vec<Value> = self.db.blocking_read(move |c| {
            let mut stmt = c.prepare("SELECT version, body, diff, editor, reason, ts FROM post_versions WHERE post_id=? ORDER BY version")?;
            let rows = stmt
                .query_map([id], |r| {
                    Ok(json!({
                        "version": r.get::<_, i64>(0)?,
                        "body": r.get::<_, String>(1)?,
                        "diff": r.get::<_, String>(2)?,
                        "editor": r.get::<_, String>(3)?,
                        "reason": r.get::<_, String>(4)?,
                        "ts": ms_to_iso(r.get::<_, i64>(5)?).unwrap_or_default(),
                    }))
                })?
                .collect::<std::result::Result<Vec<_>, _>>()?;
            Ok::<_, Fail>(rows)
        })?;
        if rows.is_empty() {
            return Ok(json!({"post_id": post_id, "versions": [{"version": 1, "body": p.body, "diff": "",
                "editor": author_display(&p.author), "reason": "", "ts": p.ts()}]}));
        }
        Ok(json!({"post_id": post_id, "versions": rows}))
    }

    /// Posts that open as an addendum or correction to an earlier post of the same scope,
    /// named by its id or the first 8 characters of it.
    pub async fn addenda(&self, project: Option<&str>, scope: Option<&str>) -> Result<Vec<Value>> {
        let all = self.scan("true").await?;
        let mut by_owner: HashMap<(&str, &str), Vec<&Post>> = HashMap::new();
        for p in &all {
            by_owner.entry((p.project.as_str(), p.scope.as_str())).or_default().push(p);
        }
        let mut rows: Vec<&Post> = all
            .iter()
            .filter(|p| !p.has(FOLDED))
            .filter(|p| project.is_none_or(|x| x.is_empty() || p.project == x))
            .filter(|p| scope.is_none_or(|x| x.is_empty() || p.scope == x))
            .collect();
        rows.sort_by_key(|p| (p.ms, p.seq));
        let mut found = Vec::new();
        for r in rows {
            let Some(m) = ADDENDUM_RE.captures(&r.body) else { continue };
            let prefix = m[2].to_lowercase();
            let target = by_owner[&(r.project.as_str(), r.scope.as_str())]
                .iter()
                .filter(|t| t.id.to_lowercase().starts_with(&prefix) && t.id != r.id && t.ms < r.ms)
                .max_by_key(|t| (t.ms, t.seq));
            if let Some(t) = target {
                let first = awm_pycompat::pystr::splitlines(&r.body, false).first().map(|l| awm_pycompat::pystr::head(l, 160).to_string()).unwrap_or_default();
                found.push(json!({"addendum": r.id, "target": t.id, "project": r.project, "scope": r.scope, "first_line": first}));
            }
        }
        Ok(found)
    }

    /// Append the addendum to its target as a new version, then retire the addendum.
    pub async fn fold(&self, addendum_id: &str, target_id: &str, editor: &str) -> Result<Value> {
        let add = self.get_post(addendum_id).await?;
        let target = self.get_post(target_id).await?;
        let (Some(add), Some(target)) = (add, target) else { return Err(Fail::value("no such post")) };
        let stamp: String = add.ts().chars().take(10).collect();
        let body = format!(
            "{}\n\n**Correction ({stamp})**\n\n{}\n",
            awm_pycompat::pystr::rstrip(&target.body),
            awm_pycompat::pystr::strip(&add.body)
        );
        let _w = self.write.lock().await;
        let out = self.edit_locked(target_id, editor, Some(&body), None, None, &format!("fold addendum {addendum_id}"), false).await?;
        let mut add = self.get_post(addendum_id).await?.ok_or_else(|| Fail::value("no such post"))?;
        let (raw, tid) = (add.meta_raw.clone(), target_id.to_string());
        // SQLite's json_set, as the Python ran it, so the stored text is the same bytes.
        add.meta_raw = self.db.blocking_read(move |c| {
            Ok::<String, Fail>(c.query_row("SELECT json_set(?, '$.folded_into', ?)", params![raw, tid], |r| r.get(0))?)
        })?;
        if !add.has(FOLDED) {
            add.tag_ids.push(FOLDED.into());
        }
        self.write_posts(std::slice::from_ref(&add)).await?;
        Ok(out)
    }

    /// List the addenda and their targets. With `apply`, fold each one in.
    pub async fn sweep(&self, project: Option<&str>, scope: Option<&str>, apply: bool, editor: &str) -> Result<Value> {
        let found = self.addenda(project, scope).await?;
        if apply {
            for f in &found {
                self.fold(f["addendum"].as_str().unwrap_or(""), f["target"].as_str().unwrap_or(""), editor).await?;
            }
        }
        Ok(json!({"addenda": found, "applied": apply}))
    }
}
